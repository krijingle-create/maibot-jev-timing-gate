"""Jev 参与门控（timing gate）。

在 `maisaka.planner.before_request` 之前做一次**廉价判断**：用决策模型 Jev 判
「本轮是否值得进入完整 planner」，高置信判「无需参与」时抑制这一轮，省下一次 planner 调用。

**开箱即用**：装好后只需要填两个东西——Jev 的 `endpoint` 与 `api_key`。
机器人昵称（@豁免用）会自动从主程序配置 `bot.nickname` 读取，不需要手填。

**多提供商**：`api_style` 支持三种接口形状，指向任意提供 Jev 的网关都能用：
  · `typesafe`（默认）—— TypeSafe 原生 Choice 原语（`state` + `questions`）
  · `classifier_dev` —— classifier.dev 风格（`inputs` + `labels` + `instructions`）
  · `openai_json` —— 任何 OpenAI 兼容网关，要求模型回一段 JSON

三个关键设计（都有实测依据，见 README 的「设计依据」）：
1. 控制流用 `probabilities["no_reply"]`，**不用 confidence**——前者重复调用的抖动只有一半。
2. @机器人 走**确定性豁免**，且只扫 state 尾部窗口——直接点名时 Jev 基本是瞎的（p≈0.5），
   而扫全窗又会被历史提及污染。
3. 抑制时同时返回 `abort` 与极简改写：核心支持 abort 就真正跳过模型调用（零 token、零延迟），
   不支持则被调度器忽略 abort、自动降级为"极简改写"，绝不会退化成"完全不拦"。
"""

from __future__ import annotations

import asyncio
import json
import os
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from . import gate_core as core
from .config import GateSettings

PRIVATE_KEY_FILE = "jev_config.json"
HOOK_TIMEOUT_MS = 8000
LOG_TAG = "Jev 门控"
ALIAS_RETRY_SECONDS = 60.0
NICKNAME_HINT = "config/bot_config.toml 的 [bot] nickname"


