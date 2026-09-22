"""Jev 参与门控插件配置模型。

MaiBot 插件配置系统要求：顶层模型作为 ``config_model``，各分区用
``PluginConfigBase`` 子模型 + ``Field(default_factory=...)``。
运行时用 ``self.config.plugin.xxx`` / ``self.config.gate.xxx`` 读取。

UI 约定：`Literal[...]` 字段会被宿主渲染成**下拉选择**（`_extract_select_choices`），
`json_schema_extra` 支持 `label` / `hint` / `placeholder` / `disabled` / `hidden` 等键。
"""

from typing import ClassVar, List, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

#: 配置 schema 版本。MaiBot 的插件配置系统要求 `[plugin] config_version` 存在，
#: 缺失会被判为"无版本配置"而拒绝加载。改动配置字段结构时递增。
CONFIG_SCHEMA_VERSION = "1.0.0"

#: 三种接口模板。选哪个决定请求/响应形状，也决定 endpoint 的默认地址。
API_STYLE_CHOICES = ("typesafe", "classifier_dev", "openai_json")


class PluginOptions(PluginConfigBase):
    """连接与运行方式。"""

    # WebUI 展示约定（SDK 的 PluginConfigBase 读取这些 ClassVar）
    __ui_label__: ClassVar[str] = "连接与运行"
    __ui_icon__: ClassVar[str] = "settings"
    __ui_order__: ClassVar[int] = 0

    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )
    enabled: bool = Field(
        default=True,
        description="总开关。默认开：填好 endpoint + api_key 即可生效；想先只观察就把 shadow_mode 设为 true。",
        json_schema_extra={"label": "启用门控", "hint": "关掉它插件完全不动作"},
    )
    shadow_mode: bool = Field(
        default=False,
        description="影子模式：只记录判断结果，不修改 planner 请求。用来评估阈值是否合适。",
        json_schema_extra={"label": "影子模式", "hint": "只记录\"本该抑制\"，不改行为"},
    )
    api_style: Literal["typesafe", "classifier_dev", "openai_json"] = Field(
        default="typesafe",
        description=(
            "模型提供商接口模板（下拉选择）：\n"
            "· typesafe —— TypeSafe 直连 / 其它 TypeSafe 兼容网关（原生 Choice 原语）\n"
            "· classifier_dev —— classifier.dev 风格（免费转发站，无需 key）\n"
            "· openai_json —— 任意 OpenAI 兼容网关（要求模型回 JSON）\n"
            "选定后 endpoint 留空即用该模板的默认地址；填了无效值会自动退回 typesafe。"
        ),
        json_schema_extra={"label": "模型提供商模板", "hint": "决定请求/响应形状与默认端点"},
    )
    endpoint: str = Field(
        default="",
        description=(
            "Jev 端点的**完整 URL**。留空时按上面的模板取默认值：\n"
            "typesafe → https://api.typesafe.ai/v1/systemone\n"
            "classifier_dev → https://classifier.dev/v1/classify\n"
            "openai_json → 无默认值，必须自己填（例如 https://你的网关/v1/chat/completions）"
        ),
        json_schema_extra={"label": "端点", "hint": "留空 = 用所选模板的默认地址", "placeholder": "留空即用模板默认"},
    )
    auth_header: str = Field(
        default="",
        description=(
            "鉴权头名字。留空 = 按模板取默认（通常 Authorization）；填 none = 不发送鉴权头；"
            "也可填 x-api-key 等适配其它网关。"
        ),
        json_schema_extra={"label": "鉴权头名", "placeholder": "留空=默认 / none=不发"},
    )
    auth_prefix: str = Field(
        default="",
        description='鉴权前缀。留空 = 按模板取默认（通常 "Bearer "）；填 none = 不带前缀直接放 key。',
        json_schema_extra={"label": "鉴权前缀", "placeholder": "留空=默认 / none=不带前缀"},
    )
    api_key: str = Field(
        default="",
        description=(
            "访问上面端点的 key。留空时会尝试读取插件目录下的 jev_config.json"
            "（内容 {\"api_key\": \"...\"}），适合想把密钥从 WebUI 配置里藏起来的场景。"
        ),
        json_schema_extra={"label": "API Key", "hint": "也可放插件目录 jev_config.json（600 权限）"},
    )
    model: str = Field(
        default="jev-latest",
        description="模型名。实测可用的还有 jev-1.13.0；jev-fast / jev 会返回 400。",
        json_schema_extra={"label": "模型名"},
    )
    timeout_sec: float = Field(
        default=6.0,
        description="单次判定超时（秒）。超时即放行，不会阻塞聊天。必须小于插件的 hook 超时 8 秒。",
        json_schema_extra={"label": "判定超时（秒）"},
    )
    state_max_chars: int = Field(
        default=3000,
        description="送给 Jev 的聊天文本上限（取最新内容）。调小可省钱，但实测砍到 1000 字符以下判断会明显跑偏。",
        json_schema_extra={"label": "判定文本上限（字符）"},
    )

    @field_validator("api_style", mode="before")
    @classmethod
    def _coerce_api_style(cls, value: object) -> str:
        """把未知模板名归一成 typesafe：避免手改配置写错导致整个配置校验失败。"""

        text = str(value or "").strip().lower()
        return text if text in API_STYLE_CHOICES else "typesafe"


