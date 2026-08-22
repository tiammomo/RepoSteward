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

Runner 不把宿主工作区直接以读写方式交给容器。每次验证先创建一份临时快照，
Bootstrap 和后续无网络命令共享该快照及专用 HOME/工具缓存，因此依赖只安装一次，
但宿主 `.venv`、`node_modules` 和其他本地环境不会被替换。Git 元数据单独只读挂载；
快照在成功或失败后都会清理，运行目录只保留有界日志和清理清单供审计。

## 仓库级 PR 组合视图

单个 run 的 Checkpoint 不能回答多个开放 PR 是否修改同一批文件。Portfolio Inspector 因此从
GitHub 当前事实临时构建仓库级快照：先完整分页枚举开放 PR，再复用 Merge Snapshot 的完整分页读取
获得每个 PR 的 head/base、文件、规模、required checks、Review 和未解决会话。采集错误、权限不足
或过程中检测到的 head/state 变化都会显式降低完整性，不会被当成“没有冲突”。

重叠关系按 changed file 建立倒排索引，只为实际共同修改文件的 PR 组合生成边；成本与读取的文件
引用和最终输出边数相关，不对所有 PR 两两重扫文件列表。规范化后的完整快照生成稳定摘要，调用方
可以用 expected digest 检测后续读取是否已经陈旧。v1 不把快照写入数据库、不调用 Harness，也不
修改 workspace 或 GitHub；它是依赖排序、WIP 策略和单步协调器的只读事实层。

WIP 容量门禁位于显式 submit 边界。RepoSteward 先按精确 branch 查询是否已有 open PR：已有 PR 的
增量 push 不消耗新容量；创建或重新打开 PR 才完整分页读取仓库 open PR，并只统计当前配置身份创建
的项目。默认每仓库 4 个，用户层可以调高，项目和仓库层只能取更小值。读取失败或达到上限时，在
创建 fork、push 或写 PR 前失败关闭。该门禁控制在线并发，不调用 Harness，也不改变 Portfolio 的
依赖、重叠或合并判断。

Dependency Planner 在该快照上叠加两类权威边：PR 正文中严格、独立的 `Depends on #N` 声明，以及
维护者通过显式本地门禁确认的 head-bound attestation。外部正文仍是不可信输入，解析器只接受仓库和
数字引用，不执行其中的其他语义；changed-file 重叠只形成建议，绝不自动升级为门禁。规划器以稳定
SCC、拓扑和反向邻接遍历以 O(V+E) 检测循环、传播阻塞，并识别缺失、跨仓库、关闭未合并和开放
依赖，最后生成规范化摘要。已合并依赖不再阻塞顺序，但会提示依赖方的 base 或验证证据可能需要刷新。

正文声明绑定当前 head、PR 作者和来源摘要，其编辑历史由 GitHub 保管；只读规划不会复制外部正文
或为它新增本地事件。维护者显式确认与撤销才进入本地追加审计，两种来源在计划中分别标识。

维护者确认与撤销都是本地追加事件，绑定仓库、依赖双方、当前 head、身份、来源和前一事件。相同
动作可并发幂等重试，撤销不删除历史；head 变化使旧确认失效。Portfolio plan 不调用 Harness 或写
GitHub，Merge Decision 只在当前 PR 存在直接依赖时读取目标 PR，并把依赖摘要和 blocker 纳入已有
的两次 freshness 检查。RepoSteward 不能阻止维护者绕过它直接在 GitHub 点击 Ready，但所有由
RepoSteward 产生的 Ready 判断和 merge 决策都会失败关闭。

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
- `storage_gc_runs`：对实际 GC 追加 `applying` 与 `completed` 记录，保存精确计划摘要、操作者与结果；
  进程中断会留下可识别的未完成意图，该审计不参与普通 GC。
- `github_pr_watermarks`：保存每个 run 已经形成 Review Checkpoint 的事件序号。事件先幂等入库，
Checkpoint 与水位再在同一事务中提交，因此中断后可以安全重试。

会改变上述事实的长任务按 `repository + Issue` 获取 SQLite 运行租约。租约包含 owner、
generation 和过期时间，并由执行进程续租；同一 Issue 的 prepare、adopt、repair、follow-up、
submit 与 merge 串行，不同 Issue 仍可并行。绑定租约时，每次短 Store 操作在同一 SQLite 写事务内
完成 generation 点查和状态写入；事务不会跨 Harness、Docker 或 GitHub 等慢边界。远端写入前还会
重新检查租约，并继续使用 head SHA、force-with-lease 等远端幂等条件。崩溃后的租约可在到期后接管，
但旧 generation 不能继续写入本地事实，所有获取、续租、接管和释放动作都保留追加式审计记录。

