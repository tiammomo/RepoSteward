# RepoSteward

[English](README.md) · **简体中文**

**让已有的 Coding Agent 带着项目上下文持续工作。**

RepoSteward 帮助个人维护者和小团队管理多个 GitHub 项目：理解代码、保存任务进展、
接续 Agent 会话、验证修改，并跟进 Issue 和 PR。你继续使用 Codex、Claude Code 或
Copilot 编码；RepoSteward 在会话之外保存项目事实、决定、验证证据与维护规则。

[快速开始](#快速开始) · [Codex 插件](#接入-codex-插件) · [MCP / Skills / A2A](#mcpskills-与-a2a) · [多项目管理](#管理多个已有项目) · [文档导航](#文档导航)

## 它能帮你做什么

| 使用场景 | RepoSteward 提供的能力 |
| --- | --- |
| 接手陌生项目或准备贡献 PR | 从本地代码生成导览，定位实现、测试与可回查的来源 |
| 换会话、换模型或换客户端 | 保存目标、决定、未完成事项与下一步，按任务恢复上下文 |
| 判断 Agent 的修改是否可交付 | 在隔离环境验证代码，区分 Agent 自述、历史结果与当前有效证据 |
| 同时维护多个仓库 | 关联已有 clone/worktree，集中查看任务、阻塞和 GitHub 缓存状态 |
| 持续跟进 Issue 和 PR | 审阅 Issue、准备修改、跟进 CI/评审，再按独立门禁发布与合并 |

项目从本机运行。CLI、MCP、Codex 插件与本地工作台共用底层服务和状态。

## 快速开始

从源码构建需要 **Python 3.12+、uv、Git、Node 22.12+ 和 npm**，然后可对已有本地仓库体验代码导览，
无需先配置 GitHub 身份、Docker 或 Agent。

```bash
git clone https://github.com/tiammomo/RepoSteward.git
cd RepoSteward
uv sync --locked --python 3.12

uv run reposteward understand scan /absolute/path/to/project
uv run reposteward understand guide /absolute/path/to/project
uv run reposteward understand query /absolute/path/to/project "symbol_or_path"
```

将路径替换为你的 clone/worktree。`scan` 写入仓库外的理解缓存；`guide` 返回项目入口、
建议阅读顺序与代码来源，`query` 按符号、路径或关键词缩小阅读范围。
修改代码后重新扫描，旧导览不会被当成当前事实。

目前以 Python 静态结构、项目清单和文档读取为主，不代表已理解所有语言或运行时行为。
详见[项目理解指南](docs/project-understanding.zh-CN.md)。

日常在其他项目目录使用时，按[独立安装指南](docs/local-installation.zh-CN.md)安装已核验的
wheel。下文的 `reposteward` 命令均假定已完成独立安装；不要在另一个项目里沿用本仓库的
`uv run` 环境。需要 MCP 或插件时，安装该 wheel 的 `mcp` extra。

## 接入 Codex 插件

插件推荐名称为 **`reposteward`**。四类技能分别负责理解项目、继续任务、验证改动和维护 PR。
它为你已有的 Codex 工作流提供上下文与工具。

### 1. 关联目标项目

首次配置身份时先完成 GitHub CLI 登录，再运行 `reposteward init`；已有用户配置则跳过。
下面以自己维护的项目为例，向其他项目贡献时选择 `--mode contributor`。

```bash
cd /absolute/path/to/project
reposteward repo add owner/project --mode maintainer
reposteward project link .
reposteward project inspect .
reposteward understand scan .
```

替换 `owner/project`；已配置仓库无需重复 `repo add`。该命令生成本机专用的仓库策略，
执行验证与交付前还需填写允许的依赖安装和验证命令。

### 2. 预览并导出插件

继续在目标项目目录执行；输出目录必须尚不存在，且位于目标仓库之外。

```bash
mkdir -p "$HOME/plugins"
reposteward plugin plan . --output "$HOME/plugins/reposteward"
```

检查返回的工作区、账号、解释器路径及文件内容，再将 `plan_digest` 代入独立导出命令：

```bash
reposteward plugin export . --output "$HOME/plugins/reposteward" \
  --plan-digest REVIEWED_PLAN_DIGEST
```

### 3. 注册、安装并开始使用

按[插件指南](docs/agent-plugin.zh-CN.md#在-codex-安装)将已导出的目录注册到个人 marketplace。
仅导出目录还不等于客户端已安装。在支持插件命令的 Codex 版本中，默认市场名为 `personal` 时执行：

```bash
codex plugin add reposteward@personal
codex plugin list --json
```

确认已安装、已启用，然后新开会话：

> 使用 reposteward 插件，先确认绑定项目，再读取项目导览和当前任务上下文，给出下一步建议。

完整的[改名、升级与回退说明](docs/agent-plugin.zh-CN.md)也适用于原先的 `reposteward-local`。
生成包包含本机路径；换机器时重新安装、关联和导出。

## MCP、Skills 与 A2A

按要完成的工作选择入口。**Skills 是工作指导，MCP 和 A2A 是通信协议，插件负责打包技能与连接。**
这些入口复用 RepoSteward 的任务和证据服务，各自有明确的作用范围。

| 入口 | 用来做什么 | 当前交付状态 |
| --- | --- | --- |
| CLI / JSON | 人工操作和脚本自动化；`--json-envelope` 选择版本化响应 | 已实现；详见[机器接口契约](docs/machine-interfaces.zh-CN.md) |
| MCP | 让已有 Agent 通过本地 STDIO 调用一个关联工作区的工具 | 已实现六类工具，需要可选 `mcp` 依赖 |
| Skills | 指导代码阅读、任务接续、验证改动和 PR 跟进 | Codex 插件导出四类技能，名称见下表 |
| 插件 | 打包绑定工作区的技能和 MCP 连接 | 已有 Codex 本机导出、诊断和安装预览；见[插件指南](docs/agent-plugin.zh-CN.md) |
| 指令文件 | 向 AGENTS.md、CLAUDE.md 和 Copilot 指令添加经过审阅的片段 | `integration plan/apply/revert`，保留已有指令 |
| HTTP 工作台 / OpenAPI | 在浏览器查看项目、任务和证据 | 已实现 FastAPI、React 和类型化 OpenAPI 契约；见[工作台指南](docs/local-workbench.zh-CN.md) |
| 持久异步操作 | 用 operation ID 查询进度、请求取消和恢复 | [Issue #165](https://github.com/tiammomo/RepoSteward/issues/165) 待交付；当前 MCP 未实现持久 Tasks |
| A2A | 向另一个 Agent 端点委派限定范围的项目理解报告 | [Issue #166](https://github.com/tiammomo/RepoSteward/issues/166) 待交付，当前主线未启用 |

### 直接接入 MCP

已有 MCP 客户端也可以直接连接，无需先安装 Codex 插件。先在运行服务的 Python 环境安装
RepoSteward 的 `mcp` extra，并按上文关联工作区。在目标项目目录选择对应客户端的预览：

```bash
reposteward mcp config . --client codex
reposteward mcp config . --client claude-code
reposteward mcp config . --client copilot-vscode
```

这些命令只输出配置，不安装或启用客户端连接。通过客户端自己的配置流程应用所选预览后，
客户端以 STDIO 启动 `reposteward mcp serve PATH`，服务限定在该工作区内。
详细步骤见[Agent 接续指南](docs/coding-agent-assistance.zh-CN.md#文件与-mcp-接入)。

六类工具为 `project`、`understanding`、`context`、`evidence`、`checkpoint`、`verification`，
用于读取项目和任务事实、保存进展、执行可信验证 profile。MCP 不提供 GitHub 发布和合并工具。
修改用户策略或验证 profile 后需要重启服务；保存检查点不代表测试已经通过。

### 四类 Skills 怎么用

| Skill | 适用场景 |
| --- | --- |
| `understand-project` | 阅读项目导览，按来源取回实现和测试证据 |
| `resume-task` | 中断或换客户端后恢复要求、决定、未完成工作与下一步 |
| `verify-change` | 执行可信验证 profile，核对证据是否仍适用于当前代码 |
| `maintain-pr` | 检查 RepoSteward 管理的 PR 的 CI、评审反馈和合并阻塞 |

向 Agent 描述对应任务即可，具体技能名称或命名空间由客户端提供。
例如：“使用 `understand-project`，带源码引用解释这个仓库的主要入口。”
导出的技能负责指导工具使用，安装技能不会增加公开写入权限。
仓库内 `.agents/skills/` 则是另一组面向贡献者和维护者的工作指导。

### A2A 与跨客户端接续

计划中的 A2A 服务委派的是**项目理解报告**。报告完成不表示代码已修改、验证或发布。
该能力交付前，跨客户端继续工作使用 CLI/MCP 配合 Context Pack 和 Checkpoint。
Context Pack/Bundle 当前写出 v3，Checkpoint 写出 v1；这些持久文档版本与 MCP 协商版本、
A2A 协议版本分别管理。

接入前用实际运行的可执行文件检查安装：

```bash
reposteward version
reposteward capabilities
reposteward --json-envelope capabilities
reposteward doctor --local
```

当前主线报告 `a2a.implemented=false`、`mcp.durable_async_tasks=false`。
源码合入、安装包升级、客户端实际连接成功是三个不同阶段。
各协议的版本依据、实现边界和交付跟进见[协议与兼容性索引](docs/protocol-map.zh-CN.md)。

## 管理多个已有项目

项目可以保持原有目录结构。先按项目自己的方式 clone，再用 `project link` 登记：

```bash
reposteward project link /absolute/path/to/project-a
reposteward project link /absolute/path/to/project-b
reposteward project list
reposteward overview show --format text
reposteward web
```

- **仓库与工作区分开管理**：相同 remote 的多个 worktree 属于同一项目，各自保留工作区绑定。
- **关联不等于授权**：参与维护或贡献时，还需为对应仓库配置角色与策略。
- **每个插件实例绑定一个工作区**：额外项目使用 `reposteward-project-a` 等不同实例名；切换 Codex 目录不会自动切换绑定。
- **线上事实显式刷新**：`overview refresh` 读取 GitHub 并保存带时间和错误状态的缓存；`overview show` 与网页查询读取本地事实；网页同步按钮显式创建持久本地操作，区分开放、已合并和已关闭 PR，并保留来源时间。

FastAPI/React 工作台提供跨项目待办、项目导览、任务接续和审阅依据，支持本地查询和显式 GitHub 同步。
安装 wheel 已内置前端资源，日常使用无需 Node；在项目页粘贴 GitHub URL 或本地路径，审阅计划后可关联已有目录、克隆到新目录或仅关注远程项目；导入不覆盖已有代码，也不授予维护权限。
详见[本地工作台](docs/local-workbench.zh-CN.md)与[Agent 接续指南](docs/coding-agent-assistance.zh-CN.md)。

## 从任务接续到 GitHub 维护

日常编码可以继续在自己的 Agent 中进行。开发任务从已审阅的开放 Issue 开始，在 feature
branch 上用 `task start` 建立任务，再通过 CLI/MCP 读取上下文、保存检查点并执行受约束的验证。
若希望由 RepoSteward 调用已配置的 Codex Harness 准备修改，则使用 `gate`、`prepare` 流程。

<p align="center">
  <img src="docs/assets/reposteward-lifecycle.svg" width="100%" alt="经过审阅的 Issue 进入独立工作区，Agent 准备修改，隔离验证生成证据，维护者审阅后独立发布 PR，再跟进 CI 与评审。">
</p>

<p align="center"><sub><a href="docs/assets/reposteward-lifecycle.excalidraw">流程图可编辑源文件</a></sub></p>

| 阶段 | 常用入口 |
| --- | --- |
| 接续已有 Agent 任务 | `task start`、`task current`、`task context`、检查点 |
| 委派一次经过审阅的修改 | `gate`、`prepare`；`adopt` 接入已有的干净提交 |
| 检查结果 | `inspect`、`logs`、独立验证记录 |
| 发布与跟进 | 独立 `submit`、`follow-up`、`repair` |
| 评估合并 | `merge-decision`；符合条件后另行审阅并执行合并 |

联网维护需要 GitHub 身份、仓库策略与 SSH；隔离验证需要 Docker/Runner；委派编码还需
相应 Harness 的认证。`submit` 要求独立调用、`REPOSTEWARD_ENABLE_SUBMIT=1` 和匹配身份的
`--reviewed-by`。MCP 不提供发布或合并工具。

完整命令、Project Draft 提案流程与维护门禁见[操作手册](docs/operator-guide.zh-CN.md)。

## 上下文、验证与用量

RepoSteward 按任务提供有预算的 Context Pack，保留要求、决定、未完成事项与来源。
可选内容被省略时给出覆盖说明和取回线索；强制要求超出预算时明确报错。
Checkpoint 用于跨会话接续，不需要把所有历史聊天重新塞进上下文。

Agent 报告的“完成”和“测试通过”会与独立验证证据分开。验证结果绑定代码快照和策略；
代码变化后，旧结果作为历史保留。Harness 与测试不接收 GitHub 凭据，验证命令在隔离容器
内离线运行；依赖准备使用独立的无凭据阶段。

[用量采集](docs/external-usage.md)只处理显式选定的任务与会话片段，不代表能读取你所有项目的
聊天记录。缺失指标保持未知；当前没有可承诺的 token 节约比例。

## 客户端支持与当前边界

| 接入形式 | 当前支持 |
| --- | --- |
| Codex 插件 | 四类技能、六类 MCP 工具，固定工作区与账号范围 |
| Codex CLI / 可选 Codex SDK | 可作为 RepoSteward 调用的 Coding Harness |
| Claude Code / Copilot（VS Code） | CLI/文件接续与 MCP 配置预览；原生插件打包、托管 Runner 及实机验证范围另行推进 |
| 本地工作台 | 本地项目与任务查询、显式 GitHub 同步；不提供公网多用户服务 |

[客户端试点记录](docs/handoff-pilot-2026-09-05.zh-CN.md)说明具体版本和当次验证范围。
长期方向是在维护者规则下逐步增加仓库自动化：先有证据充分的建议，再扩大可执行动作。
[RFC #70](https://github.com/tiammomo/RepoSteward/issues/70)记录这一方向，不代表当前已有无人值守自治维护能力。

## 文档导航

| 我想了解 | 文档 |
| --- | --- |
| 安装、配置诊断、状态升级 | [独立安装](docs/local-installation.zh-CN.md) · [备份与迁移](docs/state-upgrades.zh-CN.md) |
| 插件接入与会话接续 | [Codex 插件](docs/agent-plugin.zh-CN.md) · [已有 Agent](docs/coding-agent-assistance.zh-CN.md) |
| 代码理解与项目浏览 | [阅读导览](docs/project-understanding.zh-CN.md) · [工作台](docs/local-workbench.zh-CN.md) |
| GitHub 维护、队列、组合视图与清理 | [操作手册](docs/operator-guide.zh-CN.md) · [配置示例](reposteward.example.toml) |
| 接口、Schema 与协议交付范围 | [协议索引](docs/protocol-map.zh-CN.md) · [CLI/MCP 契约](docs/machine-interfaces.zh-CN.md) |
| 系统结构、源码位置与验证边界 | [架构](docs/architecture.md) · [源码指南](docs/source-layout.zh-CN.md) · [Project Draft Actions](docs/github-actions.md) |
| 用量、版本与发布 | [用量采集](docs/external-usage.md) · [变更日志](CHANGELOG.md) · [发布与回退](docs/releases.md) |

## 版本与参与开发

当前为 **0.1.0 开发基线**，配置、schema 和公开接口在 1.0 前仍可能调整。
使用 `reposteward version` 检查安装，同时记录提交与 wheel 摘要；插件内容摘要用来区分导出包，
不等于正式发布版本。正式 tag、GitHub Release 与变更日志按[版本规则](docs/releases.md)对应。

贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [AGENTS.md](AGENTS.md)，从经过审阅的
Issue 开始，在独立分支提交聚焦 PR。完整验证按仓库规范在加固容器内执行。
代码、文档、可复现的问题和实际使用反馈都欢迎贡献。

项目采用 [MIT](LICENSE) 许可证。安全问题通过 [SECURITY.md](SECURITY.md) 私下报告。
原名 Starfix 的旧配置与状态位置按文档保持兼容读取。
