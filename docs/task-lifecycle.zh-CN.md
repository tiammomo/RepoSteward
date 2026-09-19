# 结束外部 Agent 任务

工作项表示要解决的问题，run 表示一次尝试。结束一次尝试不会修改其他尝试、
关闭 GitHub Issue、发布 PR 或清空历史检查点。`TaskLifecycle` 统一执行本地终态
操作；CLI 调用它，CLI/MCP/工作台继续通过共享任务查询读取结果。

## 预览与记录

取消不再继续的尝试：

```sh
reposteward task resolve-plan RUN_ID --outcome cancelled --reason '维护者决定停止此次尝试'
reposteward task resolve RUN_ID --outcome cancelled --reason '维护者决定停止此次尝试' \
  --plan-digest REVIEWED_DIGEST --reviewed-by YOUR_LOGIN --idempotency-key UNIQUE_KEY
```

计划只读、离线。检查 `eligible`、`reasons`、目标、检查点版本和摘要后，以相同
参数执行独立的 `resolve`。执行在同一短事务内重新检查计划、保存唯一结束记录并
更新尝试状态。没有网络调用、仓库代码执行或公开写入；它不需要当前开发基线仍然有效。

另两种结果需要 `--target-run-id`：

| outcome | 目标 | 必须满足的条件 |
| --- | --- | --- |
| `completed` | 原生交付 run | 同仓库、同 Issue，已有原生成功合并审计；合并 head 与此次外部任务最后一次干净检查点的 HEAD 一致 |
| `superseded` | 新的外部 run | 同一工作项、同一工作区绑定、同一配置账号，目标仍处于活动状态；不能引用自身或已结束的尝试 |
| `cancelled` | 无 | 必须给出原因；取消不代表已经完成目标 |

完成依据来自本地原生合并审计。手填 PR URL、Issue 已关闭、Agent 自述测试通过，
均不能作为完成凭证。尚无本地原生合并记录的第三方 PR，需要先通过后续的贡献者
接入/对账流程取得证据，本入口不会临时放宽要求。

## 状态与恢复

结束记录保存当时的账号、API 主机、状态目录、任务修订、快照和目标证据。
有在途或未对账的验证记录时拒绝结束，先通过验证流程确认其结果。
检查点、目标状态、配置作用域或完成依据变化后，旧计划失效。

网络与客户端不参与本地提交。若响应丢失，以相同幂等键、相同参数和原计划摘要
重试，可取回同一结束记录；换键或改参数不能覆盖已经结束的尝试。事务失败会同时
回滚结果和状态，不会留下半完成的本地写入。

`task inspect/context`、MCP `context` 和工作台展示 `resolution`。历史检查点的
`status` 仍描述当时的开发状态，尝试是否结束以顶层状态和结束记录为准。
终态上下文不再要求继续旧的未完成工作；原内容仍保留在历史检查点。
`task current` 和多项目 overview 不再把该尝试列为活动任务。

结束不会使当前工作区通过验证，也不会清除 `base_changed` 等适用性信息。
它仅确认这次历史尝试的处置结果。新增检查点和新的验证请求会被拒绝；旧请求
的幂等读取仍可返回既有记录。

## 数据兼容性

任务库 schema 23 新增 `external_task_resolutions`，保留原任务、检查点和证据。
计划、查询以及 resolve 的前置检查不会自动迁移旧库。请按
[显式备份与升级流程](state-upgrades.zh-CN.md)先停止旧 CLI/MCP 写入者，再执行升级。
旧插件可能固定引用旧解释器；仅升级日常 CLI 不会升级该插件。

升级后应让所有入口使用支持新 schema 的安装。回退解释器不等于回退数据；
不要以覆盖备份的方式丢弃升级后的新记录。