事件正文默认没有清理期限。只有仓库策略显式设置正整数 `event_payload_retention_days`，且同一 PR
的每个 run 水位都已经越过该事件时，正文才可成为 GC 候选。候选在删除事务内重新计算；删除会先
写 tombstone，再移除 Blob。无配置、保留期内或任一 run 尚未形成 Checkpoint 的正文都必须保留。
验证日志和可恢复的终态隔离工作区是默认可回收类别，期限和单次最大对象数由用户级 `[storage]`
配置拥有，项目层不能静默缩短。工作区扫描不跟随符号链接；只有所有关联 run 均到达终态
Checkpoint、Git 状态干净且 HEAD 可从 submitted run 或远端引用恢复时才会进入计划。apply 会再次
核对目录身份、元数据快照、HEAD 和 run 状态，变化即跳过。GC 默认 dry-run；apply 同时要求
`--apply` 和 `REPOSTEWARD_ENABLE_GC=1`，并在删除前后追加审计。普通 GC 永不删除事件索引、
Context Checkpoint、Portfolio Dependency、Merge Decision、Merge Execution 或自身审计。
- `merge_decisions`：追加保存每次合并评估的 head/base、policy、GitHub 快照与决策摘要；重复评估
  不覆盖旧结果，便于解释状态变化。
- `merge_executions`：按 attempt 追加保存执行身份、指定决策、精确 head、方法、写入前意图与最终
  GitHub 结果；`applying` 没有对应 `completed` 时表示进程可能在外部写入期间中断，重试必须先回读。
- `portfolio_dependency_events`：按依赖对追加保存维护者 confirm/revoke、当前 head、身份、来源和
  前一事件；事件 digest 唯一，读取时会同时校验物化列与规范化载荷。
- `owner_review_attestations`：追加保存单维护者对精确 PR 事实的本地审查声明；物化 scope、规范化
  facts、facts digest 和 attestation digest 在读取时交叉校验，普通 GC 不删除该审计。

Context Pack 与 Harness 绑定在一个事务中写入；Checkpoint 采用追加方式写入。数据库列中的版本、
摘要、基线和关联 ID 必须与 JSON 内容一致，否则拒绝保存。

PR 跟进以 GitHub 的结构化事件为唯一真相。`follow-up` 完整遍历 REST 分页，以稳定 ID 和规范化
内容摘要区分事件版本，并只从水位后的版本生成紧凑 Review Checkpoint。模型分类或摘要属于可重建
派生信息，不能覆盖事件，也不能直接授权 push、回复或 merge。原始评论和 Review 正文始终标记为
外部不可信输入。

水位后的事件先经过供应商无关的确定性预算器：相同稳定 ID 只保留最新版本，相同内容摘要跨 ID
去重，再按行级 Review、相关 diff、Review 和 Issue comment 的优先级装入预算。估算直接使用 UTF-8
字节数作为供应商无关的保守上界，并把紧凑 Checkpoint、head/base、安全阻塞、当前失败 check 和阻塞
Review 一并计入。强制事实不能裁剪；无法容纳时失败关闭。可选内容的版本替换、去重、字段裁剪、
预算裁剪和 diff 缺失均进入 `context_plan.stats`，因此 Harness 输入大小与丢弃原因都可复现。
事件规划阶段先保守预留 Context Pack 和提示包装空间；真正执行 Repair 前再渲染完整 Harness Prompt，
将 Issue 正文、指导路径、分片包装和 Checkpoint 纳入同一 UTF-8 字节预算并写回最终估算。超限时仅
裁剪可选 diff、低优先级反馈和 Repair 中重复携带的 Issue 正文；强制事实与至少一条可执行反馈不可
容纳时失败关闭。初次 `prepare` 的 Issue 输入不受该二次拟合影响。

Contributor 修复循环消费一个已提交 run 的下一批事件。确定性规划先排除重复轮询、非失败 check
和指向原 PR diff 之外路径的建议；只有剩余反馈需要理解代码时才创建 successor run 并调用 Harness。
successor 从已提交水位开始，避免重新注入完整 PR 历史。修复通过相同隔离 Runner 与 diff 策略后只
进入本地 `ready` 状态；每个新 commit 都需要新的 submit 身份确认。准备时冻结 head、base、policy
digest、事件水位和完整 GitHub 结构化快照，公开 push 前逐项复核，任一变化都使准备失效。