class JevTimingGatePlugin(MaiBotPlugin):
    """按 Jev 判定结果抑制「无需参与」的 planner 轮次。"""

    config_model = GateSettings

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        cfg = self._cfg()
        aliases, source = await self._resolve_aliases(cfg.get("bot_aliases") or [])
        self._alias_cache = (aliases, source)
        self.ctx.logger.info(
            "%s已加载：enabled=%s shadow=%s 风格=%s 端点=%s 概率阈值=%s 别名=%s（来源：%s）窗口=%s字",
            LOG_TAG, cfg.get("enabled"), cfg.get("shadow_mode"), cfg.get("api_style"),
            cfg.get("endpoint"), cfg.get("probability_threshold"),
            aliases or "—", source, cfg.get("mention_window_chars"),
        )
        if cfg.get("enabled") and not cfg.get("api_key"):
            self.ctx.logger.warning("%s：还没填 api_key（插件配置与 %s 都为空）→ 判定一律放行。",
                                    LOG_TAG, PRIVATE_KEY_FILE)
        if cfg.get("enabled") and not aliases:
            self.ctx.logger.warning(
                "%s：拿不到机器人昵称（%s 里也没有）→ 门控**拒绝启用**，"
                "以免在你被 @ 时被静默抑制。可在 [gate] bot_aliases 手动指定。",
                LOG_TAG, NICKNAME_HINT,
            )

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        cfg = self._cfg()
        aliases, source = await self._resolve_aliases(cfg.get("bot_aliases") or [])
        self._alias_cache = (aliases, source)
        self.ctx.logger.info("%s：配置已更新（scope=%s）别名=%s（来源：%s）",
                             LOG_TAG, scope, aliases or "—", source)

    # ------------------------------------------------------------------ 配置
    def _cfg(self) -> dict[str, Any]:
        """读取并归一化配置；任何异常都返回空字典（调用方按"未启用"处理）。"""

        try:
            settings = self.config
            plugin, gate = settings.plugin, settings.gate
        except Exception as exc:  # 配置未注入 / 校验失败都不能拖垮 planner
            self.ctx.logger.debug("%s：读取配置失败（按未启用处理）: %s", LOG_TAG, exc)
            return {}

        style = core.normalize_style(str(plugin.api_style or "typesafe"))
        auth_header, auth_prefix = core.resolve_auth(
            style,
            getattr(plugin, "auth_header", "") or "",
            getattr(plugin, "auth_prefix", "") or "",
        )
        aliases = [str(a).strip() for a in (gate.bot_aliases or []) if str(a).strip()]
        return {
            "enabled": bool(plugin.enabled),
            "shadow_mode": bool(plugin.shadow_mode),
            "api_key": (str(plugin.api_key or "").strip() or self._private_key()),
            "endpoint": str(plugin.endpoint or core.DEFAULT_ENDPOINTS.get(style, "")).strip(),
            "api_style": style,
            "auth_header": auth_header,
            "auth_prefix": auth_prefix,
            "model": str(plugin.model or "jev-latest").strip() or "jev-latest",
            "timeout_sec": float(plugin.timeout_sec or 6.0),
            "state_max_chars": int(plugin.state_max_chars or 3000),
            "gate_enabled": bool(gate.enabled),
            "probability_threshold": float(gate.probability_threshold or 0.80),
            "confidence_fallback_threshold": float(gate.confidence_fallback_threshold or 0.62),
            "bot_aliases": aliases,
            "mention_window_chars": int(gate.mention_window_chars or core.DEFAULT_MENTION_WINDOW_CHARS),
        }

    def _private_key(self) -> str:
        """从插件目录下的 jev_config.json 读 key（便于用 600 权限把密钥隔离在配置界面之外）。"""

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PRIVATE_KEY_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                return str(json.load(fh).get("api_key") or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------ 昵称（@豁免用）
    async def _resolve_aliases(self, configured: list[str]) -> tuple[list[str], str]:
        """确定 @豁免 用的昵称：优先用户手填，否则自动读主程序配置，最后兜底读配置文件。"""

        if configured:
            return configured, "插件配置手填"
        name = ""
        try:
            result = await self.ctx.call_capability("config.get", key="bot.nickname", default="")
            if isinstance(result, dict) and result.get("success"):
                name = str(result.get("value") or "").strip()
            elif isinstance(result, str):
                name = result.strip()
        except Exception as exc:
            self.ctx.logger.debug("%s：config.get(bot.nickname) 失败，改用配置文件兜底: %s", LOG_TAG, exc)
        if name:
            return [name], "自动读主程序配置 bot.nickname"
        fallback = self._nickname_from_file()
        if fallback:
            return [fallback], "自动读 config/bot_config.toml"
        return [], "未获取到"

    @staticmethod
    def _nickname_from_file() -> str:
        """兜底：直接读 `config/bot_config.toml` 的 `[bot] nickname`。"""

        here = Path(__file__).resolve()
        for parent in list(here.parents)[:4]:
            candidate = parent / "config" / "bot_config.toml"
            if not candidate.is_file():
                continue
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                return str((data.get("bot") or {}).get("nickname") or "").strip()
            except Exception:
                return ""
        return ""

    async def _aliases_for_gate(self, cfg: dict[str, Any]) -> list[str]:
        """给门控用的别名：手填优先；否则用缓存，缓存空则限频重试解析。"""

        configured = cfg.get("bot_aliases") or []
        if configured:
            return configured
        cached, _source = getattr(self, "_alias_cache", ([], ""))
        if cached:
            return cached
        now = asyncio.get_running_loop().time()
        if now - getattr(self, "_alias_refresh_ts", 0.0) < ALIAS_RETRY_SECONDS:
            return []
        self._alias_refresh_ts = now
        aliases, source = await self._resolve_aliases([])
        if aliases:
            self._alias_cache = (aliases, source)
        return aliases

    # ------------------------------------------------------------------ Jev 调用
    async def _ask_choice(
        self, text: str, cfg: dict[str, Any], bot_aliases: list[str]
    ) -> tuple[str | None, float | None, dict[str, Any]]:
        """按配置的接口风格询问 Jev，归一成 (choice, confidence, probabilities)。"""

        body = core.build_request(
            cfg["api_style"],
            text=text,
            model=cfg["model"],
            bot_name=bot_aliases[0] if bot_aliases else "",
        )
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = cfg["endpoint"]
        api_key, timeout = cfg["api_key"], cfg["timeout_sec"]
        auth_header, auth_prefix = cfg["auth_header"], cfg["auth_prefix"]

        if not url:
            self.ctx.logger.warning(
                "%s：api_style=%s 需要填 endpoint，当前为空 → 本轮放行", LOG_TAG, cfg["api_style"])
            return None, None, {}

        def _call() -> dict[str, Any]:
            headers = {
                "Content-Type": "application/json",
                # 部分网关（Cloudflare 前置）缺 UA 会直接 403 且不返回 JSON
                "User-Agent": "maibot-jev-timing-gate/1.0",
            }
            if auth_header and api_key:
                headers[auth_header] = f"{auth_prefix}{api_key}"
            request = urllib.request.Request(url, data=payload_bytes, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))

        try:
            payload = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout + 0.5)
        except Exception as exc:
            self.ctx.logger.warning("%s：判定调用失败（已忽略，正常进入 Planner）: %s", LOG_TAG, exc)
            return None, None, {}
        return core.parse_response(cfg["api_style"], payload)

    # ------------------------------------------------------------------ 门控主体
    async def _maybe_gate(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.get("enabled") or not cfg.get("gate_enabled"):
            return {"action": "continue"}
        if not cfg.get("api_key") or not cfg.get("endpoint"):
            return {"action": "continue"}
        aliases = await self._aliases_for_gate(cfg)
        if not aliases:
            # 安全默认：拿不到机器人昵称就不动手，避免"被点名时被静默抑制"这种不可见的坏结果
            return {"action": "continue"}

        items = kwargs.get("items")
        state_text = core.extract_planner_state_text(items, max_chars=cfg["state_max_chars"])
        if not state_text:
            return {"action": "continue"}

        # ---- ① 确定性豁免：只在最新消息（尾部窗口）里找 @机器人 ----
        if core.is_bot_addressed(state_text, aliases, cfg["mention_window_chars"]):
            self.ctx.logger.info("%s：检测到 @机器人，跳过门控，正常进入 Planner", LOG_TAG)
            return {"action": "continue"}

        # ---- ② Jev 判定 ----
        choice, confidence, probabilities = await self._ask_choice(state_text, cfg, aliases)
        decision = core.evaluate_decision(
            choice,
            confidence,
            probabilities,
            probability_threshold=cfg["probability_threshold"],
            confidence_fallback_threshold=cfg["confidence_fallback_threshold"],
        )
        conf_text, p_text = decision["conf_text"], decision["p_text"]

        if decision["reason"] == "no_result":
            return {"action": "continue"}
        if decision["reason"] == "not_no_reply":
            self.ctx.logger.info(
                "%s：%s (conf=%s p_no_reply=%s)，正常进入 Planner",
                LOG_TAG, choice, conf_text, p_text,
            )
            return {"action": "continue"}
        if not decision["suppress"]:
            self.ctx.logger.info(
                "%s：no_reply 但证据不足 (%s=%.2f < %.2f; conf=%s p_no_reply=%s)，正常进入 Planner",
                LOG_TAG, decision["evidence"], decision["value"], decision["limit"], conf_text, p_text,
            )
            return {"action": "continue"}

        # ---- ③ 抑制 ----
        original_items = len(items) if isinstance(items, list) else 0
        original_tools = len(kwargs.get("tool_definitions") or [])
        if cfg.get("shadow_mode"):
            self.ctx.logger.info(
                "%s[影子]：本该抑制（%s=%.2f >= %.2f; conf=%s）items=%s→1 tools=%s→0，本轮不改行为",
                LOG_TAG, decision["evidence"], decision["value"], decision["limit"],
                conf_text, original_items, original_tools,
            )
            return {"action": "continue"}

        kwargs["items"] = [core.build_gate_skip_item()]
        if "tool_definitions" in kwargs:
            kwargs["tool_definitions"] = []
        self.ctx.logger.warning(
            "%s：判定无需参与(%s=%.2f >= %.2f; conf=%s)，已请求中止本轮"
            "（核心支持 abort 则跳过模型调用；否则回落为极简改写） items=%s->1 tools=%s->0",
            LOG_TAG, decision["evidence"], decision["value"], decision["limit"],
            conf_text, original_items, original_tools,
        )
        return {"action": "abort", "modified_kwargs": kwargs}

    # ------------------------------------------------------------------ Hook
    @HookHandler(
        "maisaka.planner.before_request",
        name="jev_timing_gate",
        description="用 Jev 判定本轮是否值得进入 planner；高置信无需参与时抑制该轮。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=HOOK_TIMEOUT_MS,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._maybe_gate(kwargs)
        except Exception as exc:  # 任何意外都必须放行，绝不能因为门控让机器人不吭声
            self.ctx.logger.warning("%s：异常（已忽略，正常进入 Planner）: %s", LOG_TAG, exc)
            return {"action": "continue"}


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""

    return JevTimingGatePlugin()
