# 检查、备份与升级本地台账

新版本需要更新任务数据库时，可以使用显式维护命令。先让使用同一台账的 CLI、MCP
和队列写入者正常退出，再确认安装与实际状态目录：

```bash
reposteward version
reposteward doctor --local --expect-state-dir /absolute/path/to/state
reposteward state plan --expect-state-dir /absolute/path/to/state
```

`plan` 不创建缺失数据库，不初始化 Store，不认证 GitHub。输出当前/目标 schema、
所需迁移、活动租约数量、文件身份与内容摘要及 `plan_digest`。检查 `eligible` 和数据库
状态；计划命令能成功输出信息，并不代表可以升级。活动日志、文件变化、超过检查上限
或不可读台账会保留为未知。任务库检查上限为 2 GB；过大台账需要独立维护安排。

检查计划后，将输出中的精确摘要传给独立升级命令：

```bash
reposteward state upgrade --expect-state-dir /absolute/path/to/state --plan-digest REVIEWED_DIGEST
```

作用域、文件内容或身份、迁移语句、版本与活动租约变化会使计划不可用。执行时再次
取得写锁并核对计划；锁保护数据库写入，不代替停止长期客户端的操作。旧客户端可能
已加载旧数据模型，不应在升级后继续运行。

备份保存在该状态目录的 `backups/唯一ID/`，包含一致性的 `reposteward.sqlite3` 和
`manifest.json`。备份使用 SQLite backup API，在持有写锁期间通过独立只读连接复制，
核验完整性与 SHA256 后才开始迁移。迁移复用现有 Store 语句和兼容处理，在单个事务
内完成；中途失败回滚所有尚未提交版本。原 Store 写入口的兼容行为保持不变。

在 POSIX 上，新备份目录为 0700，文件为 0600；其他平台需将状态目录放在仅当前用户
可访问的位置。备份包含任务内容，应留在本地用户目录，不放进目标仓库或编码会话。
本命令只迁移任务数据库；项目注册表、配置和证据文件仍需按其用途单独保留。

## 查询结果与演练恢复

```bash
reposteward state inspect-backup /absolute/path/to/state/backups/ID
reposteward state plan --expect-state-dir /absolute/path/to/state
```

`inspect-backup` 核对格式、内容摘要、原 schema 和 SQLite 完整性，不修改日常状态。
校验成功说明备份与本地记录一致，不代表备份来自一个经过签名认证的外部来源。

manifest 的 `upgraded` 表示升级与结果记录完成，`rolled_back` 表示本次没有提交；备份
准备失败可能保留一个不完整的备份目录。`committing`、`commit_outcome_unknown` 或
`upgraded_record_incomplete` 需要结合当前 schema 与备份检查对账，不能盲目重跑。当前
schema 已到目标版本时，旧计划被拒绝，不重放迁移。

恢复演练应复制经核验的备份到单独目录，以匹配原 schema 的安装检查数据。升级后若
已有新任务或反馈写入，覆盖旧备份会丢失这些新增内容，所以此功能不提供无条件覆盖
日常数据库的回退命令。必须先停止写入、保存当前状态并核对增量，再决定恢复方式。

一次成功的 schema 升级与 SQLite 完整性检查不等于所有项目的使用验收；重新启动
已升级客户端后，还需核对关联项目、代表任务、上下文与验证证据。

原理参见 [SQLite backup API](https://www.sqlite.org/backup.html) 与
[事务语义](https://www.sqlite.org/lang_transaction.html)。