class GateOptions(PluginConfigBase):
    """门控判定参数。"""

    __ui_label__: ClassVar[str] = "门控判定"
    __ui_icon__: ClassVar[str] = "tune"
    __ui_order__: ClassVar[int] = 1

    enabled: bool = Field(
        default=True,
        description="门控判定开关（在 plugin.enabled 打开的前提下生效）。",
        json_schema_extra={"label": "启用判定"},
    )
    probability_threshold: float = Field(
        default=0.80,
        description=(
            "正式阈值：probabilities[\"no_reply\"] ≥ 该值才抑制本轮。"
            "实测该字段的抖动约为 confidence 的一半，所以控制流用它。0.80 ≈ 旧口径 confidence 0.58。"
        ),
        json_schema_extra={"label": "抑制阈值（概率）", "hint": "调低更省但更容易误抑制"},
    )
    confidence_fallback_threshold: float = Field(
        default=0.62,
        description="端点未返回 probabilities 时的回退阈值（用 confidence）。0.5 是模型边界，+0.12 是实测抖动余量。",
        json_schema_extra={"label": "回退阈值（confidence）"},
    )
    bot_aliases: List[str] = Field(
        default_factory=list,
        description=(
            "机器人昵称/群名片前缀，用于「被 @ 时不抑制」的确定性豁免。"
            "**留空即自动**：插件通过官方配置能力读主程序的 bot.nickname。"
            "只有读不到、或你在群里用了别的群名片时，才需要在这里手填（例如 [\"小助手\"]）。"
        ),
        json_schema_extra={"label": "@豁免别名", "hint": "留空 = 自动读 bot.nickname"},
    )
    mention_window_chars: int = Field(
        default=600,
        description=(
            "只在 state 尾部这么长的范围内找 @提及。**别改大**：planner 上下文里长期残留"
            "历史消息、模型自己的推理、上一轮工具参数中的 @字样，扫全窗会造成大量假豁免。"
        ),
        json_schema_extra={"label": "@提及扫描窗口（字符）", "hint": "别改大，容易撞上历史提及残留"},
    )


class GateSettings(PluginConfigBase):
    """插件顶层配置。"""

    __ui_label__: ClassVar[str] = "Jev 参与门控"
    __ui_icon__: ClassVar[str] = "psychology"
    __ui_order__: ClassVar[int] = 0

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    gate: GateOptions = Field(default_factory=GateOptions)
