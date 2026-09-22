"""gate_core 的离线单测（不需要 MaiBot SDK、不联网）。

直接跑：python tests/test_gate_core.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate_core import (  # noqa: E402
    CLASSIFIER_DEV_LABELS,
    DEFAULT_MENTION_WINDOW_CHARS,
    build_gate_skip_item,
    build_request,
    evaluate_decision,
    extract_planner_state_text,
    is_bot_addressed,
    jev_extract_choice,
    jev_extract_probability,
    normalize_style,
    parse_response,
    resolve_auth,
)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)
    print("  %s %-56s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want))


# ---------------------------------------------------------------- 1. 状态文本提取
def test_extract():
    print("\n[1] extract_planner_state_text")
    items = [
        {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "系统提示，应被跳过"}]},
        {"item_type": "UserMessageItem", "parts": [
            {"type": "text", "text": '<message msg_id="1" time="10:00:00" user="甲">\n'},
            {"type": "text", "text": "你好"},
        ]},
        {"item_type": "UserMessageItem", "parts": [
            {"type": "text", "text": "【人物画像-内部参考】\n以下内容仅供内部推理"},
        ]},
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "时间：2026-09-22 10:00:00"}]},
        {"item_type": "ReasoningItem", "text_parts": ["模型自己的推理，无 parts 字段"]},
        {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "最近一句聊天"}]},
    ]
    state = extract_planner_state_text(items)
    check("保留聊天、丢弃系统/框架/时间", "你好" in state and "最近一句聊天" in state, True)
    check("丢弃 SystemMessageItem", "系统提示" in state, False)
    check("丢弃框架文本", "人物画像" in state, False)
    check("丢弃『时间：』开头", "时间：2026" in state, False)
    check("ReasoningItem（无 parts）不进 state", "模型自己的推理" in state, False)
    check("max_chars 截断取尾部", extract_planner_state_text(items, max_chars=5), state[-5:])


# ---------------------------------------------------------------- 2. @豁免窗口
def test_mention_window():
    print("\n[2] is_bot_addressed（只扫尾部窗口）")
    aliases = ["小助手"]
    filler = "闲聊" * 100                                     # 200 字符
    check("最新一条 @小助手🌸 → 命中",
          is_bot_addressed(filler + "\n@小助手🌸 在吗", aliases), True)
    check("群名片带后缀 @小助手Bot → 命中",
          is_bot_addressed(filler + "\n@小助手Bot 在吗", aliases), True)
    check("@幽幽子 → 不命中", is_bot_addressed(filler + "\n@幽幽子 /今日运势", aliases), False)
    check("邮箱 a@小助手.com → 不命中", is_bot_addressed(filler + "\n邮箱 a@小助手.com", aliases), False)
    check("@小助先生 → 不命中", is_bot_addressed(filler + "\n@小助先生 你好", aliases), False)

    # 核心回归：老 @ 在窗口之外时不应命中（否则门控会长期失效）
    stale = "@小助手🌸 想你了\n" + "闲聊" * 300 + "\n@幽幽子 /今日运势"
    check("历史 @ 远在窗口外 → 不命中（关键回归）", is_bot_addressed(stale, aliases), False)
    check("历史 @ 远在窗口外（扫全窗则命中）",
          is_bot_addressed(stale, aliases, 0), True)
    check("默认窗口 = 600", DEFAULT_MENTION_WINDOW_CHARS, 600)
    check("空文本 → 不命中", is_bot_addressed("", aliases), False)
    check("无别名配置 → 不命中", is_bot_addressed(filler + "\n@小助手🌸 在吗", []), False)


# ---------------------------------------------------------------- 3. 响应解析
def test_parse():
    print("\n[3] jev_extract_choice / jev_extract_probability")
    payload = {"answers": {"timing_gate": {
        "type": "choice", "choice": "no_reply", "confidence": 0.68,
        "probabilities": {"continue": 0.16, "no_reply": 0.84}}}}
    check("choice", jev_extract_choice(payload)[0], "no_reply")
    check("confidence", jev_extract_choice(payload)[1], 0.68)
    check("p(no_reply)", jev_extract_probability(payload, "no_reply"), 0.84)
    check("缺字段 → None", jev_extract_choice({"answers": {}}), (None, None))
    check("probabilities 缺失 → None", jev_extract_probability({"answers": {"timing_gate": {}}}, "no_reply"), None)


# ---------------------------------------------------------------- 4. 判定表
def test_decision():
    print("\n[4] evaluate_decision（阈值 0.80 / 回退 0.62）")
    P = {"probability_threshold": 0.80, "confidence_fallback_threshold": 0.62}

    d = evaluate_decision("no_reply", 0.68, {"no_reply": 0.84}, **P)
    check("p=0.84 → 抑制", d["suppress"], True)
    check("依据字段 = p_no_reply", d["evidence"], "p_no_reply")

    check("p=0.79（贴线以下）→ 放行", evaluate_decision("no_reply", 0.66, {"no_reply": 0.79}, **P)["suppress"], False)
    check("p=0.80（边界含）→ 抑制", evaluate_decision("no_reply", 0.66, {"no_reply": 0.80}, **P)["suppress"], True)
    check("直接点名那种 p=0.50 → 放行", evaluate_decision("no_reply", 0.62, {"no_reply": 0.50}, **P)["suppress"], False)
    check("choice=continue → 放行", evaluate_decision("continue", 0.95, {"no_reply": 0.05}, **P)["suppress"], False)
    check("调用失败 choice=None → 放行", evaluate_decision(None, None, {}, **P)["suppress"], False)
    check("调用失败 reason 标记", evaluate_decision(None, None, {}, **P)["reason"], "no_result")

    # 回退路径
    d2 = evaluate_decision("no_reply", 0.70, {}, **P)
    check("缺 probabilities + conf=0.70 → 抑制", d2["suppress"], True)
    check("回退依据字段 = conf", d2["evidence"], "conf")
    check("缺 probabilities + conf=0.55 → 放行", evaluate_decision("no_reply", 0.55, {}, **P)["suppress"], False)
    check("缺 probabilities + conf=None → 放行", evaluate_decision("no_reply", None, {}, **P)["suppress"], False)
    check("probabilities 非字典 → 回退", evaluate_decision("no_reply", 0.70, None, **P)["evidence"], "conf")


# ---------------------------------------------------------------- 5. 极简请求
def test_skip_item():
    print("\n[5] build_gate_skip_item")
    item = build_gate_skip_item()
    check("item_type", item["item_type"], "UserMessageItem")
    check("是 text part", item["parts"][0]["type"], "text")
    check("有 meta.item_id", bool(item["meta"]["item_id"]), True)
    check("自定义文本生效", build_gate_skip_item("（自定义）")["parts"][0]["text"], "（自定义）")
    check("默认文案前缀中性（无插件专有前缀）", item["parts"][0]["text"].startswith("【门控】"), True)


# ---------------------------------------------------------------- 6. 多提供商适配
def test_adapters():
    print()
    print("[6] 多提供商适配（请求构造 / 响应归一 / 鉴权归一）")
    text = "12:00:00[msg_id:1][甲]@小助手 在吗"

    check("未知风格退回 typesafe", normalize_style("whatever"), "typesafe")
    check("风格名大小写归一", normalize_style("CLASSIFIER_DEV"), "classifier_dev")

    check("typesafe 默认鉴权", resolve_auth("typesafe"), ("Authorization", "Bearer "))
    check("classifier_dev 无鉴权", resolve_auth("classifier_dev"), ("", ""))
    check("自定义鉴权头 + 无前缀", resolve_auth("typesafe", "x-api-key", "none"), ("x-api-key", ""))
    check("header=none 不发鉴权", resolve_auth("typesafe", "none"), ("", "Bearer "))

    body = build_request("typesafe", text=text, model="jev-1.13.0", bot_name="小助手")
    check("typesafe: 有 state", body.get("state"), text)
    check("typesafe: 有 questions", "timing_gate" in (body.get("questions") or {}), True)
    check("typesafe: 模型名透传", body.get("model"), "jev-1.13.0")
    check("提示词按昵称模板化（instruction）",
          "小助手" in body["questions"]["timing_gate"]["instructions"], True)
    check("提示词按昵称模板化（criteria）",
          "小助手" in body["questions"]["timing_gate"]["criteria"]["continue"], True)

    body2 = build_request("classifier_dev", text=text, bot_name="小助手")
    check("classifier_dev: inputs", body2.get("inputs"), [text])
    check("classifier_dev: labels", body2.get("labels"), list(CLASSIFIER_DEV_LABELS))
    check("classifier_dev: instructions 提到昵称", "小助手" in body2.get("instructions", ""), True)

    body3 = build_request("openai_json", text=text, model="jev-latest", bot_name="小助手")
    check("openai_json: 单条 user 消息", len(body3["messages"]), 1)
    check("openai_json: 正文含 JSON 要求", '"choice"' in body3["messages"][0]["content"], True)

    payload_ts = {"answers": {"timing_gate": {"choice": "no_reply", "confidence": 0.68,
                                             "probabilities": {"continue": 0.16, "no_reply": 0.84}}}}
    choice, conf, probs = parse_response("typesafe", payload_ts)
    check("typesafe 解析 choice", choice, "no_reply")
    check("typesafe 解析 probabilities", probs.get("no_reply"), 0.84)

    payload_cd_no = {"results": [{"label": CLASSIFIER_DEV_LABELS[1], "confidence": 0.9,
                                 "scores": {CLASSIFIER_DEV_LABELS[1]: 0.93}}]}
    choice, conf, probs = parse_response("classifier_dev", payload_cd_no)
    check("classifier_dev 解析 no_reply", choice, "no_reply")
    check("classifier_dev scores → probabilities", probs.get("no_reply"), 0.93)

    payload_cd_yes = {"results": [{"label": CLASSIFIER_DEV_LABELS[0], "confidence": 0.8}]}
    check("classifier_dev 解析 continue", parse_response("classifier_dev", payload_cd_yes)[0], "continue")
    check("classifier_dev 无 scores → 空概率", parse_response("classifier_dev", payload_cd_yes)[2], {})

    fence = "`" * 3
    fenced_json = fence + "json\n" + '{"choice":"no_reply","confidence":"0.82"}' + "\n" + fence
    payload_oj = {"choices": [{"message": {"content": fenced_json}}]}
    choice, conf, _ = parse_response("openai_json", payload_oj)
    check("openai_json 解析（含代码块/字符串数字）", (choice, conf), ("no_reply", 0.82))
    check("openai_json 容忍别名 yes/no",
          parse_response("openai_json", {"choices": [{"message": {"content": '{"choice":"yes"}'}}]})[0], "continue")
    check("openai_json 垃圾输出 → None",
          parse_response("openai_json", {"choices": [{"message": {"content": "我不知道"}}]}), (None, None, {}))

    # 归一后接判定：classifier_dev 无 probabilities → 走 confidence 回退阈值
    d = evaluate_decision(*parse_response("classifier_dev", payload_cd_no),
                          probability_threshold=0.80, confidence_fallback_threshold=0.62)
    check("classifier_dev 走回退阈值判定", (d["suppress"], d["evidence"]), (True, "p_no_reply"))


if __name__ == "__main__":
    test_extract()
    test_mention_window()
    test_parse()
    test_decision()
    test_skip_item()
    test_adapters()
    print("\n通过 %d / 失败 %d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
