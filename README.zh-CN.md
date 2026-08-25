# RepoSteward

<p align="right"><a href="README.md">English</a></p>

> 让模型按照你定义的规则治理 GitHub 仓库。

RepoSteward 是位于 GitHub、Coding Harness 和隔离验证环境之间的本地优先控制面。仓库策略、
任务状态、审阅证据和公开写入门禁都保存在模型会话之外。

当前 0.1 版本把经过审核的 GitHub Issue 转换为经过验证和人工审阅的 Pull Request。
长期方向更进一步：模型能够根据维护者定义的目标和风险边界管理 Issue、实现和审查修改，
并推进低风险 PR。
下文提到的自治管家属于路线图，不是当前版本已经提供的功能。

<p align="center">
  <img src="docs/assets/reposteward-lifecycle.svg" width="100%" alt="RepoSteward 工作流：经过审核的 GitHub Issue 进入无凭据 Coding 工作区，隔离验证生成证据，维护者检查结果，再通过独立门禁发布 Draft PR 并跟进 CI 与 Reviewer 反馈。">
</p>

<p align="center"><sub>流程图提供可编辑的 <a href="docs/assets/reposteward-lifecycle.excalidraw">Excalidraw 源文件</a>。</sub></p>

## 为什么需要 RepoSteward

Coding Harness 擅长理解代码和修改工作区，但模型会话不适合持有长期仓库策略、GitHub 凭据、
公开写入权限或审阅记录。

RepoSteward 把这些责任留在确定性控制面：

- 开始工作前冻结 Issue、基础 commit、仓库指导文件和策略；
- 只向 Harness 提供无凭据 worktree 和有界 Context Pack；
- 在隔离容器中执行允许的验证命令；
- 保存 Checkpoint、验证证据、资源用量和 GitHub 事实；
- 公开写入前重新核对身份和远端状态；
- 把准备修改与发布、合并分开。

项目主要服务使用 Coding Agent 维护多个仓库的个人维护者和小团队。Contributor 流程继续受支持，
但 Maintainer 流程是默认产品路径。

RepoSteward 与 Coding Model、CI 和 GitHub 项目管理配合使用。它负责约束维护流程，不以批量制造
PR 为目标。

## 当前产品与长期方向

| 领域 | 0.1 已实现 | 长期方向 |
| --- | --- | --- |
| Issue 入口 | 本地草稿、重复项搜索、审核后的 Project Draft 转换 | 模型分类、价值评分、可逆关闭和策略约束的创建 |
| 修改实现 | Codex CLI 或可选 Codex SDK 准备聚焦的本地 commit | 隔离的 Steward、Builder 和 Reviewer 模型角色 |
| 验证 | 允许的命令在无凭据、无网络验证环境中运行 | 风险分级和基于真实表现的自治等级 |
| 发布 | 维护者检查 Review Packet 后单独运行 submit | 低风险 PR 阶段可在仓库常驻策略下推进 |
| 跟进 | 增量读取 CI 与 Review 变化并准备修复 | 日常仓库自治运营，异常时升级给人 |
| 合并 | 只读资格判断、可选 Owner Attestation、显式 merge 门禁 | 每个仓库根据表现取得并失去合并权限 |

