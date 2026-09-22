# 发布说明（面向本插件的维护者，不是给使用者看的）

README 面向使用者；这一份放仓库维护相关的内容。

## 一、发布到官方插件中心

文档：<https://docs.mai-mai.org/plugin/submission> · 插件中心 <https://plugins.maibot.chat/>
· 索引仓库 [Mai-with-u/plugin-repo](https://github.com/Mai-with-u/plugin-repo)

**仓库要求**（根目录必须有以下文件）：

| 文件 | 要求 | 本包状态 |
|---|---|---|
| `_manifest.json` | manifest v2，字段规范见文档 | ✅ 已符合（含 `changelog`，SDK 模型里已声明该字段） |
| `plugin.py` | 含 `create_plugin()` 工厂函数 | ✅ |
| `LICENSE` | 许可证类型需与 manifest 的 `license` 一致 | ✅ MIT / MIT（版权人待替换） |
| `README.md` | 建议含功能介绍、安装方式、配置说明、示例 | ✅ |

提交方式：在 plugin-repo 开一个 **「Add Plugin / 添加插件」Issue**（填插件 ID + 公开仓库 HTTPS 地址），
CI 会自动读取你仓库的 `_manifest.json` 校验，结果评论在 Issue 里；通过后维护者 `/approve` 收录。

> `urls.repository` 是插件中心定位你仓库的依据，必须是**真实公开的 GitHub 地址**（见下节）。

## 二、发布前要改的东西（已完成 ✅）

已改为真实地址：`author.url` = `https://github.com/krijingle-create`，`urls.*` 指向 `https://github.com/krijingle-create/maibot-jev-timing-gate`，
`LICENSE` 版权人 = `krijingle-create`。

保留备忘：

1. 改配置字段结构时记得递增 `config.py` 里的 `CONFIG_SCHEMA_VERSION`（当前 1.0.0）与
   `_manifest.json` 的 `version`。

## 三、开发约定

- `gate_core.py` 是**生成物**：源头是仓库外的 `gen_gate_core.py`（从生产实现里用 AST 抽取 + 脱敏 + 拼装
  多提供商适配层）。要改逻辑先改生成器再重跑，别直接改生成结果。
- 改动后请跑离线用例：`python tests/test_gate_core.py`（当前 65 条）。
- `_manifest.json` 是 `extra="forbid"` 严格模式：**用到的字段必须在 SDK 的 manifest 模型里声明过**；
  用到的能力必须写进 `capabilities`（宿主按能力令牌授权，未声明会被拒）。
- 提交前自检：`config.toml` 不要入库（已在 `.gitignore`）；`jev_config.json`（若用于存 key）也不要入库。

## 四、审核回应记录

### 2026-09-22 核心补丁与宿主文件直读（1.0.0 → 1.0.1）

审核要求：插件包不要附带改动宿主核心的手段；去掉绕过 `config.get`、直读宿主配置文件的兜底。处理如下。

1. 删除 `core_patch/apply_gate_abort_patch.py`（会改写宿主 `src/maisaka/chat_loop_service.py` 的
   abort 补丁，靠字符串锚点维持）。官方 Hook 表把 `maisaka.planner.before_request` 标为
   「允许 abort ❌ · 允许改参 ✅」，插件改为只走「改参」这一条路：把本轮 items 换成一条跳过提示、
   清空 `tool_definitions`，返回 `{"action": "continue", "modified_kwargs": ...}`。
   要做到抑制轮零 token，需要宿主开放该 hook 的 abort，本插件不再自行争取。
   脚本仍留在 git 历史里：`git show 21e9f99:core_patch/apply_gate_abort_patch.py`。
2. 删除 `plugin.py` 的 `_nickname_from_file()`（反向遍历父目录直读 `config/bot_config.toml`）。
   昵称现在只通过 `config.get` 能力读；读不到就拒绝启用门控，并在日志里要求手填 `[gate] bot_aliases`。
   这条能力路径在真实环境验证过（加载日志 `别名=['花子']（来源：自动读主程序配置 bot.nickname）`）。

配置字段没有变化，`CONFIG_SCHEMA_VERSION` 保持 1.0.0；只递增了 `_manifest.json` 的 `version`。
对使用者的行为影响：抑制轮不再可能零 token，固定走原有的「极简改写」路径；判定逻辑与阈值不变。
