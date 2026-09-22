"""Jev 参与门控的纯逻辑层（不依赖 MaiBot SDK，可离线单测）。

这些实现从 生产实现 的生产代码抽取，两边保持一致；
改任何一边时记得同步另一边。三块内容：

1. planner 状态文本提取：只保留真实聊天内容，剔除人设/记忆/框架注入
2. @机器人 豁免：只看**最新消息**（state 尾部窗口），避免历史提及污染
3. Jev Choice 原语：请求体构造与响应解析（含 confidence / probabilities 两把尺子）
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# 1. planner 状态文本提取
# ---------------------------------------------------------------------------

_FRAMEWORK_TEXT_MARKERS: tuple[str, ...] = (
    "内部参考",
    "你需要输出",
    "当前聊天额外注意事项",
    "<system-reminder>",
    "[上下文恢复]",
    "黑话参考",
    "启发式记忆",
    "人物画像",
)

_MESSAGE_TAG_RE = re.compile(r"<message\b")

def _is_framework_text(content: str) -> bool:
    """判断 Item 文本是否为框架注入（人设/群规/记忆/执行指令），而非聊天内容。

    含 ``<message>`` 标签的按聊天内容保留；命中排除标记的丢弃。
    """

    if _MESSAGE_TAG_RE.search(content):
        return False
    if content.lstrip().startswith("时间："):
        return True
    return any(marker in content for marker in _FRAMEWORK_TEXT_MARKERS)

def extract_planner_state_text(items: Any, max_chars: int = 3000) -> str:
    """从 planner 请求 items 中提取**纯聊天内容**，作为 Jev 判定的 state。

    只保留带 ``<message>`` 标签的聊天记录与普通消息片段；
    剔除人设、群规、内部参考记忆、planner 执行指令等框架注入，
    避免稀释信号。超长时取末尾（最新内容最相关）。
    """

    if not isinstance(items, list):
        return ""
    texts: list[str] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("item_type") or "") == "SystemMessageItem":
            continue
        parts = raw.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not (isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)):
                continue
            cleaned = part["text"].strip()
            if not cleaned:
                continue
            if _is_framework_text(cleaned):
                continue
            texts.append(cleaned)
    if not texts:
        return ""
    joined = "\n".join(texts)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
    return joined

# ---------------------------------------------------------------------------
# 2. @机器人 豁免（只看 state 尾部窗口）
# ---------------------------------------------------------------------------

DEFAULT_MENTION_WINDOW_CHARS = 600
DEFAULT_BOT_MENTION_ALIASES: tuple[str, ...] = ()


def is_bot_addressed(state_text: str, aliases: Iterable[str] = DEFAULT_BOT_MENTION_ALIASES,
                     window_chars: int = DEFAULT_MENTION_WINDOW_CHARS) -> bool:
    """判断**最新那段聊天内容**里是否出现 @ 机器人（含群名片后缀）的召唤信号。

    MaiBot 把 AtComponent 内联渲染成 ``@群名片``（如 ``@<机器人昵称>``），
    用「@ + 昵称前缀」匹配即可覆盖群名片带后缀的情况。

    只扫 state 尾部 window_chars 字符，不扫整窗：planner 上下文会长期残留
    「<机器人昵称>曾被 @」的痕迹——历史消息本身、模型自己的推理文本、上一轮的工具参数里
    都会带着 ``@<机器人昵称>`` 字样。实测（09-21，13 次触发）整窗扫描有 23% 是假阳性
    （当轮其实在 @ 另一个 bot），尾部 600 字符能把两者全部区分开。
    漏判的代价很小：没命中豁免时仍交给 Jev 判定，而被点名时 p(no_reply)≈0.5
    通常低于阈值 → 照样放行进完整 planner。
    """

    if not state_text:
        return False
    tail = state_text[-window_chars:] if window_chars and window_chars > 0 else state_text
    for raw in aliases:
        alias = str(raw or "").strip()
        # 负向断言排除 a@example.com 这类邮箱形态。
        if alias and re.search(r"(?<![A-Za-z0-9])@\s*" + re.escape(alias), tail):
            return True
    return False

# ---------------------------------------------------------------------------
# 3. Jev Choice 原语
# ---------------------------------------------------------------------------

# 判定指令与判定标准都**按机器人昵称模板化**：名字进提示词判定更准，
# 同时不把任何具体实例的名字写死在代码里。{name} 由 bot_aliases 的第一项填充。
JEV_TIMING_GATE_INSTRUCTION_TEMPLATE = (
    "判断：当前聊天中，是否已经出现值得「{name}」（一位参与群聊的角色）进入完整思考并可能发言的时机？"
)

JEV_TIMING_GATE_CRITERIA_TEMPLATE: dict[str, str] = {
    "continue": (
        "值得进入完整思考：有人提到、@、引用或接续{name}；{name}正在参与的话题被承接；"
        "或出现自然可接话的窗口（接梗、附和、轻量互动、活跃气氛）。"
    ),
    "no_reply": (
        "本轮无需参与：明显是群友之间的互动且与{name}无关；只是自言自语或随手感叹；"
        "或{name}刚回复过且没有新的承接信号、没有被再次 cue、也没有新的可接话点。"
    ),
}


def build_gate_prompts(bot_name: str) -> tuple[str, dict[str, str]]:
    """按机器人昵称生成 Jev 判定用的指令与标准。

    Args:
        bot_name: 机器人在群里的昵称（建议取 bot_aliases 的第一项）。

    Returns:
        tuple: ``(instructions, criteria)``，可直接传给 :func:`build_jev_choice_body`。
    """

    name = str(bot_name or "").strip() or "本机器人"
    return (
        JEV_TIMING_GATE_INSTRUCTION_TEMPLATE.format(name=name),
        {key: text.format(name=name) for key, text in JEV_TIMING_GATE_CRITERIA_TEMPLATE.items()},
    )


def build_jev_choice_body(text: str, *, criteria: Mapping[str, str],
                          instructions: str, model: str = "jev-latest",
                          question_id: str = "timing_gate") -> dict[str, Any]:
    """构造 Jev 的 Choice 请求体。"""

    return {
        "model": model,
        "state": text,
        "questions": {
            question_id: {
                "type": "choice",
                "instructions": instructions,
                "criteria": dict(criteria),
            }
        },
    }

def jev_extract_choice(payload: Any, question_id: str = "timing_gate") -> tuple[str | None, float | None]:
    """从 Jev 响应中取出 (choice, confidence)；结构不符时返回 (None, None)。"""

    try:
        answer = payload["answers"][question_id]
        choice = str(answer.get("choice") or "").strip() or None
        confidence = answer.get("confidence")
        confidence = float(confidence) if confidence is not None else None
        return choice, confidence
    except Exception:
        return None, None

def jev_extract_probability(payload: Any, label: str, question_id: str = "timing_gate") -> float | None:
    """取出某个选项的 ``probabilities`` 值；结构不符时返回 None。

    实测（同一输入重复 8 次）：``probabilities`` 的抖动约为 ``confidence`` 的一半
    （σ 0.009~0.023 vs 0.020~0.044），所以门控的控制流用它，两者都记进日志。
    """

    try:
        return float(payload["answers"][question_id]["probabilities"][label])
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 4. 抑制判定（纯函数，便于离线单测）
# ---------------------------------------------------------------------------

def evaluate_decision(
    choice: str | None,
    confidence: float | None,
    probabilities: Any,
    *,
    probability_threshold: float = 0.80,
    confidence_fallback_threshold: float = 0.62,
) -> dict[str, Any]:
    """把 Jev 的原始响应归一成"是否抑制本轮"的判定结果。

    控制流优先用 ``probabilities["no_reply"]``：实测同一输入重复调用 8 次，
    它的极差/标准差约为 ``confidence`` 的一半，而 confidence 是收缩过的另一把尺子。
    端点未返回 probabilities 时退回 confidence，阈值本身已含实测抖动余量。

    Returns:
        dict: ``suppress``（是否抑制）、``evidence``（依据字段名）、``value``/``limit``（比较值）、
        ``conf_text``/``p_text``（日志用文本）、``reason``（未抑制时的原因，抑制时为 ""）。
    """

    conf_text = f"{confidence:.2f}" if confidence is not None else "?"
    probability = probabilities.get("no_reply") if isinstance(probabilities, dict) else None
    p_text = f"{probability:.2f}" if isinstance(probability, (int, float)) else "?"

    if choice is None:
        return {"suppress": False, "reason": "no_result", "evidence": "", "value": 0.0,
                "limit": 0.0, "conf_text": conf_text, "p_text": p_text}
    if choice != "no_reply":
        return {"suppress": False, "reason": "not_no_reply", "evidence": "", "value": 0.0,
                "limit": 0.0, "conf_text": conf_text, "p_text": p_text}

    if isinstance(probability, (int, float)):
        limit = float(probability_threshold)
        evidence, value = "p_no_reply", float(probability)
    else:
        limit = float(confidence_fallback_threshold)
        evidence = "conf"
        value = float(confidence) if confidence is not None else -1.0

    return {
        "suppress": value >= limit,
        "reason": "" if value >= limit else "insufficient_evidence",
        "evidence": evidence,
        "value": value,
        "limit": limit,
        "conf_text": conf_text,
        "p_text": p_text,
    }



# ---------------------------------------------------------------------------
# 5. 抑制时的极简替代请求
# ---------------------------------------------------------------------------

def build_gate_skip_item(text: str = "") -> dict[str, Any]:
    """构造门控命中后的极简替代 Item（让 planner 以最小成本结束本轮）。"""

    body = text or (
        "【门控】本轮判定为无需参与：请直接结束本轮思考，"
        "不要调用任何工具，不要输出发言内容。"
    )
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": None,
            "timestamp": datetime.now().isoformat(),
        },
        "parts": [{"type": "text", "text": body}],
    }


# ---------------------------------------------------------------------------
# 6. 多提供商适配（Jev 可能由不同网关提供，请求/响应形状不同）
# ---------------------------------------------------------------------------

#: 支持的接口风格。控制流统一走 evaluate_decision()，适配层只负责"把各家形状归一"。
API_STYLES = ("typesafe", "classifier_dev", "openai_json")

DEFAULT_ENDPOINTS: dict[str, str] = {
    "typesafe": "https://api.typesafe.ai/v1/systemone",
    "classifier_dev": "https://classifier.dev/v1/classify",
    "openai_json": "",
}

#: 各风格的默认鉴权头（header 名, 前缀）。header 名为空表示"不发送鉴权头"。
DEFAULT_AUTH: dict[str, tuple[str, str]] = {
    "typesafe": ("Authorization", "Bearer "),
    "classifier_dev": ("", ""),
    "openai_json": ("Authorization", "Bearer "),
}

#: classifier.dev 风格用到的两个标签（接口会原样回显标签，按等值匹配归一）
CLASSIFIER_DEV_LABELS: tuple[str, str] = (
    "值得现在参与（进入完整思考）",
    "本轮无需参与（保持安静）",
)

OPENAI_JSON_HINT = (
    "只输出一个 JSON 对象，不要任何解释或额外文本，格式："
    '{"choice": "continue 或 no_reply", "confidence": 0 到 1 的小数}'
)


def normalize_style(style: str) -> str:
    """归一化接口风格名；未知值退回 typesafe，保证永不让配置写错导致不工作。"""

    value = str(style or "").strip().lower()
    return value if value in API_STYLES else "typesafe"


def build_request(api_style: str, *, text: str, model: str = "jev-latest",
                  bot_name: str = "") -> dict[str, Any]:
    """按接口风格构造请求体（统一 POST 到配置里的完整 endpoint）。"""

    style = normalize_style(api_style)
    instructions, criteria = build_gate_prompts(bot_name)

    if style == "classifier_dev":
        criteria_text = "\n".join("%s：%s" % (k, v) for k, v in criteria.items())
        return {
            "inputs": [text],
            "labels": list(CLASSIFIER_DEV_LABELS),
            "instructions": instructions + "\n" + criteria_text,
        }

    if style == "openai_json":
        criteria_text = "\n".join("%s：%s" % (k, v) for k, v in criteria.items())
        prompt = "\n".join([
            instructions,
            criteria_text,
            "",
            "以下是最近的聊天内容：",
            text,
            "",
            OPENAI_JSON_HINT,
        ])
        return {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }

    # typesafe（默认）：原生 Choice 原语
    return build_jev_choice_body(
        text, criteria=criteria, instructions=instructions, model=model,
    )


def _extract_openai_text(payload: Any) -> str:
    """从 OpenAI 兼容响应里取正文（兼容 chat.completions 与 responses 两种形状）。"""

    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):   # 多段 content
                    return "".join(
                        str(part.get("text") or "") for part in content if isinstance(part, dict)
                    )
            text = first.get("text")
            if isinstance(text, str):
                return text
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return ""


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ``` 代码块与前后废话）。"""

    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


