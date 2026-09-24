# GitHub Actions 评测与公开写入门禁

RepoSteward 把 Issue 提案和正式 Issue 分开。多人协作的共享真相是 GitHub Projects Draft Issue；
本地 SQLite 只保存个人草稿和操作缓存，不作为团队审批记录。

## CI 评测报告

`CI / quality` 在锁定的 Python 3.12 环境中运行完整 RepoStewardBench。依赖安装完成后，
评测命令使用 `uv run --locked --offline`；场景不访问网络、不调用模型，也不读取账号配置。
评测失败会让 quality 失败，但已经生成的 JSON 仍会归档。上传步骤只读取
`benchmark-report.json`，找不到该文件时失败；评测被跳过或任务取消时不上传。

报告保存 **30 天**，artifact 名称为
`reposteward-bench-<github.sha>-<run_id>-<run_attempt>`。每次重跑拥有独立名称，避免覆盖历史。
`pull_request` 的 `github.sha` 是 GitHub 测试的合并提交，通常不同于 PR head；`push` 则是主线
提交。核对 artifact 名称、运行页面和报告的 `git_sha`，不要把两种 SHA 混用。

上传 action 固定到官方完整 commit SHA；输入、保留期与下载方式可查阅
[upload-artifact 文档](https://github.com/actions/upload-artifact)和
[GitHub 下载指南](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)。
quality 仍只有 `contents: read`，没有新增 Secrets、发布凭据或写权限；归档报告本身不授予
Issue、PR 或合并权限。

### 下载并比较基线

在可信 CI 运行页面确认仓库、提交、workflow 及运行结果后下载对应 artifact。也可用已有 GitHub
CLI 登录下载；将下面的 `RUN_ID`、`COMMIT_SHA` 和 `ATTEMPT` 替换为该运行的实际值：

```bash
gh run download RUN_ID --repo tiammomo/RepoSteward \
  --name reposteward-bench-COMMIT_SHA-RUN_ID-ATTEMPT \
  --dir .artifacts/baseline
uv run --locked --offline --python 3.12 reposteward benchmark run \
  --baseline .artifacts/baseline/benchmark-report.json \
  --output .artifacts/current.json
```

本地比较前先在受信任环境安装锁定依赖；公共仓库验证仍应在加固 verifier 内执行。
基线仅作为 JSON 数据读取，不能把下载的文件当脚本执行。`baseline_comparison` 展示新增失败、
语义摘要变化和共同指标差值；命令退出状态由本次场景结果决定，不会仅因指标变化自动失败。
耗时只供观察，不设绝对耗时门槛。30 天后 artifact 会过期，需要长期比较时自行保存已核验的
JSON 和来源记录；仓库不提交机器相关 timing 基线。

这些是确定性控制面场景，不能替代真实客户端接续试点、模型质量或 token 节省测量。

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

RepoSteward 默认使用 `require_distinct_reviewer = true`。只有单维护者在可信用户配置中显式设为
`false` 时，提案创建者才可以同时作为 `--reviewed-by`；项目级配置无法覆盖 `[issue_review]`。
当前附带 workflow 每次都生成默认配置，因此固定用于团队第二人模式。单维护者自审应使用本地受控
CLI，而不是移除 workflow 的 Environment 保护。关闭 distinct reviewer 不会关闭最新 digest、
重复项确认、安全扫描、发布身份校验、显式环境开关或 promotion 的独立调用。

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
