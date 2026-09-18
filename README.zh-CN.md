# RepoSteward

[English](README.md) · **简体中文**

**让已有的 Coding Agent 带着项目上下文持续工作。**

RepoSteward 帮助个人维护者和小团队管理多个 GitHub 项目：理解代码、保存任务进展、
接续 Agent 会话、验证修改，并跟进 Issue 和 PR。你继续使用 Codex、Claude Code 或
Copilot 编码；RepoSteward 在会话之外保存项目事实、决定、验证证据与维护规则。

[快速开始](#快速开始) · [Codex 插件](#接入-codex-插件) · [多项目管理](#管理多个已有项目) · [文档导航](#文档导航)

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

先体验代码导览，只需要 **Python 3.12+、uv、Git** 和一个已有的本地仓库，
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
- **线上事实显式刷新**：`overview refresh` 读取 GitHub 并保存带时间和错误状态的缓存；`overview show` 与当前网页读取本地事实。

当前网页提供跨项目待办、项目导览、任务接续和审阅依据，是本机只读入口。
FastAPI/React 工作台及通过网页粘贴链接导入项目仍在后续开发范围。
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
| 本地工作台 | 只读浏览本机项目和任务；不提供公网多用户服务 |

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
| 系统结构、持久化与验证边界 | [架构](docs/architecture.md) · [Project Draft Actions](docs/github-actions.md) |
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
