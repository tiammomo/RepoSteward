# 用 RepoSteward 辅助已有 Coding Agent

RepoSteward 负责把项目、任务目标、未完成事项、决定与验证证据连接起来。日常编码继续使用自己的 Codex、Claude Code 或 Copilot；完成一段工作后留下检查点，换客户端时重新读取当前任务。

## 关联已有项目

先按项目自己的方式 clone，再在 RepoSteward 的用户配置添加对应仓库的 maintainer 策略。`project link` 只建立本地关联，不 clone、不改动目标代码。相同 remote 的多个 worktree 属于同一个项目，但各有独立的工作区绑定。

```sh
reposteward repo add owner/project --mode maintainer
reposteward project link /absolute/path/project
reposteward project inspect /absolute/path/project
reposteward project list
```

开发任务必须来自已审阅的开放 Issue，并在 feature branch 上开始。`task start` 会重新检查 Issue、贡献策略及本地 origin 基线；需要在开始前按项目方式 fetch。

```sh
reposteward task start /absolute/path/project --issue 123 --reviewed-by YOUR_GITHUB_LOGIN
reposteward task current /absolute/path/project
reposteward task inspect RUN_ID --live
reposteward task context RUN_ID --budget 24000 --scope-path src/module.py
```

上下文中的 contract 保留完整源要求，或使用显式审阅的精简契约；超出预算的强制要求会报错。未完成事项和当前决定来自台账。可选描述、经验和偏好被裁减时，coverage 给出遗漏及取回线索。Agent 所称“已完成”和“测试通过”始终是待核验声明。

## 文件与 MCP 接入

文件与 CLI 接续可独立使用。MCP 入口需要安装包含该扩展的 RepoSteward 版本；若当前版本未提供 `mcp` 命令，请先使用文件/CLI 方式。下方的 MCP 实机记录来自已验证的完整集成版本。

`integration plan` 先生成可审阅 diff 和摘要，`apply` 必须使用该摘要。接入保留原有 AGENTS.md、CLAUDE.md 与 Copilot 指令；撤销只移除自己管理的片段。普通文件不会保存机器路径或认证信息。

MCP 是可选依赖：在运行 RepoSteward 的 Python 环境安装 `reposteward[mcp]`。`mcp config PATH --client codex|claude-code|copilot-vscode` 输出本机配置预览，不自动写客户端配置。服务每次只绑定一个具体工作区，提供 project、context、evidence、checkpoint、verification 五类工具。MCP 服务启动后持有当时的用户配置；修改策略或验证 profile 后应重启服务。

Codex 可以通过用户 `config.toml` 配置 STDIO MCP；Claude Code 可使用临时配置文件及 `--mcp-config`；VS Code 使用用户 MCP 设置。命令路径和本地配置保存在用户目录。[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Claude Code MCP](https://code.claude.com/docs/en/mcp)。

如果客户端没有 MCP，使用 `task current` 的 CLI 输出和受管理指令文件继续工作；切换前保存 checkpoint。不要将旧会话文字当成当前验证结果。旧包晚导入不会覆盖较新的本地外部任务检查点。

## 验证、知识与多项目视图

在用户拥有的配置中设置 `[[verification_profiles]]`，列出 repository、name、commands 和 bootstrap_commands。Agent 只能选择已有 profile；不能通过仓库文件临时指定任意验证命令。验证在隔离副本和加固容器中执行；开发目录有未提交代码也可验证，但结果只对应当时 revision、代码快照、基线和策略。编辑代码后，旧结果作为历史保留，并明确不再适用于当前快照。

通过验证的开发任务仍需整理为干净提交，再走 adopt、精确本地审阅和独立 submit。MCP 不提供发布与合并工具。

可复用经验先 propose 为 candidate，再由维护者 promote；记录适用路径、来源和审阅理由。即使有测试证据，也不把任意经验断言当成已被测试证明。只有调用 context 时显式指定路径，才选取适用经验；代码或证据变化时过期条目不进入建议。

```sh
reposteward overview show --format text
reposteward overview refresh --format text
```

show 默认仅查看本地事实。refresh 显式拉取 GitHub，缓存有时间与错误状态；一个项目刷新失败不妨碍查看其他项目。未处理反馈、未知验证与新增阻塞持续可见。

## 评测与适用范围

现有 `benchmark run` 增加七个固定 golden 场景：长正文末尾要求、超预算反馈、决定替代、旧包晚导入、dirty 快照、过期证据和中断重试。预期事实位于 `handoff-gold-v1.json`，报告标明这是离线控制面评测。注入的 verifier 仅测试证据失效语义，不是实际测试通过的证明。

在加固验证容器内运行 benchmark，输出 JSON 可沿用 #90 的独立 CI 归档流程。本变更不修改 workflow，不启动模型、联网或发布。报告 schema 继续为 v1，benchmark_version 为 0.2.0，原有场景 ID 保持兼容。

真实客户端试点单独记录版本、同一任务 ID、读取事实、检查点与实际验证。人工接续分钟、重复解释和返工若没有人工观测，标为未知。一次任务不能支持百分比效率提升结论。

## 2026-09-05 实际能力表

| 入口 | 实现与验证 | 实际限制 |
| --- | --- | --- |
| 项目关联、任务 CLI | 容器回归覆盖，RepoSteward #104 已实际创建任务 | 开发任务仍需已审阅开放 GitHub Issue 与 feature branch |
| AGENTS / CLAUDE / Copilot 文件 | 管理片段、冲突、恢复与撤销已通过容器测试 | 本轮未实测各客户端的文件自动加载优先级 |
| MCP 服务 | 五类工具、真实 STDIO、官方 SDK、新旧协议及取消已通过容器测试 | 本地工作区范围；配置修改后重启 |
| Codex CLI 0.153.0 | 真实模型通过 MCP 读取原始 Issue，保存并回读 revision 1 → 2 | 此次证明上下文接续；未让客户端编码或执行测试 |
| Claude Code 2.1.81 | 已启动真实客户端；服务返回 429 配额耗尽 | 双客户端接续尚未完成，恢复现有服务额度后重试 |
| Copilot | 文件适配及 VS Code MCP 配置预览可用 | 本机未安装 CLI/扩展，实机未验证 |
| HarnessRunner | 既有 Codex CLI/SDK 路径保留并回归 | Claude/Copilot 托管适配器为条件阶段，未实现 |

#105 hooks 的实施条件是手动检查点有明显遗漏；本轮 Codex 保存与回读成功，尚无该证据。#106/#107 托管 Runner 还需要实际委派需求、客户端能力与收益数据。本轮保留这些开放 Issue，不启用自动路径。

详细试点结果见 [2026-09-05 接续记录](handoff-pilot-2026-09-05.zh-CN.md)。