CI Failure Analyzer 是 follow-up 与 Repair 之间的只读证据层。当前失败 check 通过 Actions run/job
结构化关联定位，job/step 元数据完整分页，日志只从一分钟有效的签名 URL 读取；GitHub Authorization
不得转发到下载主机。每类最多读取 24 个日志，每个使用 256 KiB Range 上限，随后先脱敏再提取有界
错误片段，完整日志不持久化。
指纹由 workflow、job、平台标签、失败 step、测试标识和规范化错误片段组成。分类只使用同 head 的
其他 attempt、同 PR 历史和当前 base SHA 的 push run；证据不完整、第三方 provider 或结果矛盾时
返回 unknown。该层不调用 Harness、重跑 job、修改 PR，也不把 inherited 等同于通过。

Merge Decision Engine 位于执行器之前。它完整分页读取 GitHub 结构化状态，并以纯函数检查已验证
head/base、策略摘要、审批、required checks、未解决会话、规模和不可削弱的高风险路径。结果只表示
当前快照是否具备资格，不执行 merge，也不以 Harness 推理替代确定性事实。任何不完整快照都失败
关闭；策略、head 或 base 在验证后变化时必须重新验证。

Maintainer Merge Executor 是独立、默认关闭的 CLI 写入边界。仓库必须同时配置 Maintainer、
same-repository 和 `auto_merge = true`，进程还必须设置一次性环境开关并声明与 token 一致的身份。
执行器要求调用者指定一条已审计的 eligible 决策，随后重新计算完整活动摘要和 Merge Snapshot；
真正调用 GitHub 前再读取一次，且 PUT 请求绑定验证过的 head SHA。任何摘要不一致、高风险、超限、
不完整事实或权限问题都阻止写入。网络超时或 GitHub 返回不确定结果时先回读；相同 head 已合并视为
幂等成功，其他状态不会盲目重试。执行器不回复 Reviewer、不服务 Contributor mode，也没有常驻
调度器。

Owner Attestation 是 Merge Decision Engine 的可选输入，不是 GitHub Review。它只在仓库显式启用、
当前 token 身份为 owner/admin 且有 push 权限、PR 作者与操作者一致、head 是该 run 持续跟踪的
same-repository 分支、GitHub branch protection 与 applicable rulesets 均可完整读取且不要求独立
Reviewer 时创建。创建动作有独立环境开关，并要求除 ReviewDecision 外的全部合并门禁已经通过。
声明绑定 head/base、policy、diff、checks、Review、会话、活动、依赖和规则摘要；Merge Decision 与
Merge Executor 每次重新读取事实后才接受完全匹配的最新声明。它不会提交 GitHub Approval，也不会
调用 admin bypass。

Harness Usage Ledger 是本地追加式观测投影。Pipeline 在 Harness 返回后、进入 Runner 验证前写入
每个 run 唯一的 prompt-free 事件，因此后续验证失败仍能归因实际消耗。事件绑定 run、work item、
Issue、阶段、Harness 和模型，只包含规范化资源计数、原生会话恢复结果、可移植上下文兜底和裁剪原因；
事件摘要用于发现本地记录被意外修改。报告层再关联 run 中的 PR 和 Merge Executor 结果，按用户维护
的生效日期价格计算成本。历史记录缺失、定价缺失和不一致指标均保持 unknown。报告是确定性只读路径，
不会调用 Harness，也不会存储或重新加载原始提示。

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

适配器还必须声明版本化 `HarnessCapabilities`。控制面只依据能力决定是否使用原生
session 恢复等可选路径，不根据供应商名称猜测行为。缺少声明、版本未知或状态为
`unknown` 的能力一律按不支持处理，并使用 Context Pack 等可移植路径安全降级。
`doctor` 会输出同一份清单，便于接入新 Harness 时审计实际能力。

当前内置适配器为默认的 `codex-cli` 和显式可选的 `codex-sdk`，现有本地提交可通过
`external-workspace` 路径纳管。SDK 会优先恢复已有 thread；原生 thread 不存在或属于其他
账号时，从 Context Pack 创建新 thread。Claude Code 和 DeepSeek 适配器仍可作为独立、小范围
变更加入，不需要修改 Pipeline 的状态机。

## 后续优先级

1. 增加 Claude Code 与 DeepSeek 适配器，并运行同一契约测试套件。
2. 增加 context redaction 和跨机器加密导出策略。
