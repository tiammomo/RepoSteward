# 参与 RepoSteward 开发

RepoSteward 采用 Issue 驱动、聚焦 PR、自动检查和人工 Review 的维护方式。除明显的拼写修正外，
请先创建或认领 Issue，再开始实现。

多人共同准备 Issue 时，先使用团队配置的 GitHub Project Draft Issue 作为线上提案。提案在转换为
正式 Issue 前必须重新检查最新正文、潜在重复项和安全风险，并由提案创建者之外的 reviewer 确认。

## 开始前

1. 搜索已有 Issue 和 PR，确认问题尚未被处理或认领。
2. Bug Issue 应提供最小复现、实际行为、预期行为和环境信息。
3. 性能 Issue 应提供数据规模、基线、测量方法和可重复的性能证据。
4. Feature Issue 应说明用户问题、产品边界、替代方案和验收标准。
5. 安全问题不要进入公开 Issue，请按照 [`SECURITY.md`](SECURITY.md) 私下报告。

## 本地开发

项目需要 Python 3.12+、uv、Git 和 Docker。

```bash
uv sync
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uv run reposteward --help
uv build
```

仅运行与改动相关的测试不足以替代完整检查。涉及容器验证链路时，还应构建并检查 Runner：

```bash
uv run reposteward image build
```

## 分支与提交

- 不要直接在 `main` 上提交或推送代码；所有代码变更必须关联已审核的开放 Issue，并通过独立
  分支或 worktree 上的 PR 合并。安全事件按 `SECURITY.md` 私下处理，不先创建公开 Issue。
- 从最新 `main` 创建短生命周期分支，例如 `feat/context-redaction`、
  `fix/import-idempotency` 或 `perf/checkpoint-query`。
- 每个 PR 只解决一个 Issue，避免捆绑无关重构、格式化或依赖升级。
- 提交标题使用 Conventional Commits，例如 `feat(context): import portable bundles`。
- 提交不得包含 token、私钥、账号缓存、数据库、`.env`、运行日志或本机绝对路径。
- 使用 Coding Harness 时，仍需由提交者检查完整 diff、测试结果和公开说明。

仓库内的 `.agents/skills/reposteward-maintainer/SKILL.md` 提供从 Issue 审核、实现、验证到
CI/Reviewer 跟进的可复用流程，适用于不同 Coding Harness；它不会替代 RepoSteward 代码中的
身份、凭据、摘要和公开写入门禁。

## Pull Request

PR 应关联对应 Issue，并完整填写模板：问题、改动、验证、风险和自动化辅助说明。提交前请确认：

- 分支基于最新 `main`，工作区没有无关文件；
- 完整测试、lint、格式和构建检查通过；
- 新行为包含回归测试或说明无法自动测试的原因；
- 文档、配置示例和协议 schema 已随行为同步；
- 没有削弱公开写入门禁、凭据隔离或无网络验证边界；
- Reviewer 可以仅依据 Issue、diff 和验证记录判断改动是否合理。

维护者 Review 通过且必需检查为绿色后才能合并。合并通常采用 squash，以保持 `main` 历史聚焦。
