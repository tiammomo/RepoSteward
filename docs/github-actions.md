# GitHub Actions 公开写入门禁

RepoSteward 把 Issue 提案和正式 Issue 分开。多人协作的共享真相是 GitHub Projects Draft Issue；
本地 SQLite 只保存个人草稿和操作缓存，不作为团队审批记录。

## Issue 流程

```text
本地草稿或 Project 在线草稿
              ↓
GitHub Project Draft Issue
              ↓
只读 review workflow
最新正文 + 重复项 + 安全扫描 + review digest
              ↓
另一位 reviewer + GitHub Environment 审批
              ↓
promotion workflow
Project Draft Issue 转换为正式仓库 Issue
```

GitHub 官方支持 Project Draft Issue 在线保存标题和正文，并在审核后转换为仓库 Issue。RepoSteward
不会直接调用普通的 Issue 创建接口，因此 Project item 也是转换操作的幂等锚点。

### 仓库变量

在运行 workflow 的仓库中配置：

- `REPOSTEWARD_ISSUE_PROJECT_OWNER`：共享 Project 所属用户或组织；
- `REPOSTEWARD_ISSUE_PROJECT_NUMBER`：Project 页面中的编号；
- `REPOSTEWARD_ISSUE_PROJECT_OWNER_TYPE`：`user` 或 `organization`；
- `REPOSTEWARD_PUBLISHER_LOGIN`：最终发布凭据所属的 GitHub login。

### Secrets

- `REPOSTEWARD_GITHUB_REVIEW_TOKEN`：只能读取共享 Project 和目标仓库 Issue；
- `REPOSTEWARD_GITHUB_PUBLISH_TOKEN`：可以写共享 Project，并在目标仓库创建 Issue。

使用 classic PAT 时，review 凭据至少需要 `read:project`，publish 凭据需要 `project` 以及目标仓库
Issue 写权限；私有仓库还需要相应的 `repo` 访问。实际权限应按 Project 所属用户或组织以及目标仓库
收紧。发布 token 的实际 GitHub login 必须等于 `REPOSTEWARD_PUBLISHER_LOGIN`；当前版本的 promotion
因此使用用户 token，GitHub App 安装 token 需在后续引入可审计的 CI 身份协议后再支持。不要把 token
交给 Harness、测试、目标仓库代码或容器。

### Environments 与分支限制

创建两个 GitHub Environments：

- `reposteward-issue-review`：保护只读 Project token；
- `reposteward-issue-publishing`：配置 required reviewers，并只允许受保护的默认分支部署。

同时为默认分支配置 Ruleset，要求 PR、CI 和 Code Owner review。不要允许任意功能分支直接运行
带上述 secrets 的 workflow。

### 操作顺序

1. 运行 `Review online issue proposal`，输入 Project 提案的 GraphQL node ID、网页 URL 或 URL 中的
   `itemId` 数字，以及目标仓库。Project 网页直接建立的提案也可以进入同一审核流程。
2. 在日志中检查标题、重复 Issue、风险信号和 `review_digest`；正文以 Project 中的在线版本为准，
   workflow 不把完整正文复制到日志。
3. 如需修改，在 Project 中编辑 Draft Issue，然后重新运行 review。
4. 运行 `Promote reviewed issue proposal`，输入同一 item、目标仓库和最新 digest，并勾选已审查
   所有重复项。
5. Environment reviewer 确认后，workflow 再次读取线上内容。任何变化都会使 digest 失效。

同一目标仓库的 promotion 会串行执行。因此多人同时提交相似提案时，后一个任务会在前一个
Issue 已可见后重新查重；重复项快照发生变化将使旧 digest 失效，不会直接继续发布。
为保证数字 `itemId` 查找有界，RepoSteward 最多扫描 1,000 个未归档 Project item；团队应将已处理的
提案定期归档，也可以直接使用 `stage` 返回的 GraphQL node ID。

安全报告不会进入该流程。检测到高风险安全语义或凭据时，CLI 会在任何线上暂存或转换前失败。

## PR 流程边界

现有 `prepare`/`adopt` 只生成经过验证的本地 commit，`submit` 是唯一能够 push 和创建 PR 的
命令。GitHub-hosted Runner 不持久保存本地数据库和 worktree，因此不能仅靠另一个 workflow
直接恢复本地准备结果。远端 PR 发布需要独立的、带摘要的 publication bundle；在该协议完成前，
不要把 Harness、写 token 和目标仓库代码放进同一个 CI job，也不要使用 `pull_request_target`
执行外部代码。
