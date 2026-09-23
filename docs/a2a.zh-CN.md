# 本地 A2A 项目理解服务

RepoSteward 的 A2A 入口委派**一个已关联工作区的项目理解报告**。它采用稳定的
A2A 1.0 HTTP+JSON 绑定和官方 Python SDK 1.1.0 的协议模型，复用 CLI/MCP 的
[持久操作服务](assistance-operations.zh-CN.md)。报告完成表示阅读路线已产出，
不代表开发任务完成或 PR 已交付。

安装时选择 `reposteward[a2a]`，先关联并显式扫描工作区，再启动服务：

```sh
reposteward understand scan /path/to/project
reposteward a2a token --output ~/.config/reposteward/a2a-token
reposteward a2a serve --workspace /path/to/project --token-file ~/.config/reposteward/a2a-token
```

启动输出 `http://127.0.0.1:PORT/.well-known/agent-card.json`。服务固定监听本机；
本版不提供互联网部署、多个租户或任意仓库导入。令牌文件由当前用户持有、权限为
0600 且放在仓库外；它是独立随机令牌，不使用 GitHub 凭证。文件不会被覆盖，也不
会向终端打印令牌。更换文件后重新启动服务使新令牌生效。

Agent Card 可在本机读取，业务请求需要 `Authorization: Bearer <local-token>`
和 `A2A-Version: 1.0`。不支持的版本返回标准错误，不降级解释；省略版本按协议视为
0.3，因此被拒绝。版本也可以使用同名查询参数，HTTP 头优先。

| 接口 | 行为 |
| --- | --- |
| `POST /message:send` | 委派报告；默认等待终态，`configuration.returnImmediately=true` 立即返回任务 |
| `GET /tasks/{id}` | 查询同一作用域的报告和来源 artifact |
| `GET /tasks` | 按 context、状态和更新时间过滤，分页返回；默认省略 artifact |
| `POST /tasks/{id}:cancel` | 取消排队任务，或请求正在执行的任务停止 |

发送一个 `ROLE_USER` 消息，`messageId` 使用稳定的幂等标识（1–120 个字母、数字、
点、下划线、冒号或连字符）。文字 part 是关注点；JSON part 接受 `focus`、`mode`
（`maintainer` / `contributor`）和 `limit`（1–20）。示例请求体：

```json
{
  "message": {
    "messageId": "onboarding-1",
    "role": "ROLE_USER",
    "parts": [{"text": "入口、模块边界和关键测试"}]
  },
  "configuration": {"returnImmediately": true}
}
```

相同幂等请求返回同一个操作；改变来源快照或参数时需用新请求。服务启动一个仅领取
报告的 worker，验证操作仍由显式 CLI worker 执行。客户端断开后任务继续，重启服务
也可接续排队任务。A2A task ID 等于本地 operation ID，CLI/MCP/工作台可交叉查询。

`contextId` 是工作区绑定的作用域标识，服务自行给出。既有报告可查询，但暂不支持
向 task 继续发送多轮消息、文件/URL 输入、流式事件、推送通知和扩展 Agent Card；
未实现的能力均明确拒绝，Card 不宣称支持。

列表支持 `pageSize`（1–100，实际可少于请求）、`pageToken`、`contextId`、`status`、
`statusTimestampAfter`、`includeArtifacts`。返回值按更新时间及 ID 降序排列；游标
绑定查询、工作区和本地令牌。分页是实时视图，期间状态变化可能移动条目，需按任务
ID 去重。`historyLength` 非负，但本版不保存对话历史，因此返回零条历史消息。

取消是请求，不保证瞬间终止。执行器已生成结果时保留真实完成结果；终态任务拒绝
取消。Artifact 包含固定索引摘要、扫描时间和代码引用，属于历史源码依据；后续源码
变化不会改写历史 artifact。需要最新报告时重新显式扫描并发起新任务。

协议依据：[A2A 1.0 定义](https://github.com/a2aproject/A2A/blob/v1.0.0/specification/a2a.proto)、
[官方 Python SDK 1.1.0](https://github.com/a2aproject/a2a-python/releases/tag/v1.1.0)。
