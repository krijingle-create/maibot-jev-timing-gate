# Jev 参与门控（jev timing gate）

在 planner 请求发出之前，用决策模型 [Jev](https://typesafe.ai) 判断一次：这一轮群里的新消息，
值不值得花一次完整的 planner？

Jev 提供 Choice / Score / Noul 三种原语，返回带校准置信度的结构化答案，不生成文本。它判「本轮无需参与」
且置信度足够高时，插件抑制这一轮 planner；拿不准、端点故障、被 @ 时一律放行。宁可多花一次，
也不让机器人该说时不说。

---

## 一、它能省什么

下面的量级来自本插件在 MaiBot 1.2.5 上的实测，仅供参考：

- 一个完整 planner 轮次的 prompt 通常几千 token（6k 量级）；被抑制的轮次只剩几十 token：
  只发一条「本轮无需参与」提示，不带选中的历史消息，也不带工具定义。
- 抑制率取决于群里的噪声轮次占比，实测大约在 1/4 到 1/3。
- Jev 每次判定约 1 到 2k token，与送去的文本长度成正比。

开销和收益量级接近，近似打平：每轮都要付一次 Jev 判定，只有一部分轮次能省下 planner。
实际收益主要在两处：

1. 明确的噪声轮次（别的 bot 的指令刷屏、纯灌水）不再走完整规划；
2. 抑制会推进 MaiBot 原生的空闲退避：连续空闲触发指数退避（15 秒起、最多 300 秒），
   退避窗口内的新消息不会触发 planner。

这些数字不能直接套到你的实例：planner 的 prompt 大小、群里的噪声比例、空闲退避配置都会让结果差很多。
想看清自己这儿的量级，先把 `shadow_mode` 设成 `true` 只做观察，再按日志估算。

---

## 二、安装（开箱即用：只需要填两个字段）

先把插件放进 `MaiBot/plugins/`，目录名用 `local_jev-timing-gate`（与 manifest 的 id 对应）。
在 MaiBot 根目录下执行：

```bash
# git clone：之后可以直接 git pull 更新
git clone https://github.com/krijingle-create/maibot-jev-timing-gate.git plugins/local_jev-timing-gate

# 或者下载 ZIP：仓库页 Download ZIP，解压后把 maibot-jev-timing-gate-main
# 改名成 local_jev-timing-gate，整个目录放进 plugins/
```

目录名要写在 clone 命令末尾，直接 `git clone <地址>` 会建成 `maibot-jev-timing-gate`。

1. 首次加载后，Runner 会依据 `config.py` 里的 `config_model` 自动生成 `config.toml`，
   并在 WebUI 的插件设置页渲染出来。本包附带的 `config.example.toml` 只是一份带注释的参考。
2. 在设置页里选一个模型提供商模板（见下表），填上 `api_key`。`endpoint` 留空就用所选模板的默认地址，
   TypeSafe 和 classifier.dev 都不用填。
3. 想先观察：打开影子模式，它只记录本该抑制的轮次，不改行为。

### 更新

```bash
cd plugins/local_jev-timing-gate && git pull
```

WebUI 插件页的更新按钮两条路径都认：目录里有 `.git` 就 `git pull`，没有就按 manifest 的仓库地址重新克隆一份。
两者都会保留你填好的 `config.toml`；用 `jev_config.json` 存 key 的话只有前一条会连它一起保住，
走重新克隆前记得先把那个文件备份出来。

### 模型提供商模板（设置页下拉）

| 模板 | `endpoint` | `api_key` | 备注 |
|---|---|---|---|
| typesafe（默认） | 留空即官方直连地址 | 必填 | TypeSafe 官方端点；其它 TypeSafe 兼容网关也可以直接填自己的完整 URL |
| classifier_dev | 留空即默认地址 | 不用填 | 免费转发站；文本会经该站转发给 TypeSafe |
| openai_json | 必填（如 `https://host/v1/chat/completions`） | 按需 | 任意 OpenAI 兼容网关，要求模型严格回 `{"choice":..., "confidence":...}` |

鉴权默认按模板走，一般是 `Authorization: Bearer <key>`。要对接自定义鉴权的网关，用 `鉴权头名`
和 `鉴权前缀` 覆盖。两者都可以填 `none` 表示不发送该部分，例如头名填 `x-api-key`、前缀填 `none`，
就是直接放裸 key。

实测（2026-09-22）：同一批样本下 `classifier_dev` 与 `typesafe` 给出的判断方向一致。别人家 bot
的指令刷屏判 `no_reply`，直接点名机器人判 `continue`。

### 配置速查

```toml
[plugin]
enabled = true           # 总开关。默认开：模板 + key 填好即生效
shadow_mode = false      # true = 只记录"本该抑制"，不改行为
api_style = "typesafe"   # typesafe / classifier_dev / openai_json（设置页里是下拉）
endpoint = ""            # 留空按模板取默认；openai_json 必填
auth_header = ""         # 留空按模板取默认；"none" = 不发鉴权头
auth_prefix = ""         # 留空按模板取默认；"none" = 不带前缀直传 key
api_key = ""             # 填这里即可；也可放插件目录 jev_config.json（600 权限）隐藏密钥
model = "jev-latest"     # 实测 jev-1.13.0 也可用；jev-fast / jev 会返回 400
timeout_sec = 6.0        # 超时即放行；必须小于 hook 超时 8 秒
state_max_chars = 3000   # 判定用的聊天文本上限（取最新内容）

[gate]
enabled = true
probability_threshold = 0.8           # probabilities["no_reply"] ≥ 该值才抑制
confidence_fallback_threshold = 0.62  # 端点没返回 probabilities 时的回退阈值
bot_aliases = []                      # 留空 = 自动读 bot.nickname（推荐）
mention_window_chars = 600            # @提及的扫描窗口（只看最新内容）
```

### 怎么确认它在工作

```bash
grep -a 'Jev 门控' logs/nohup.log | tail -20
```

会看到四类行，对应四个分支：

```
Jev 门控：检测到 @机器人，跳过门控，正常进入 Planner          ← 豁免，不调用 Jev
Jev 门控：continue (conf=0.44 p_no_reply=0.28)，正常进入 Planner
Jev 门控：no_reply 但证据不足 (p_no_reply=0.61 < 0.80 …)   ← 拿不准就放行
Jev 门控：判定无需参与(p_no_reply=0.95 >= 0.80 …)，本轮已改写成极简请求
```

影子模式下最后一行会变成 `Jev 门控[影子]：本该抑制（…），本轮不改行为`。

---

## 三、边界与注意事项

需要 Maisaka 架构（`src/maisaka/`），也就是 dev / main / neo-mai 分支。`classical` 老架构没有这些
hook，装不上。

不要和同类门控同时开。若你另外还装了一个内置 Jev 门控的插件，两者同时启用会让每轮调用两次 Jev，
两套抑制逻辑同时生效，请二选一。

抑制轮不是零成本：它仍要发一次模型请求，只是内容被压成一条提示，几十 token。

判断会把真实群聊文本发送到你配置的端点。实测把昵称和群名片匿名化后判断会明显跑偏：`@昵称` 换成
`@我` 后，被点名的消息反而被判成不用回，置信度 0.84。因此插件按原样发送文本。如果群里的人在意隐私，
请不要启用。

Jev 只做判断，不生成文本，不能替 planner 或 replyer 写任何东西。

任何失败都走放行：网络错误、超时、返回结构异常、配置读不到，一律正常进入 Planner，
不会让门控把机器人变成哑巴。

---

## 四、文件说明

| 文件 | 作用 |
|---|---|
| `plugin.py` | 插件主体：hook 注册、Jev 调用、门控判定 |
| `gate_core.py` | 纯逻辑（状态提取 / @豁免窗口 / 判定 / 多提供商适配），不依赖 SDK，可离线单测 |
| `config.py` / `config.example.toml` | 配置模型（含设置页标签与下拉）与带注释的参考 |
| `tests/test_gate_core.py` | 65 条离线用例（`python tests/test_gate_core.py`） |
| `LICENSE` | 许可证（MIT） |
