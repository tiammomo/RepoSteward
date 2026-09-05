# RepoSteward 中文操作手册

> 本文档保留 RepoSteward 0.1 的详细命令和运维说明。产品入口、当前能力与长期方向见
> [English README](../README.md) 或 [中文 README](../README.zh-CN.md)。

> 把 GitHub Issue 变成经过验证和人工确认的 Pull Request。

RepoSteward 是运行在 GitHub 和 Coding Harness 之间的本地维护控制面。它保存 Issue、仓库策略、
执行状态和验证证据，让 Codex 等 Harness 专注于推理和修改工作区。是否创建 Issue、推送分支或
提交 PR，仍由用户通过单独的审核门禁决定。

RepoSteward 适合需要长期维护 GitHub 项目、在多个 Coding Harness 或账号之间切换，又希望保留
统一审阅记录的维护者和贡献者。它不是批量 PR 机器人，也不会让模型直接持有 GitHub 凭据。

<p align="center">
  <img src="assets/reposteward-lifecycle.svg" width="100%" alt="RepoSteward 工作流：GitHub Issue 经过策略门禁进入 RepoSteward，Coding Harness 在无凭据工作区实现修改，隔离 Runner 完成验证，人工审阅后创建 Draft PR，CI 与 Reviewer 反馈再增量回流。">
</p>

<p align="center"><sub>技术图提供可编辑的 <a href="assets/reposteward-lifecycle.excalidraw">Excalidraw 源文件</a>。</sub></p>

## 一分钟理解

RepoSteward 把一次代码维护任务拆成八个可审计步骤：

1. 从 GitHub Issue 冻结目标、范围和讨论事实；
2. 检查重复项、权限、贡献规则和竞争工作；
3. 由 RepoSteward 保存策略、状态、上下文、审计和 GitHub 写入门禁；
4. 让 Coding Harness 只在隔离 workspace 中推理和编辑，不向它暴露 GitHub 凭据；
5. 在无凭据、无网络的 Runner 中执行允许的验证命令；
6. 由用户审阅最终 diff、验证证据和风险；
7. 通过独立命令显式创建 Draft PR；
8. 增量采集 CI 与 Reviewer 反馈，重新进入同一个受审计流程。

这套路径的重点不是替代 Codex、Claude Code 或其他 Harness，而是让不同 Harness、账号和机器
共享同一份任务事实，同时把公开写入和安全边界留在确定性的控制面中。

## 从这里开始