_CHOICE_ALIASES: dict[str, str] = {
    "continue": "continue", "yes": "continue", "reply": "continue", "1": "continue",
    "no_reply": "no_reply", "no": "no_reply", "skip": "no_reply", "silent": "no_reply", "0": "no_reply",
}


def parse_response(api_style: str, payload: Any) -> tuple[str | None, float | None, dict[str, Any]]:
    """把各家响应归一成 (choice, confidence, probabilities)。"""

    style = normalize_style(api_style)

    if style == "classifier_dev":
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None, None, {}
        first = results[0] if isinstance(results[0], dict) else {}
        label = str(first.get("label") or "").strip()
        if label == CLASSIFIER_DEV_LABELS[0]:
            choice = "continue"
        elif label == CLASSIFIER_DEV_LABELS[1]:
            choice = "no_reply"
        else:
            choice = None
        raw_conf = first.get("confidence")
        confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else None
        probabilities = {}
        scores = first.get("scores")
        if isinstance(scores, dict) and isinstance(scores.get(CLASSIFIER_DEV_LABELS[1]), (int, float)):
            probabilities["no_reply"] = float(scores[CLASSIFIER_DEV_LABELS[1]])
        return choice, confidence, probabilities

    if style == "openai_json":
        parsed = _parse_json_object(_extract_openai_text(payload))
        if not parsed:
            return None, None, {}
        raw_choice = str(parsed.get("choice") or "").strip().lower()
        choice = _CHOICE_ALIASES.get(raw_choice)
        if choice is None:
            for key, mapped in _CHOICE_ALIASES.items():
                if key in raw_choice:
                    choice = mapped
                    break
        raw_conf = parsed.get("confidence")
        if isinstance(raw_conf, str):
            try:
                raw_conf = float(raw_conf)
            except ValueError:
                raw_conf = None
        confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else None
        probabilities = {}
        raw_probs = parsed.get("probabilities")
        if isinstance(raw_probs, dict) and isinstance(raw_probs.get("no_reply"), (int, float)):
            probabilities["no_reply"] = float(raw_probs["no_reply"])
        return choice, confidence, probabilities

    # typesafe（默认）
    choice, confidence = jev_extract_choice(payload)
    probabilities = {}
    if isinstance(payload, dict):
        raw = payload.get("answers", {}).get("timing_gate", {}).get("probabilities")
        if isinstance(raw, dict):
            probabilities = raw
    return choice, confidence, probabilities


def resolve_auth(api_style: str, header: str = "", prefix: str = "") -> tuple[str, str]:
    """把「用户填的鉴权配置」归一成 (header 名, 前缀)。

    语义（空串表示"用该风格的默认值"）：
      · header=""        → 用风格默认头（通常 Authorization）；header="none" → 不发送鉴权头
      · prefix=""        → 用风格默认前缀（通常 "Bearer "）；prefix="none" → 空前缀（直传 key）
    """

    style = normalize_style(api_style)
    default_header, default_prefix = DEFAULT_AUTH.get(style, ("Authorization", "Bearer "))
    raw_header = str(header or "").strip()
    raw_prefix = str(prefix or "").strip()
    if raw_header.lower() == "none":
        header_name = ""
    else:
        header_name = raw_header or default_header
    prefix_value = "" if raw_prefix.lower() == "none" else (raw_prefix or default_prefix)
    return header_name, prefix_value
