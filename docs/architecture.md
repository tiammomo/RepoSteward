# RepoSteward 架构与上下文连续性

RepoSteward 不是另一个 Coding Agent。它是位于 GitHub、Coding Harness 和隔离验证环境之间的
项目维护控制面：把一次性的 Agent 对话变成可审计、可恢复、可移交的 Issue-to-PR 工作流。

## 为什么不直接使用 Codex 或 Claude Code

Coding Harness 擅长理解代码、调用工具和修改工作区，但它通常不知道一个项目长期采用的贡献
门禁、公开写入规则、验证隔离、历史决策和审阅责任。直接使用 Harness 时，这些内容需要在每次
新会话、账号切换或模型切换后重新解释。

RepoSteward 在 Harness 之外持续保存这些信息，并提供稳定的控制面：

- 项目策略：仓库贡献规则、验证 allowlist、diff 限额和公开提交门禁；
- 工作状态：Issue、run、commit、验证证据、风险、决定和下一步；
- 安全边界：Harness 不持有 GitHub 凭据，测试在无凭据、无网络容器中运行；
- 人工责任：准备和提交分离，公开写入必须经过显式开关和 review attestation；
- 可移植交接：不依赖某个供应商的会话 ID，也不依赖某个 GPT 账号的历史记录。

Issue 在进入实现流水线前也采用准备/发布分离：GitHub Project Draft Issue 是多人共享的线上
提案，仓库正式 Issue 是经过第二人审核后的工作契约。二者不能由一次无审核写操作直接贯通。

Harness 仍然可以保留自己的原生 session，作为命中缓存或继续推理的加速信息；它不是任务事实
的唯一副本。

## 组件边界

```text
GitHub facts + repository policy
              │
              ▼
       Versioned Context Pack ─────► Coding Harness
              │                         │
              │                         ▼
              │                    workspace edits
              │                         │
              ▼                         ▼
      append-only Checkpoint ◄──── isolated verification
              │
              ▼
       compact human review
              │ explicit submit gate
              ▼
            GitHub PR
```

Issue 入口使用独立状态机：

```text
local draft ── explicit stage ──► Project Draft Issue
                                      │ online edits
                                      ▼
                         duplicate + security review digest
                                      │ distinct reviewer
                                      ▼
                              repository Issue
```

- RepoSteward 拥有流水线、策略、持久状态、审计和 GitHub 读写。
- Harness 只接收一个 Context Pack 和隔离工作区，返回规范化结果与使用量。
- Runner 只安装依赖并执行已允许的验证命令，不接触宿主凭据。
- 人类审阅者决定是否发布，并对最终提交负责。

## 持久数据模型

SQLite 使用显式 `PRAGMA user_version` 迁移。现有用户的未版本化数据库会原地升级；高于当前
程序支持版本的数据库会拒绝打开，避免旧程序破坏新数据。

- `work_items`：同一仓库 Issue 的稳定身份，唯一键为 repository、kind、external ID；
- `runs`：一次准备、验证或提交过程；
- `context_packs`：run 开始时冻结的目标、基线 commit、策略摘要、指导文件指纹和来源信任；
- `harness_runs`：run 与 Harness、模型、可选原生 session 的绑定；
- `checkpoints`：按 run 单调递增的状态快照，记录 HEAD、完成项、决定、证据、风险和下一步。
- `context_imports`：按 bundle digest 去重保存的跨机器交接历史，不覆盖本机较新的 Issue 快照。
- `issue_proposals`：缓存本机发起的 Project item 和转换结果；GitHub Project 中的最新内容仍是
  多人审查阶段的唯一真相。
- `github_pr_events`：按 repository、PR、事件类型、稳定 ID 和内容摘要追加 GitHub 事件版本；
  评论编辑、check 状态和 PR head 变化不会覆盖旧版本。事件行只保留稳定 ID、作者、状态、时间与
  内容摘要，原始正文显式标记为 GitHub 不可信输入。
- `content_blobs`：按 SHA-256 对较大的 GitHub 事件载荷做内容寻址和跨引用去重；事件索引通过摘要
  引用 Blob，事务会同时写入并校验内容，旧版内联正文原地无损迁移。
- `content_blob_tombstones`：记录经显式保留策略删除的载荷摘要、原大小、原因和时间，防止后续完整
  GitHub 轮询把已到期正文静默恢复；事件轻量索引和水位仍永久保留。