在经过审核的 Issue 修改实现前，现有安全门禁仍是权威事实。
[定位 RFC #70](https://github.com/tiammomo/RepoSteward/issues/70) 记录模型自治仓库治理的长期方向。

## 安装

RepoSteward 要求 Python 3.12+、uv、Git、Docker、GitHub CLI，以及已登录的 Codex CLI。

```bash
git clone https://github.com/tiammomo/RepoSteward.git
cd RepoSteward
uv sync
uv run reposteward init
uv run reposteward --help
```

`init` 从当前 `gh auth` 和 Git 配置读取身份，然后把用户拥有的设置写入
`~/.config/reposteward/config.toml`。该文件不保存 GitHub token。

以 Maintainer 模式添加仓库：

```bash
cd /path/to/repository
uv run reposteward repo add owner/repository --mode maintainer
```

命令创建本机专用的 `.reposteward.toml`，并通过 `.git/info/exclude` 排除它。填写该仓库允许的
依赖安装和验证命令，然后构建验证镜像并检查环境：

```bash
uv run reposteward image build
uv run reposteward doctor
```

完整设置见[配置示例](reposteward.example.toml)和[中文操作手册](docs/operator-guide.zh-CN.md)。

## 准备第一个经过审核的修改

从你维护的仓库中一个已经审核的开放 Issue 开始：

```bash
uv run reposteward gate owner/repository 123
uv run reposteward prepare owner/repository 123
```

`prepare` 从最新默认分支创建隔离工作区，调用配置的 Harness，执行允许的验证命令，检查 diff，
并创建本地 commit。返回的 Review Packet 包含 commit、diff 规模、风险、验证状态、日志和资源用量。

先检查结果，不执行发布：

```bash
uv run reposteward inspect RUN_ID
uv run reposteward logs RUN_ID
```

检查精确 diff 和证据后，通过独立命令发布：

```bash
REPOSTEWARD_ENABLE_SUBMIT=1 \
  uv run reposteward submit owner/repository 123 \
  --reviewed-by your-github-login
```

默认结果是 Draft PR。环境门禁、配置身份、实际 GitHub 身份、当前 head、base、策略和远端 PR 状态
必须全部匹配。

发布后可以继续跟进：

```bash
uv run reposteward follow-up RUN_ID
uv run reposteward repair RUN_ID
uv run reposteward merge-decision RUN_ID
```

`follow-up` 只摄取发生变化的 GitHub 事实。`repair` 在反馈确实需要修改代码时准备新的验证
commit。`merge-decision` 保存确定性的资格判断，但不执行合并。

## Maintainer 视图

RepoSteward 还提供以只读为主的仓库级视图：

```bash
uv run reposteward inbox --repo owner/repository --format text
uv run reposteward portfolio inspect owner/repository --format text
uv run reposteward portfolio plan owner/repository --format text
uv run reposteward batch plan owner/repository --format text
uv run reposteward usage report owner/repository
uv run reposteward storage stats --repo owner/repository
uv run reposteward benchmark run --output .artifacts/benchmark.json
```

持久队列和 Batch Planner 只保存有界的控制面意图。它们不能自行开启 submit、Owner Attestation
或 merge 权限。

`benchmark run` 完全离线运行 RepoStewardBench v0。版本化 fixtures 覆盖安全门槛、上下文边界、
维护者注意力、故障恢复和规模；每个场景会重复执行以检测非确定性，关键安全与恢复场景组成硬门槛，
机器相关耗时只作为观察数据。使用 `--baseline PREVIOUS.json` 可以比较不同 commit 的语义指标变化。

## 信任边界

- GitHub 凭据只留在控制面，不传给 Harness、测试、仓库 hooks、Git push 或 Docker 容器。
- Git clone 和 push 使用宿主 SSH 身份；GitHub API 调用使用配置的维护者身份。
- Issue 正文、仓库文件、评论、Review 和导入上下文都按不可信输入处理。
- 验证命令在无网络环境中运行。依赖 Bootstrap 可以在独立的无凭据阶段联网。
- 项目配置可以收紧用户级限制，不能放宽凭据、路径、身份或公开写入控制。
- GitHub 事实不完整、head 或策略变化、存在竞争工作、高风险路径或 diff 超限时失败关闭。
- 每类公开写入都有独立环境门禁和新鲜度检查；队列不能自行开启这些门禁。

GitHub 写入使用配置的维护者身份。RepoSteward 在本地审计状态中保存模型、策略、证据、意图和结果
来源。

## 可移植任务状态

每次准备的修改都有版本化 Context Pack、追加式 Checkpoint 和 Harness run 记录。Harness 原生
会话可以加速恢复，但不是任务事实的唯一来源。

```bash
uv run reposteward context inspect RUN_ID
uv run reposteward context export RUN_ID --output handoff.json
uv run reposteward context import handoff.json
```

交接包包含摘要和有界任务事实，不包含账号凭据。导入内容仍按不可信输入处理。

## 路线图：先实现 Shadow Steward

模型自治维护的第一个里程碑是只读 Shadow Steward。它读取 Issue、PR、CI、Review 和人类拥有的
治理章程，再输出带证据、置信度和适用策略的行动建议。

自治权限按仓库逐级开放：

1. 只提供影子建议，不写 GitHub；
2. 执行可逆的 Issue 标签与关闭；
3. 按策略创建 Issue 和发布 Draft PR；
4. 通过独立模型审查推进 Ready；
5. 在 CI、新鲜度检查和观察期通过后合并低风险修改。

每一级都必须由真实结果证明，并允许自动降级。安全、权限、发布、治理变更等高风险工作继续升级给
人。模型可以提出策略调整，不能修改自己的权限。

## 文档

| 需要了解的内容 | 文档 |
| --- | --- |
| 英文产品入口 | [README.md](README.md) |
| 详细命令和操作流程 | [中文操作手册](docs/operator-guide.zh-CN.md) |
| 组件、持久化和 Harness 契约 | [架构文档](docs/architecture.md) |
| 使用 GitHub Actions 审核 Project Draft | [GitHub Actions](docs/github-actions.md) |
| 完整项目配置 | [TOML 示例](reposteward.example.toml) |
| 贡献流程 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 私下报告安全问题 | [SECURITY.md](SECURITY.md) |
| 长期定位决策 | [RFC #70](https://github.com/tiammomo/RepoSteward/issues/70) |

## 项目状态

RepoSteward 当前版本是 0.1。配置、Schema 和公开接口在 1.0 前仍可能调整。内置 Harness 包括
Codex CLI 和可选的 Codex SDK。Claude Code、DeepSeek 和模型自治仓库治理尚无内置实现。

项目采用 MIT 许可证。项目原名为 Starfix；文档列出的旧配置和状态位置仍保持兼容读取。

## 本地开发

```bash
uv sync
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uv run reposteward --help
uv run reposteward benchmark run
uv build
```

提交修改前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。漏洞应通过 [SECURITY.md](SECURITY.md)
私下报告，不要创建公开 Issue。
