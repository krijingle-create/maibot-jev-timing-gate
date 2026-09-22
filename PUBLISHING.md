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