| 目标 | 建议入口 |
| --- | --- |
| 第一次试用 | [安装](#安装) → [添加项目](#添加项目) → [基本工作流](#基本工作流) |
| 评估产品边界 | [产品边界](#产品边界) → [架构文档](architecture.md) |
| 运行指标评测 | [RepoStewardBench 指标评测](#repostewardbench-指标评测) |
| 切换 Harness、账号或机器 | [上下文与跨 Harness 交接](#上下文与跨-harness-交接) |
| 参与开发 | [贡献指南](../CONTRIBUTING.md) → [安全报告说明](../SECURITY.md) |

## 当前状态

项目仍处于 0.x 早期开发阶段。已经实现的主流程包括：

- 在本地准备 Issue 草稿、搜索重复项，并通过 GitHub Project 审核线上提案；
- 从已有 Issue 创建隔离工作区，调用 Codex CLI 或 Codex SDK 完成修改；
- 在无凭据、无网络的容器中执行允许的验证命令；
- 生成紧凑的 Review Packet，并在人工确认后创建 Draft PR；
- 使用 Context Pack 和 Checkpoint 在账号、机器或 Harness 之间交接任务。
- 只读汇总仓库全部开放 PR，并标出文件重叠、CI、Review 与事实完整性。

Claude Code、DeepSeek 等 Harness 目前只有统一接入契约，尚未提供内置适配器。配置格式和公开
接口在稳定版本发布前仍可能调整。

## 产品边界

| 组件 | 职责 |
| --- | --- |
| RepoSteward | 流水线、仓库策略、持久上下文、审计和 GitHub 事实 |
| Coding Harness | 推理和工作区编辑，不接触 GitHub 凭据 |
| Docker Runner | 安装依赖并执行隔离验证 |
| 用户 | 审阅 Issue、diff 和验证证据，决定是否公开提交 |

Harness 的原生会话只用于加速恢复。版本化 Context Pack 与 Checkpoint 才是任务连续性的记录。
组件设计、持久数据模型和跨账号恢复约束见
[`docs/architecture.md`](architecture.md)。README 顶部流程图描述稳定的产品路径；内部组件和
持久化细节以架构文档为准。

## 安装

要求 Python 3.12+、uv、Git、Docker、GitHub CLI，以及已登录的 Codex CLI。

```bash
git clone https://github.com/tiammomo/RepoSteward.git
cd RepoSteward
uv sync
uv run reposteward --help
uv run reposteward init
```

`init` 默认从当前 `gh auth` 和 Git 全局配置读取登录名、姓名和邮箱，然后将用户配置写入
`~/.config/reposteward/config.toml`。也可以显式提供身份：

```bash
uv run reposteward init \
  --login your-github-login \
  --git-name "Your Name" \
  --git-email your-github-login@users.noreply.github.com
```

配置文件只保存身份声明和运行偏好，不保存 GitHub token。RepoSteward 优先读取当前进程的
`GITHUB_TOKEN` 或 `GH_TOKEN`；未设置时使用 `gh auth token`。新用户默认添加 DCO sign-off，
但不强制 GPG/SSH 签名；需要签名时可在用户配置中设置 `sign_commits = true`。

默认 Harness 是 `codex-cli`。需要使用官方 Python Codex SDK 和可恢复 thread 时，安装可选
依赖并在用户配置中显式选择：

```bash
uv sync --extra codex-sdk
```

```toml
[agent]
harness = "codex-sdk"
```

SDK 适配器不会由项目级 `.reposteward.toml` 静默启用；缺少可选依赖时会明确失败，不会回退到
其他 Harness。

## 添加项目

在需要维护或贡献的项目目录运行：

```bash
uv run reposteward repo add owner/repository
```

维护自己的仓库时使用维护者模式：

```bash
uv run reposteward repo add owner/repository --mode maintainer
```

贡献者模式默认推送到用户 fork；维护者模式默认推送到原仓库的独立分支。提交前会通过 GitHub
API 验证当前身份确实拥有 push 权限，并继续禁止直接使用默认分支。

命令会创建本机专用的 `.reposteward.toml`，并自动加入该仓库的 `.git/info/exclude`，不会修改
仓库受版本控制的 `.gitignore`，也不会让配置混入后续 PR。随后需要填写该仓库允许的安装和验证命令。完整字段可参考
[`reposteward.example.toml`](../reposteward.example.toml)，现有复杂仓库适配示例位于
[`examples/tiammomo.toml`](../examples/tiammomo.toml)。

配置按以下顺序合并，后者覆盖前者：

```text
内置安全默认值
      ↓
~/.config/reposteward/config.toml
      ↓
项目目录最近的 .reposteward.toml
      ↓
显式命令参数
```

旧的 `starfix.toml` 仍可被发现，缺少 `config_version` 的旧配置按版本 1 兼容读取。新配置
默认把数据库和运行日志隔离到 `~/.local/state/reposteward/<GitHub host>/<login>/`，把临时克隆隔离到
`~/.local/share/reposteward/workspaces/<GitHub host>/<login>/`；支持 XDG 目录变量，Windows 使用
`LOCALAPPDATA`。这样不会在被维护的仓库中产生运行文件，也避免切换 GitHub 用户时混用记录。
旧配置显式指定 `state_dir` 时保持原有工作区布局。项目层不能覆盖用户层的运行目录、GitHub 身份、Agent executable 或 Runner
image；项目安全设置只能收紧用户限额和默认禁止路径，不能静默放宽它们。

少数仓库会把普通源码放在名为 `secrets` 或 `credentials` 的目录中。验证器默认仍拒绝复制这些
路径；可信的用户配置可以按仓库放行一个至少包含两级目录的规范化相对路径前缀：

```toml
[safety.tracked_sensitive_paths]
"makecindy/cindy" = ["apps/desktop/src/main/secrets/"]
```

该表只从用户配置读取，仓库项目配置中的同名表会被忽略。授权只适用于前缀下的已跟踪普通文件；
未跟踪文件、符号链接、`.env` 文件、仓库根级 `secrets`/`credentials` 前缀和包含通配符或父目录
跳转的路径仍会失败关闭。每个授权前缀都会写入验证沙箱清单，便于 Review 时核对。

### 并发与变更规模

默认情况下，每个仓库最多同时保留 4 个由当前 GitHub 账号创建的 open PR；Draft 和 Ready 都计入，
其他贡献者、closed 与 merged PR 不计入。容量只阻止创建或重新打开会增加 WIP 的 PR，不阻止向已有
PR 推送 repair、响应 Review 或重新验证。提交前会重新完整分页读取 GitHub；事实读取失败时不执行
push 或创建 PR，也不会调用 Harness。

用户可以提高本机容量，项目或单个仓库只能进一步收紧。默认单个变更最多涉及 40 个文件和 2,000 行
diff；高风险路径、隔离验证、Review 和身份门禁不会因容量提高而放宽：

```toml
[safety]
max_active_pull_requests = 8
max_files_changed = 80
max_diff_lines = 5000

[repositories."owner/repository"]
max_active_pull_requests = 6
max_files_changed = 60
max_diff_lines = 3000
```

上述配置的实际仓库上限为 6 个 PR、60 个文件和 3,000 行；仓库值即使高于用户值，也不能突破用户
上限。所有容量值都必须是正整数。容量门禁用于控制并行负担，不替代“一个 PR 只解决一个清晰问题”
的范围审查。

当某个仓库明确以“一个完整可验收能力”作为 Issue/PR 边界，可信用户可以只对该仓库关闭 diff
行数门。这个例外必须写在用户配置的仓库表中；项目配置中的同名值会被忽略：

```toml
[repositories."owner/capability-scoped-repository"]
unlimited_diff_lines = true
```

该值不使用 `0`、负数或哨兵整数，运行时把有效行数上限表示为 `None`，同时继续记录精确的新增和
删除行数。项目配置仍可设置一个正整数 `max_diff_lines` 来收紧该例外。文件数、活动 PR 数、禁止
路径、敏感文件、验证、Review、发布和合并门禁完全不变；关闭行数门也不意味着应把多个无关能力
放进同一个 PR。

## 准备 Issue 草稿

RepoSteward 可以在本地生成结构化 Markdown，并只读搜索相似 Issue：

```bash
uv run reposteward issue draft owner/repository \
  --title "Watcher 单轮重复读取同一轨迹" \
  --summary "轨迹数量较多时，单轮轮询会重复执行相同读取。" \
  --actual "每个 Atom 都重新读取完整轨迹。" \
  --expected "每条轨迹在单轮中最多读取一次。" \
  --reproduction "启动 watcher，并让一条轨迹包含多个 Atom。" \
  --acceptance "同一轨迹单轮只读取一次" \
  --language zh

uv run reposteward issue list
uv run reposteward issue inspect DRAFT_ID
uv run reposteward issue duplicate-check DRAFT_ID
```

草稿保存在当前用户和项目隔离的本地数据库中。`duplicate-check` 只调用 GitHub 搜索接口，
不会创建或修改 Issue。多人协作时，可以把草稿暂存为团队 GitHub Project 中的 Draft Issue：

```bash
REPOSTEWARD_ENABLE_ISSUE_STAGE=1 \
  uv run reposteward issue stage DRAFT_ID --submitted-by your-github-login
```

Project Draft Issue 是线上共享提案，不会出现在目标仓库的正式 Issue 列表。团队可以在线修改正文；
review 命令始终重新读取线上最新版本，并同时生成重复项快照、安全扫描结果和内容摘要：

```bash
uv run reposteward issue review PROJECT_ITEM_ID_OR_URL \
  --repository owner/repository
```

符合当前 reviewer 策略的维护者检查 Project 正文和所有潜在重复项后，使用该次 review 返回的精确
摘要进行转换：

```bash
REPOSTEWARD_ENABLE_ISSUE_PROMOTION=1 \
  uv run reposteward issue promote PROJECT_ITEM_ID_OR_URL \
  --repository owner/repository \
  --reviewed-by reviewer-login \
  --review-digest REVIEW_DIGEST \
  --duplicates-reviewed
```

线上正文、Project、重复项结果或目标仓库发生变化时，旧摘要失效，必须重新 review。默认配置
`require_distinct_reviewer = true`，禁止提案创建者自行转换；单维护者仓库可在用户配置的
`[issue_review]` 中显式设为 `false`，或在初始化时使用 `--allow-issue-self-review`，此时同一维护者
可以通过本地 CLI 审核并转换。该配置属于可信用户层，项目级 `.reposteward.toml` 不能覆盖。附带的
GitHub Actions 模板仍固定采用默认的团队第二人模式；无论采用哪种模式，最新 digest、重复项确认、
安全扫描、身份校验、环境开关和独立 promotion 都保持强制。检测到凭据或疑似安全漏洞时必须改用
私有报告渠道。
GitHub Actions 的人工审查与转换流程见 [`docs/github-actions.md`](github-actions.md)。
Project 页面 URL 里的数字 `itemId` 也可直接使用，因此不经本地 `stage` 而在线创建的提案同样可审核。

## 基本工作流

构建验证镜像并检查本地环境：

```bash
uv run reposteward image build
uv run reposteward doctor
```

发现和查看候选：

```bash
uv run reposteward discover
uv run reposteward discover --repo owner/repository
uv run reposteward list --all
```

批量维护时，可用只读收件箱聚合待审提案、本地运行、CI、Review 和合并检查入口；
该命令不会调用 Harness，也不会修改工作区或 GitHub：

```bash
uv run reposteward inbox --repo owner/repository --format text
```

Portfolio 只读取开放 PR；只有开放快照完整时，当一个 tracked submitted PR 已不在该快照中，
Inbox 才会在 RepoSteward 本地原生合并审计的最新终态精确为 `merged` 或 `already_merged` 时隐藏
该历史项目。Portfolio 读取失败或不完整，以及缺失、失败、未知或 closed-unmerged 合并结果仍显示
为 `refresh_required`；开放 PR 的新鲜在线事实始终优先。

需要跨进程、账号或 Harness 保存批量待办顺序时，可先把稳定控制面引用写入本地任务队列；enqueue
不会执行任务、调用 Harness 或写入 GitHub：

```bash
uv run reposteward queue enqueue owner/repository prepare --issue 123 --priority 20
uv run reposteward queue enqueue owner/repository follow-up --run-id RUN_ID
uv run reposteward queue enqueue owner/repository submit --issue 123 \
  --reviewed-by your-github-login --depends-on PREVIOUS_TASK_ID
uv run reposteward queue inspect --repo owner/repository
uv run reposteward queue inspect --task-id TASK_ID
```

同一仓库、动作、稳定引用、参数摘要和依赖的重复 enqueue 返回原任务；需要显式创建新一轮时传入新的
`--idempotency-key`。任务按 priority 和入队顺序 claim，只有前置任务 completed 后才可执行。失败会
区分可重试与 `manual_required`；瞬时错误按 5 秒起、最多 300 秒退避，并受 `--max-attempts` 限制。
人工修正后可以显式 retry，未运行或租约已过期的任务可以 cancel：

```bash
REPOSTEWARD_ENABLE_QUEUE_APPLY=1 \
  uv run reposteward queue retry TASK_ID --by your-github-login

REPOSTEWARD_ENABLE_QUEUE_APPLY=1 \
  uv run reposteward queue cancel TASK_ID --by your-github-login --reason superseded
```

执行队列需要独立开关；`submit`、owner attestation 和 `merge` 仍分别要求自己的原有开关与身份门禁，
队列不会代为开启：

```bash
REPOSTEWARD_ENABLE_QUEUE_APPLY=1 \
  uv run reposteward queue apply --repo owner/repository --limit 4
```

每个任务在慢 Harness、Runner 或 GitHub 调用期间由短 SQLite 事务续租，不持有数据库写锁。进程崩溃
后过期任务可由新 worker 接管，旧 generation 不能提交结果。任务只保存 work item/run/Issue/PR ID、
动作白名单和有界标量参数，不保存 prompt、token、评论正文或绝对工作区路径；attempt 审计不参与
普通 GC。

多个已提交 PR 可以先生成只读 merge-train 计划。计划复用同一次 Portfolio/Dependency 快照，输出
权威依赖顺序、非权威重叠分组、可并行预检集合、WIP 状态、逐 PR blocker 和稳定 batch digest；
Draft、缺失本地 submitted run、外部 head 变化及不完整事实都会失败关闭：

```bash
uv run reposteward batch plan owner/repository --max-parallel 6 --format text
```

审核该输出后，apply 必须带回精确 digest，并且只把计划写成串行 `batch-advance` 队列任务，不会立即
修改 workspace 或 GitHub。仓库级写入刻意串行，避免一个 PR 合并后其他 PR 继续消费旧 base：

```bash
REPOSTEWARD_ENABLE_BATCH_APPLY=1 \
  uv run reposteward batch apply owner/repository \
  --expected-digest BATCH_DIGEST --reviewed-by your-github-login
```

执行这些任务仍需显式开启队列和原有公开写入边界：

```bash
REPOSTEWARD_ENABLE_BATCH_APPLY=1 \
REPOSTEWARD_ENABLE_QUEUE_APPLY=1 \
REPOSTEWARD_ENABLE_SUBMIT=1 \
REPOSTEWARD_ENABLE_OWNER_ATTESTATION=1 \
REPOSTEWARD_ENABLE_MERGE=1 \
  uv run reposteward queue apply --repo owner/repository --limit 20
```

每个任务都会重新读取当前 PR。base 未变化时直接复用 owner attestation、merge decision 和 merge
executor；base 变化时在原 clean worktree 上执行确定性 Git replay，并用原验证命令重新进入
`adopt`/`submit`，不会调用 Harness。CI 尚未结束会进入有界退避，rebase 冲突会先 abort 恢复已验证
HEAD，再标记 `manual_required`，不会自动解决冲突或覆盖维护者改动。文件重叠只影响预检分组，不能
凭自身制造依赖；公开 push 和 merge 仍有各自的身份、租约、SHA 与 intent/result 审计。

检查贡献门禁并准备修复：

```bash
uv run reposteward gate owner/repository 123
uv run reposteward prepare owner/repository 123
```

`prepare` 会 clone 最新默认分支、运行 Codex、执行 allowlist 中的验证命令、检查 diff，并创建
本地 commit。Codex 和测试进程都拿不到 GitHub 凭据；验证阶段的容器没有网络、宿主目录或
Docker socket。

如果改动已经在外部工作区完成，可以使用 `adopt` 将现有 commit 纳入同一验证与审阅流程。

## PR 组合快照

在同时维护多个 PR 时，可以先生成仓库级只读快照：

```bash
uv run reposteward portfolio inspect owner/repository --format text
uv run reposteward portfolio inspect owner/repository --format json
```

命令会完整分页读取全部开放 PR，再汇总每个 PR 的 head/base、Draft 状态、changed files、diff
规模、required checks、Review decision 和未解决会话。文件重叠通过倒排索引构建，不会逐对重新扫描
所有 changed files。任何权限不足、分页不完整或采集期间的状态变化都会把快照标记为不完整，并在
结果中保留对应 PR 和错误原因。

每个快照都有基于规范化事实生成的稳定 SHA-256。需要在后续动作前检查事实是否仍未变化时，可传入
上一次摘要：

```bash
uv run reposteward portfolio inspect owner/repository \
  --expected-digest SNAPSHOT_DIGEST \
  --format text
```

该命令不调用 Harness、不修改 workspace、不持久化快照，也不执行任何 GitHub 写操作。JSON 适合
自动化消费，文本输出只展示紧凑的人类审阅摘要。

需要表达权威依赖时，在 PR 正文中使用独立行；引用、代码块或普通句子中的相似文字不会生效：

```text
Depends on #123
```

生成依赖图、循环检测和确定性建议顺序：

```bash
uv run reposteward portfolio plan owner/repository --format text
```

PR 正文的显式声明和维护者确认属于权威边；changed-file 重叠只显示为无方向建议，不能单独阻止
Ready 或 merge。缺失、跨仓库、未合并或循环依赖会进入 `ready_blockers`；已经合并的前置 PR 会进入
`revalidation_recommended`，提示重新核验 base 和验证证据。`merge-decision` 同样读取当前 PR 的直接
依赖，未满足或无法完整确认时失败关闭。

正文声明绑定当前 head、PR 作者和稳定来源摘要，编辑历史仍由 GitHub 保存；`portfolio plan` 不把
外部正文复制到本地数据库，因此保持只读。只有维护者 confirm/revoke 会写入本地追加审计表。

当依赖不是由 PR 作者写入正文时，维护者可以追加一条只保存在本机审计数据库、并绑定当前 head 的
确认；撤销会追加新事件，不覆盖历史：

```bash
REPOSTEWARD_ENABLE_DEPENDENCY_ATTESTATION=1 \
  uv run reposteward portfolio dependency confirm owner/repository 124 123 \
  --reviewed-by your-github-login

REPOSTEWARD_ENABLE_DEPENDENCY_ATTESTATION=1 \
  uv run reposteward portfolio dependency revoke owner/repository 124 123 \
  --reviewed-by your-github-login

uv run reposteward portfolio dependency list owner/repository --pull-number 124
```

该操作要求 Maintainer same-repository 配置，并同时核对配置身份、GitHub token 身份和仓库 push 权限；
它不会修改 GitHub。PR head 改变后，旧确认自动失效并成为显式 blocker，必须重新确认或撤销。

## 审阅与日志

`prepare` 和 `adopt` 返回紧凑 Review Packet，其中包括 commit SHA、diffstat、风险、验证
状态、日志路径和 Agent token 使用量，不会默认携带完整测试输出。

```bash
uv run reposteward inspect RUN_ID
uv run reposteward logs RUN_ID
uv run reposteward logs RUN_ID --command 1 --tail-chars 12000
```

验证日志默认保存在用户状态目录的 `runs/RUN_ID/verification/`。通过命令在数据库中保留最后
2,000 个字符，失败命令保留最后 12,000 个字符；更完整的日志文件有 2,000,000 字符上限，
并记录原始长度和 SHA-256。

RepoSteward 会从 Codex CLI JSONL 或 Codex SDK turn result 中提取输入、缓存输入、输出和推理
token，并记录工具调用次数；CLI 适配器还记录事件流大小。资源预算告警会出现在 Review Packet
中，但不会绕过验证。

## Work-item 生命周期轨迹

按仓库与 Issue 读取本地生命周期事实：

```bash
uv run reposteward trace owner/repository 40 --format text
uv run reposteward trace owner/repository 40 --format json --limit 200
```

Trace 使用版本化 JSON 契约，把同一 work item 的 successor runs、Context Pack、Checkpoint、
Harness 摘要、验证、租约、队列、发布、GitHub PR 事件与合并审计按稳定顺序聚合，并生成稳定的
`trace_digest`。它不联网、不调用 Harness，也不修改 Store、workspace 或 GitHub；输出只保留
白名单字段和摘要，不包含原始 Prompt、命令或日志正文、凭据、原生会话 ID、绝对 workspace 路径
及 token 计数。

默认最多返回 200 个事件，`--limit` 允许 1 到 500；文本渲染另有 50 个事件和 12,000 字符上限。
被数量或文本边界裁剪的事实会进入 `stats` 与各 `sources[].omitted_records`，不会静默丢失。
旧运行或尚未进入发布/合并阶段时，无法可靠关联的来源显示为 `unknown` 或 `incomplete`，不能把
缺失事实解释成零事件。`current` 单独保留最新 run、HEAD/base、验证状态和 checkpoint 引用，
不会随历史事件数量上限一起丢失。同一秒创建多个 run 时，以本地插入顺序确定最新记录。
`next_action` 根据该 run 的状态推导；只有绑定同一 run 与精确 HEAD 的原生合并终态才表示
`complete`，尚未完成的合并意图提示 `reconcile_merge`。这些结果是本地事实，不替代发布前的
远端新鲜度检查。

Checkpoint 中的自由文本下一步不直接输出，只有已知控制面动作码可以展示，其余显示 `unknown`。
Checkpoint 来源标记为 `derived_review_required`，导入来源标记为 `imported_untrusted`。
精简文本保留 `passed=False`、`eligible=False` 与零计数，避免省略影响判断的结果。

## 生命周期用量与成本

每次 `prepare` 和 `repair` 的 Harness 执行完成后，RepoSteward 都会追加一条有摘要保护的紧凑
用量事件。事件只保存 token、工具调用、持续时间、会话恢复结果和上下文裁剪原因等有界计数，不保存
原始提示、模型响应、告警文本或日志路径。既有数据库会无损升级；升级前没有采集到的指标显示为
`unknown`，不会按零计算。

按 Issue、PR、阶段、Harness、模型或日期查看机器可读汇总：

```bash
uv run reposteward usage report owner/repository
uv run reposteward usage report owner/repository --issue 40 --group-by stage
uv run reposteward usage report owner/repository --pull-number 41 --include-runs
uv run reposteward usage report owner/repository \
  --since 2026-08-01 --until 2026-08-31 --group-by model
```

原始用量不依赖价格配置。需要估算成本时，在用户配置中维护带生效日期的每百万 token 单价；这些
数据不会接受仓库内 `.reposteward.toml` 覆盖：

```toml
[[observability.prices]]
harness = "codex-sdk"
model = "your-model"
effective_from = "2026-08-01"
currency = "USD"
input_per_million = "1.00"
cached_input_per_million = "0.10"
output_per_million = "4.00"
# reasoning_output_per_million = "4.00"
```

同一 Harness 会优先匹配精确模型，也可用 `model = "*"` 设置兜底价格。新生效日期不会重算旧运行；
缺少适用价格或必要 token 指标时，该次成本保持 `unknown`。如果配置推理输出单价，它会替代输出
token 中推理部分的普通输出单价。一次查询超过 10,000 条运行时会要求缩小过滤范围。成功的 merge
结果也会携带对应 PR 的 `usage_summary`，方便把实际交付与生命周期成本关联起来。

## RepoStewardBench 指标评测

RepoStewardBench v0 把已有安全、上下文、项目管理、恢复和规模不变量组织为独立的离线评测套件。
它不读取项目配置、不访问网络、不调用 Harness，也不修改 workspace 或 GitHub。完整运行并原子写入
机器可读报告：

```bash
uv run reposteward benchmark run --output .artifacts/benchmark.json
```

报告使用 `benchmark-report-v1` schema，包含 suite/benchmark 版本、git SHA、Python 与平台环境、
逐场景确定性摘要、语义指标、耗时和分类汇总。默认每个场景执行两次；结果摘要变化会令场景失败。
标记为 critical 的安全与恢复场景组成 `hard_gate_pass`，任何场景失败都会令命令返回非零。耗时受
机器影响，只用于观察，不参与通过门槛。

可以缩小范围，或与先前保存的报告比较：

```bash
uv run reposteward benchmark run --category safety --category recovery
uv run reposteward benchmark run \
  --scenario context.events_10000_bounded \
  --repeat 3
uv run reposteward benchmark run \
  --baseline .artifacts/previous.json \
  --output .artifacts/current.json
```

基线比较报告新增失败、结果摘要变化及共同数值指标的 delta，但不会比较耗时。CI 应将报告作为构建
产物保存；仓库只提交稳定 fixture、schema 和语义门槛，不提交某台机器的绝对 timing 基线。

五类 v0 指标分别回答：

- `safety`：必要安全事实是否保留，不可信文本是否被误当成授权，以及只读路径是否越界；
- `context`：大事件流是否保持有界，以及 UTF-8 保守估算是否稳定；
- `management`：Inbox 对 gold attention set 的 precision/recall、Top-K blocker 召回和依赖顺序；
- `recovery`：中断 intent 是否只对账一次，旧 lease 是否无法继续写入；
- `scale`：1,000 个 PR 或依赖节点下的结果规模与完整性。

## 上下文与跨 Harness 交接

每次 `prepare` 或 `adopt` 都会创建一个持久 work item，并保存：

- 版本化 Context Pack：Issue 目标、基础 commit、仓库策略摘要、指导文件指纹和信任来源；
- Checkpoint：当前 HEAD、已完成工作、技术决定、验证证据、风险和精确下一步；
- Harness run：本次使用的 harness、模型和可选原生 session ID。

查看或导出可移植上下文：

```bash
uv run reposteward context inspect RUN_ID
uv run reposteward context export RUN_ID --output handoff.json
uv run reposteward context import handoff.json
```

导出文件包含内容摘要和估算 token 数，不包含账号凭据。更换 Codex 账号、机器或 Coding
Harness 时，应从该文件重建上下文；即使原生 session 无法恢复，任务事实和验证证据也不会
丢失。当前内置实现包括默认的 `codex-cli` 和显式可选的 `codex-sdk`，Claude Code 和 DeepSeek
等实现可以通过统一 Harness 契约接入，无需修改 Pipeline。`codex-sdk` 会尝试恢复同一原生
thread；恢复失败时从 Context Pack 开启新 thread。同一个 work item 再次运行时，RepoSteward
会把最近的 Checkpoint 压缩进新的 Context Pack，并要求 Harness 对历史结论重新核验。

Context Pack、Checkpoint 和导出包采用 Draft 2020-12 JSON Schema，并在写入或导入时严格
校验未知字段、版本、关联身份、摘要和大小。导入操作幂等；如果本机已有更新过的 work item，
交接包只补充历史 Checkpoint，不覆盖本机的 Issue 快照。`bundle_digest` 用于发现传输损坏，
不代表签名或来源认证，因此导入内容仍按不可信输入处理。

## 项目级 Skills

RepoSteward 使用 `.agents/skills/<name>/SKILL.md` 保存可跨 Coding Harness 复用的维护流程。本仓库
提供的 `reposteward-maintainer` skill 覆盖 Issue 审核、聚焦 PR、CI/Reviewer 跟进和上下文交接；
`reposteward-branch-cleanup` skill 用于盘点已合并 PR 留下的远端分支，并在明确授权后清理精确
匹配的同仓库 head。状态机、凭据隔离、内容摘要、验证与已有 GitHub 公开写入门禁不会由 skill
放宽。

原生分支清理默认只输出计划，不执行删除：

```bash
uv run reposteward branch-cleanup plan owner/repository --format text
```

计划从 SQLite 中读取全部有界 submitted run、成功合并审计和未完成清理意图，只把当前
SHA 与已合并 PR head 精确一致、且该名称没有其他 PR 历史的非默认、明确未保护同仓库分支列为
`candidates`。`pending` 会优先对账；`absent` 与 `completed` 保持幂等；活动、共享、fork、关闭未
合并、已移动或事实不完整的分支进入 `retained`。确认候选和 `plan_digest` 后，删除仍需要仓库策略
显式设置 `branch_cleanup = true`、独立环境门禁、相同摘要和实际 GitHub 身份：

```bash
REPOSTEWARD_ENABLE_BRANCH_CLEANUP=1 \
  uv run reposteward branch-cleanup apply owner/repository \
  --expected-digest PLAN_DIGEST --reviewed-by GITHUB_LOGIN
```

apply 会验证配置、审核声明与当前 GitHub 身份一致及 push 权限，并逐分支重新读取仓库、保护状态、
完整 PR 历史和 head SHA。删除前先在 SQLite 追加绑定 Issue lease 的 `pending` 意图；删除通过宿主
SSH 身份和绑定已审核 SHA 的 Git `--force-with-lease` 执行，同时禁用仓库 hooks 并移除 token 环境
变量。结果不确定时再读取精确分支：已不存在记为 `reconciled_deleted`，仍存在则失败关闭，读回也
失败则保留 `outcome_unknown` 待下次 apply 只读对账，不会盲目重复删除。每次终态结果同步到追加
审计和 Checkpoint；分支清理失败或待对账不会回滚、覆盖已经成功的合并结果。

项目仍保留旧版 Python 脚本供旧安装兼容；原生 CLI 可用后不要混用其非持久化 apply 路径。

Context Pack v2 先建立最多 24 项的轻量技能目录，只保存经过清洗和长度限制的 `name`、
`description`、仓库相对路径、状态和内容指纹，不复制完整正文。目录会显式报告无效项和被截断的
数量；Harness 先按任务语义选择少量相关技能，再从工作区读取其完整 `SKILL.md`。因此切换 Codex、
Claude Code 或 DeepSeek 等实现时可以共享流程，又不会让每次调用承担全部技能正文的上下文成本。
超过目录上限时 Harness 会继续检查 `.agents/skills`，不会把“未进入目录”等同于“不存在”。

技能元数据和正文都属于仓库不可信输入。RepoSteward 不读取越出工作区的链接，frontmatter 最多
扫描 8 KiB，单个技能文件上限为 1 MiB；Prompt 中的目录值会保持在 JSON 边界内，技能不能放宽
凭据、网络或公开写入门禁。
历史 Context Pack v1 与 Bundle v1 仍可严格校验和导入，新生成的文档使用 Context Pack/Bundle v2，
Checkpoint 保持独立的 v1 协议。

## 提交与跟进

公开提交必须使用独立命令，并同时满足环境开关、GitHub 实际身份和人工审阅声明：

```bash
REPOSTEWARD_ENABLE_SUBMIT=1 \
  uv run reposteward submit owner/repository 123 \
  --reviewed-by your-github-login
```

默认创建 Draft PR。`--reviewed-by` 必须等于用户配置中的 GitHub login，API token 的实际登录
身份也必须一致。旧的 `STARFIX_ENABLE_SUBMIT=1` 暂时保留兼容读取。

`submit` 会在 push、创建、重开或更新已有 PR 之前追加一个绑定 run、branch、head、目标仓库和
操作者的持久意图，并在动作返回后追加规范化结果。若进程在远端成功后中断，下一次调用会先读取
远端分支或 PR 对账：精确匹配时只补写本地状态，确认未执行时才继续，身份或 head 冲突时失败关闭。
审计只保存稳定标识、摘要和有界结果，不保存 token、PR 完整正文或目标仓库内容。

提交后可以增量查看变化：

```bash
uv run reposteward follow-up RUN_ID
uv run reposteward repair RUN_ID
uv run reposteward merge-decision RUN_ID
```

第一次调用会把 PR、评论、Review、Review comment 和 checks 按稳定 ID 与内容版本写入事件表，
建立 run 水位并追加 Review Checkpoint；之后只返回水位后的新增或编辑版本。REST 分页会完整读取，
不再把每类前 100 条当成完整历史。GitHub 结构化事件是跟进事实，模型摘要只是可重建的派生信息；
评论和 Review 正文始终视为不可信数据，不会被自动执行。重复调用是幂等的，事件摄取与水位推进
分离，进程在生成 Checkpoint 前中断时不会丢失待处理事件。

Contributor fork 收到新的失败 check、Reviewer 正文或 diff 内行级评论后，可以对最近一次
`submitted` run 执行 `repair`。命令先按水位读取增量并做确定性范围判断：没有新增可执行信息、
只有成功 check，或只有指向当前 diff 之外路径的建议时不会调用 Harness。确需理解代码时，Harness
只收到统一 token 预算内的事件批次、相关 diff 片段和紧凑 Checkpoint，在原 worktree 修改；Runner 重新验证后生成新的本地 `ready`
commit。更新已有 PR 仍必须再次执行带 `--reviewed-by` 的 `submit`。准备后若 head、base、策略或
事件水位变化，旧修复会被拒绝。该流程不会自动回复、催促 Reviewer 或合并 upstream PR。

预算由用户配置中的 `[context].follow_up_max_tokens` 控制，默认 24,000。估算直接使用 UTF-8 字节数作为供应商无关的保守上界，
计算，不依赖模型供应商或原生会话；稳定 ID 只保留最新事件版本，相同内容摘要只注入一次。输出的
`context_plan.stats` 会记录预算前事件数、保留数量以及版本替换、内容重复、字段裁剪、预算裁剪和
diff 不可用等原因。安全阻塞、当前 head/base、失败 check 和阻塞 Review 属于强制事实；如果它们
与紧凑 Checkpoint 本身都无法放入预算，命令会明确失败，不会静默删除后继续调用 Harness。
Repair 在调用 Harness 前会渲染完整 Prompt，并把 Issue 正文、固定提示、指导路径、Context Pack
分片、增量事件、diff 片段和紧凑 Checkpoint 一并纳入同一预算。长 Issue 正文只在 Repair 输入中按
UTF-8 字节安全截断，初次 `prepare` 语义不变；若仍超限，会先移除可选 diff 和低优先级反馈，但
始终保留安全事实及至少一条可执行反馈。最终估算和裁剪数量记录在 `context_budget` 与
`context_plan.stats.final_prompt_budget` 中，无法容纳最小必要集合时明确失败。

失败 check 可以先用确定性 CI 诊断读取，而不立即把日志发送给 Harness 或盲目重跑：

```bash
uv run reposteward ci diagnose owner/repository 123
```

该命令完整分页读取目标 workflow run 的 job/step 元数据，只对当前失败 job 及同 job/platform 的
有限失败对照下载日志。当前失败日志和对照日志分别最多读取 24 个；下载使用不携带 GitHub
Authorization 的短期签名 URL 和 256 KiB Range 上限；日志先脱敏，
再提取最多 12 条错误片段生成稳定指纹，完整原始日志不会进入输出或本地数据库。比较范围固定为
同一 workflow run 的其他 attempt、同 PR 最近 12 个 run，以及当前 base SHA 最近 12 个 push run。
结果只在证据充分时标记 `introduced`、`inherited`、`flaky` 或 `infrastructure`；第三方 check、
日志不可读、比较不完整或矛盾时返回 `unknown`。命令不调用 Harness、不修改 PR，也不触发 rerun。

`merge-decision` 只读获取完整的 changed files、required checks、Review decision 和未解决会话，
再对照验证时冻结的 head、base、policy digest、规模上限及高风险路径生成确定性结果。每次调用都会
追加一条本地审计记录，但不会调用 Harness、修改 workspace 或执行 GitHub 写操作。内置 CI、权限、
Runner、数据库、依赖发布和安全风险规则不能被项目配置削弱；项目只能通过 `merge_risk_paths` 追加
需要人工合并的路径。

Maintainer 可以为单个仓库显式启用合并执行器；默认仍关闭：

```toml
[repositories."owner/repository"]
mode = "maintainer"
submission_strategy = "same-repository"
auto_merge = true
auto_merge_method = "squash"
# 单维护者自有仓库可显式启用；默认 false
owner_attestation = true
```

GitHub 不允许 PR 作者批准自己的 PR。对单维护者自有仓库，可以在 Maintainer、
same-repository 模式下显式启用 `owner_attestation`。它不伪造 GitHub Approval，也不使用 admin
bypass；Contributor、外部作者、非受管分支或要求独立 Reviewer 的 branch protection/ruleset
仍然拒绝。先等待 CI 完成并人工检查精确 diff，再单独追加声明：

```bash
REPOSTEWARD_ENABLE_OWNER_ATTESTATION=1 \
  uv run reposteward merge-attest RUN_ID --reviewed-by your-github-login
```

声明绑定 repository、PR、run、作者、受管分支、head/base、policy、diff、checks、Review、会话、
活动、依赖和仓库规则摘要。随后必须重新运行 `merge-decision`；任何绑定事实变化都会使旧声明失效。
仓库规则接口不可读、受保护分支的 classic protection 不可确认，或当前身份不是 owner/admin 且无
push 权限时均失败关闭。声明只写本地追加审计，且还需要下方独立的一次性 merge 开关才能执行合并。

执行器只消费一次指定的 eligible 决策，不会自己生成资格。先运行 `merge-decision`，人工检查返回的
决策和 `audit.id`，再显式执行：

```bash
REPOSTEWARD_ENABLE_MERGE=1 \
  uv run reposteward merge RUN_ID \
  --decision-id MERGE_DECISION_AUDIT_ID \
  --reviewed-by your-github-login
```

命令会核对配置身份、token 实际身份和仓库 push 权限，并在写入前两次读取完整 PR 活动与合并快照。
任何评论或 Review 编辑、check、会话、head/base、策略、规模或风险变化都会使旧决策失效；请求还会
绑定精确 head SHA。网络结果不确定时先回读 GitHub，已在相同 head 合并则作为幂等成功，否则失败
关闭。每次尝试的意图和结果都追加到本地审计。Contributor mode、高风险路径、超限 PR、后台轮询、
Reviewer 回复、伪造 Approval、admin bypass 和自动开启 GitHub auto-merge 均不在该执行器范围内。

本地占用可以按仓库、数据类别和时间范围只读查看：

```bash
uv run reposteward storage stats
uv run reposteward storage stats --repo owner/repository --since-days 30
```

输出分别给出逻辑载荷字节、验证日志缓存、隔离工作区和 SQLite/WAL/SHM 的实际文件字节。工作区
统计只读取文件系统元数据，不读取文件正文或跟随符号链接；跨仓库共享的内容寻址 Blob 会在各仓库
行中显示引用字节，并在说明字段中明确其可能重复计算，避免把逻辑引用误当成全局物理占用。

清理命令默认只生成精确计划，不写数据库或删除文件：

```bash
uv run reposteward storage gc --repo owner/repository
```

验证日志候选必须超过用户级 `cache_retention_days` 且已有终态 Checkpoint。工作区候选必须超过
`workspace_retention_days`，关联的所有 run 均到达终态 Checkpoint，并且 Git 工作区干净、提交已由
submitted run 或远端引用证明可恢复；活跃、未知、脏、未推送、路径异常和符号链接工作区都会保留。
原始 GitHub 事件正文没有默认期限；只有仓库显式配置 `event_payload_retention_days` 后，超过期限且
每个 run 水位都已覆盖的正文才进入候选。计划会列出每个候选、预计可回收字节及保留原因汇总，并
始终保护事件索引、Checkpoint、Task Queue、Publication Attempt、Merge Decision、Merge Execution
和 GC 审计。

实际应用需要命令参数和独立环境开关同时存在：

```bash
REPOSTEWARD_ENABLE_GC=1 uv run reposteward storage gc \
  --repo owner/repository --apply
```

apply 前会追加 `applying` 审计；每个工作区在删除前会重新扫描并核对目录身份、快照、HEAD 和 run
状态，删除后再追加 `completed` 审计。中断后可从未完成记录识别并安全重跑。SQLite Blob 删除释放
的是可复用数据库页，不会自动执行 `VACUUM` 或承诺立即缩小数据库文件。

## 安全约束

- GitHub 凭据不会传给 Agent、测试、仓库 hooks、Git push 或 Docker 容器。
- Issue、仓库内容、评论和 Review 正文都视为不可信输入。
- CI 日志先限长和脱敏，只保存或输出可解释的有界片段与摘要。
- 安全报告、已认领 Issue、竞争 PR 和仓库贡献门禁可以阻断流水线。
- `.github/workflows`、凭据路径和超出配置限额的 diff 默认不可提交。
- 依赖安装可以在无凭据容器中联网，实际验证命令在 `--network none` 下运行。
- `run` 最多自动准备候选，永远不会自动执行 `submit`。

## 参与开发

功能、性能和行为变更应先通过 Issue 明确问题、证据和验收标准，再以聚焦 PR 落地。开始前请
阅读 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。安全问题请按 [`SECURITY.md`](../SECURITY.md) 私下报告，
不要创建公开 Issue。

## 名称迁移

项目原名为 Starfix。由于 PyPI 已存在活跃的 `starfix` 包，且 GitHub 上已有同名开发工具，
公开产品改名为 RepoSteward：Python distribution 和 CLI 均使用 `reposteward`，建议 GitHub
仓库使用 `repo-steward`。旧状态目录和 `starfix.sqlite3` 数据库仍会被兼容读取。

## 关联已经在本地开发的项目

在项目 clone 或 worktree 中执行 `reposteward project link .`，即可登记本机
工作区。命令返回稳定的项目 ID 和工作区 binding ID。相同 GitHub 仓库的多个
clone/worktree 共用项目身份，各自保留工作区身份；子目录会解析到 Git 根目录。

```bash
reposteward project link /path/to/project --name 我的项目
reposteward project inspect /path/to/project
reposteward project list --limit 50
reposteward project unlink <binding-id>
```

`inspect` 检查关联是否仍匹配，并显示当前 HEAD、分支和 dirty 状态。移动目录后
可以重新关联新路径；旧路径会在列表中显示缺失。替换目录或修改 origin 指向后，
需要明确解除旧关联再重新关联。`unlink` 只解除本地关联，不删除源码或 Git 历史。
这些命令不连接 GitHub，不启动 coding agent，也不发布任何内容。

登记保存在用户 state 目录的 `projects.sqlite3`，工作区路径不进入可移植任务包。
关联身份与仓库执行策略分别配置：`repo add owner/repository --mode maintainer`
生成维护者策略，其新配置的 `min_stars` 默认为 0；已有策略中的显式值保持不变。
Contributor 模式仍默认 1000。执行验证前仍需设置命令白名单。

## 已读取反馈与已处理反馈

`follow-up` 的事件水位只表示已经记录并查看了线上活动。`repair` 会另外查询同一
PR 尚未处理的反馈，所以先查看后修复、分批压缩和 successor run 都能继续处理余项。
读取结果中的 `pending_feedback` 提供状态计数、缺失载荷和查询省略数。

反馈按原始事件版本登记一次。`pending` 是未处理，`deferred` 保留超预算或当前 PR
范围外的事项；新版本通过 `superseded` 关联旧版本。只有本地修复产生新 commit、
验证通过且 run 保存为 ready 后，最终提示中选中的意见才会同时记为 `verified`。
这里的 verified 表示该批修复通过本地验证，后续仍需审核代码并确认是否满足反馈。
模型自述完成、读取水位或失败尝试都不能独立产生这一状态。

修复使用所选反馈的完整正文。最小完整事项无法放入预算时会明确报错，需要增加
`context.follow_up_max_tokens` 或人工细分；不会截掉意见末尾再将其确认完成。
范围外建议仍可查询，不会挤占同范围修复预算。未处理载荷有 GC 引用保护；载荷已经
缺失时报告 unknown，不能把缺失正文当成空意见或已完成事项。

Store schema 18 迁移保守地重放历史反馈：旧水位没有处理证明，不据此自动完成旧意见。
状态在 PR 范围内共享，successor run 不创建重复事项。ready 状态与该次处理证据在
同一个数据库事务中保存，失败或崩溃不会只确认一半结果。
