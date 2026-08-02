# Starfix

Starfix 是一个“低频、高质量”的开源贡献流水线：从白名单项目发现合适的
GitHub issue，让 Codex 在独立 clone 中实现最小修复，再在无凭据容器里重跑
测试。只有通过项目贡献门槛、diff 限额和人工审阅后，才会用 `tiammomo` 的
GitHub 身份创建 draft PR。

当前第一阶段只启用 `langchain-ai/deepagents`。这样可以先验证整条链路和 PR
质量，再逐个增加 DeerFlow、pi 等项目的专属规则。

## 当前第一单

候选是 [deepagents #5112](https://github.com/langchain-ai/deepagents/issues/5112)：
slash-containing grep glob 的内联 Python 命令没有被安全地传给 shell。补丁已在
本地分支 `tiammomo/sdk/quote-path-glob-script` 完成，提交为 `ca1d9e4`，工作区位于
`.starfix/workspaces/deepagents-5112`。

验证结果：

- 旧实现在完整 `/bin/sh` 命令路径下可稳定复现失败；
- 修复后相关同步/异步与边界测试 13 项通过；
- ruff、格式检查和 ty 全部通过；
- DeepAgents SDK 单元测试：2434 passed、95 skipped、4 xfailed，覆盖率 91%。

DeepAgents 规定外部贡献必须先由 maintainer 批准并把 issue assign 给贡献者。
截至 2026-08-02，`tiammomo` 已留言申请但尚未被 assign，所以程序会阻止 push/PR。

## 安装与检查

要求：Python 3.12+、uv、Git、Docker、已登录的 Codex CLI，以及属于
`tiammomo` 的 GitHub token。Token 只从当前进程的 `GITHUB_TOKEN` 或
`GH_TOKEN` 读取，不写入配置或 git remote。

```bash
uv sync
uv run starfix image build
GITHUB_TOKEN=... uv run starfix doctor
```

`doctor` 会核对 GitHub token 的实际 login、Docker daemon、Codex、Git 和隔离
runner 镜像。它不会打印 token。

## 工作流

刷新并查看候选：

```bash
uv run starfix discover --repo langchain-ai/deepagents
uv run starfix list --all
```

检查某个 issue 的 assignment/批准状态：

```bash
uv run starfix gate langchain-ai/deepagents 5112
```

准备一项修复：

```bash
uv run starfix prepare langchain-ai/deepagents 5112
```

这一步会 clone 最新 `main`、运行 `codex exec`、在 Docker 中安装依赖并执行
Codex 提议的 allowlisted pytest/ruff/ty 命令、检查 diff，然后创建本地 commit。
Codex 与测试进程都拿不到 GitHub token；测试容器也没有宿主目录、Docker
socket 或测试阶段网络。

准备完成后，先人工阅读 `.starfix` 中记录的 worktree 和 diff。确认理解并负责
该改动，且上游 assignment gate 已满足后，单独提交：

```bash
STARFIX_ENABLE_SUBMIT=1 GITHUB_TOKEN=... \
  uv run starfix submit langchain-ai/deepagents 5112 --reviewed-by tiammomo
```

如果补丁是在 Starfix 外部先完成的，可以用 `starfix adopt` 配合明确的
`--worktree`、摘要、实现说明和重复 `--verify` 参数，把现有 commit 走同一套容器
验证与 diff 门禁后登记为待审阅状态。

提交动作有三重限制：显式环境开关、GitHub token 必须属于 `tiammomo`、人工
review attestation 必须与配置账号一致。默认每天最多两个 PR，并始终先开 draft。

`uv run starfix run --limit 1` 可以自动刷新并准备最高分候选，但对于 DeepAgents，
只有已 assign 给 `tiammomo` 的 issue 才会进入自动修复；它永远不会自动执行
`submit`。

## 设计边界

- issue 标题、正文和仓库内容都视为不可信输入；安全报告、已认领 issue、
  `inprogress`/`duplicate` 等标签会被阻断。
- Codex 使用内置 `:workspace` permission profile、无命令审批、禁用命令网络，且
  子进程环境显式剥离 token/API key；输出必须符合 JSON Schema。
- 依赖安装可在无凭据容器中联网，实际验证命令在 `--network none` 容器中运行。
- `.github/workflows`、凭据/secret 路径以及过大的 diff 默认不可提交。
- Starfix 不自动发 issue、评论或催促 maintainer，避免 tracker spam。
