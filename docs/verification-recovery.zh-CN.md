# 对账中断的验证

客户端查询可能发现验证 outcome 为 `unknown`，原因是执行进程退出却没有写回终态。
这时持久记录仍为 `running`，任务结束操作会继续阻塞。查询不隐式修改记录。

```sh
reposteward verification reconcile-plan RUN_ID VERIFICATION_ID --reason '核查中断执行'
reposteward verification reconcile RUN_ID VERIFICATION_ID --reason '核查中断执行' \
  --plan-digest REVIEWED_DIGEST --reviewed-by YOUR_LOGIN --idempotency-key UNIQUE_KEY
```

`VERIFICATION_ID` 是 `verification:` 后的 32 位 ID。计划只读，绑定记录、账号、
主机、状态目录、策略及执行观察。先检查 `eligible` 和 `reasons`。apply 持有原执行
锁，重新读取 Docker 状态，再在本地短事务中保存唯一审计和 `unknown` 终态。
相同请求及幂等键可取回同一回执；不同请求不能覆盖。

新验证会在容器启动前记录随机身份、Docker daemon 身份和容器名，并给容器加上
匹配标签。进程锁仍被持有、容器仍活动、daemon 改变、读取失败或缺少执行依据时，
对账保持阻塞。旧验证没有这些依据时需要人工核查；本入口不会制造历史凭证。
它不会删除容器或重跑测试。对账期间操作人应停止通过其他工具重启相关容器。

对账仅确认该次执行不再活动，不会把退出状态提升为测试通过。原快照、修订、
结果和日志保留，CLI/MCP 的 verification inspect 显示 `reconciliation`。
`unknown` 不具有发布资格；如仍需验证，应在当前有效任务快照上提交新的显式请求。

数据库 schema 24 增加独立回执表。升级前停止共享状态的旧客户端，按 `state plan`
和 `state upgrade` 完成备份与升级；只读命令不会自动迁移。
