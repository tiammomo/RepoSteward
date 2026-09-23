# 可接续的本地操作

`operation` 为一个已关联工作区提供持久验证和项目理解报告，复用工作台的本地队列、
幂等请求、领取租约和执行记录。它不创建独立调度器，也不发布 Issue/PR。

```sh
reposteward operation --workspace /path/to/project start understanding --idempotency-key report-1 --focus "入口与测试"
reposteward operation --workspace /path/to/project worker --once
reposteward operation --workspace /path/to/project get OPERATION_ID
reposteward operation --workspace /path/to/project wait OPERATION_ID --timeout 10
```

理解报告需要此前显式执行过 `understand scan`，请求固定索引摘要；执行前源码或索引
发生变化则失败，提示重新扫描。保存的报告属于历史依据，不代表读取时的最新源码。
MCP 的普通 `operation` 工具提供 `start_understanding`、`start_verification`、
`get`、`list`、`wait`、`cancel`、`reconcile`，与 CLI 和工作台读取同一条记录。
**这不是实验性的 MCP Tasks 协议实现。** 不要求客户端支持该扩展。

验证请求必须包含 `run_id`、受信任的 `profile`、`expected_revision` 和
`expected_snapshot`；在 CLI 中使用相应的连字符选项。执行时重新核对作用域、
验证配置和快照；不能借此输入任意命令。测试失败属于已完成的验证操作，必须读取
结果中的 `outcome`。操作完成从不自动结束开发任务，也不提升 PR 发布资格。

Worker 是显式启动的独立进程，默认最多运行一小时，`--once` 只领取一次。
请求连接中断不撤销已经入队的工作；重新连接后按操作 ID 查询。`wait` 最多等待
30 秒，超时返回当前状态，不执行任务。持续服务可由用户自己的进程管理器托管。

取消使用最新 `revision` 和新的幂等键：

```sh
reposteward operation --workspace /path/to/project cancel OPERATION_ID --expected-revision REVISION --idempotency-key cancel-1
```

排队中的操作原子取消；运行中的操作先记录 `cancel_requested`，执行器确认停止后
才变成 `cancelled`。取消与完成竞争时保留实际结果。工作台同步 worker 只领取
GitHub 同步；作用域 worker 只领取绑定工作区的辅助操作。活动进程持有额外文件锁，
即使租约过期也不能并行启动同一个操作。

验证采用由操作 ID 确定的执行身份。Worker 丢失回复或重启时先读取原始验证记录，
不会重复运行已开始的验证。若原始记录仍为 `running`，先完成
[验证恢复审阅](verification-recovery.zh-CN.md)，再执行 `operation reconcile`
（同样需要最新 revision 与幂等键）并运行 worker，补记原有结果。`unknown`
仍然是未知，不变成通过；需要新的验证时创建新请求。普通“重试”不用于此类操作。

列表只返回概要；单项返回最近结果和有界尝试历史，省略数明确列出。数据库升级使用
`state plan` / `state upgrade`，服务不隐式升级旧库。开启前停止共享状态的旧客户端。