- `github_pr_watermarks`：保存每个 run 已经形成 Review Checkpoint 的事件序号。事件先幂等入库，
Checkpoint 与水位再在同一事务中提交，因此中断后可以安全重试。

事件正文默认没有清理期限。只有仓库策略显式设置正整数 `event_payload_retention_days`，且同一 PR
的每个 run 水位都已经越过该事件时，正文才可成为 GC 候选。候选在删除事务内重新计算；删除会先
写 tombstone，再移除 Blob。无配置、保留期内或任一 run 尚未形成 Checkpoint 的正文都必须保留。
- `merge_decisions`：追加保存每次合并评估的 head/base、policy、GitHub 快照与决策摘要；重复评估
  不覆盖旧结果，便于解释状态变化。

Context Pack 与 Harness 绑定在一个事务中写入；Checkpoint 采用追加方式写入。数据库列中的版本、
摘要、基线和关联 ID 必须与 JSON 内容一致，否则拒绝保存。

PR 跟进以 GitHub 的结构化事件为唯一真相。`follow-up` 完整遍历 REST 分页，以稳定 ID 和规范化
内容摘要区分事件版本，并只从水位后的版本生成紧凑 Review Checkpoint。模型分类或摘要属于可重建
派生信息，不能覆盖事件，也不能直接授权 push、回复或 merge。原始评论和 Review 正文始终标记为
外部不可信输入。

Merge Decision Engine 位于执行器之前。它完整分页读取 GitHub 结构化状态，并以纯函数检查已验证
head/base、策略摘要、审批、required checks、未解决会话、规模和不可削弱的高风险路径。结果只表示
当前快照是否具备资格，不执行 merge，也不以 Harness 推理替代确定性事实。任何不完整快照都失败
关闭；策略、head 或 base 在验证后变化时必须重新验证。

三类协议文档都使用 Draft 2020-12 JSON Schema，schema 随 Python 包发布。持久化和导入边界会
拒绝未知字段、未来版本、跨 work item/run 的关联错配及不一致摘要。Bundle digest 只能检测意外
损坏或内容变化，不是数字签名；导入数据仍保留其原始信任级别。

## 跨会话与跨账号恢复

同一个 work item 再次执行时，RepoSteward 会读取最新 Checkpoint，压缩后放入新的 Context Pack。
长描述、条目数和交接字段均有上限，避免历史上下文无限增长。历史结论标记为
`derived_review_required`，新的 Harness 必须对照当前 checkout 和证据重新验证。

```bash
uv run reposteward context inspect RUN_ID
uv run reposteward context export RUN_ID --output handoff.json
uv run reposteward context import handoff.json
```

导出包包含 schema 版本、内容摘要和 token 粗略估算，不包含 GitHub、模型供应商或其他账号凭据。
因此切换 Codex GPT 账号、Claude Code、DeepSeek Harness、机器或 CI worker 时，仍可从同一份
任务事实继续；供应商原生 session 丢失只会影响加速，不会改变任务状态。
导入按 digest 幂等保存；本机已经从 GitHub 刷新的 work item 具有更高优先级，旧导出不会反向
覆盖当前标题和状态。

## Harness 契约

适配器实现 `Harness.run(HarnessRequest) -> HarnessExecution`：

- 输入只有 worktree、run directory 和不可变 Context Pack；
- 输出必须规范化为摘要、PR 标题、实现说明、验证命令、风险、技术决定和下一步；
- 额外返回 Harness 名称、模型、可选 session ID 和资源指标；
- 不得 push、创建 PR、访问 GitHub 凭据或绕过验证策略；
- 不支持的 Harness 必须显式失败，不能静默回退到另一个供应商。

当前内置适配器为默认的 `codex-cli` 和显式可选的 `codex-sdk`，现有本地提交可通过
`external-workspace` 路径纳管。SDK 会优先恢复已有 thread；原生 thread 不存在或属于其他
账号时，从 Context Pack 创建新 thread。Claude Code 和 DeepSeek 适配器仍可作为独立、小范围
变更加入，不需要修改 Pipeline 的状态机。

## 后续优先级

1. 增加 Claude Code 与 DeepSeek 适配器，并运行同一契约测试套件。
2. 为增量 reviewer feedback 增加统一 token 预算、内容去重和 Contributor 修复闭环。
3. 增加 context redaction、分层保留期限、安全 GC 和跨机器加密导出策略。
