# 协议、接口与兼容性索引

目录位置见[源码指南](source-layout.zh-CN.md)，端到端职责见[架构说明](architecture.md)。
本页回答：从哪个入口调用、由谁执行、使用哪个版本、哪些能力已经交付。

本页随主线能力交付更新；以实际安装的 `capabilities` 输出核对可用范围。源码、某个功能分支、安装包和客户端实际加载的
插件可能处于不同版本；分支通过测试不能证明当前安装已提供该能力。

## 先识别正在使用的安装

```sh
reposteward version
reposteward capabilities
reposteward --json-envelope capabilities
reposteward doctor --local
```

`capabilities` 不加载账号配置、不认证、不创建数据库；它列出该安装实现的接口与 schema。
`doctor --local` 检查配置、状态和运行时兼容性，不迁移数据库。
`dependency_available=true` 只表示依赖可导入；`client_health=not_probed` 不表示客户端已连接成功。
CLI、MCP 和插件固定的解释器可能不同，检查时须使用实际启动该入口的解释器或可执行文件。
进一步操作见[安装诊断](local-installation.zh-CN.md)、[插件接入](agent-plugin.zh-CN.md)
和[状态升级](state-upgrades.zh-CN.md)。

## 传输与调用边界

| 入口 | 当前源码状态 | 负责什么 | 代码与契约入口 |
| --- | --- | --- | --- |
| CLI | 已实现 | 参数解析、同步结果及显式维护命令；公开写入使用独立门禁 | [cli.py](../src/reposteward/cli.py)、[机器接口](machine-interfaces.zh-CN.md) |
| MCP | 已实现本地 STDIO | 绑定一个工作区，为 Agent 提供项目、上下文、证据、理解、检查点和验证六类工具 | [integrations/mcp.py](../src/reposteward/integrations/mcp.py)、[core/api_contract.py](../src/reposteward/core/api_contract.py) |
| 工作台 HTTP | 已实现本地查询与显式同步 | 同源会话、项目阅读、任务和维护信息；显式同步操作保留开放及终态 PR 事实；由 FastAPI 提供 HTTP 服务 | [web/server.py](../src/reposteward/web/server.py)、[web/workbench.py](../src/reposteward/web/workbench.py) |
| FastAPI / React / OpenAPI | 已实现 | 工作台服务与前端的类型契约和构建分发 | [web/api](../src/reposteward/web/api)、[frontend](../frontend)、[工作台指南](local-workbench.zh-CN.md) |
| 持久异步操作 | 待交付 | 持久 operation ID、进度、取消请求和恢复，由 CLI/MCP 复用应用服务 | [Issue #165](https://github.com/tiammomo/RepoSteward/issues/165)；不是已实现 MCP Tasks 的声明 |
| A2A | 本主线未实现 | 待交付实现面向限定工作区的项目理解报告委派 | [Issue #166](https://github.com/tiammomo/RepoSteward/issues/166)；检查当前安装的 `a2a.implemented` |

主线 MCP 不提供 GitHub 发布或合并工具；保存 Agent 检查点也不等于验证通过。
验证只能选择可信用户配置中已有的 profile，不能通过传输参数注入任意命令。
MCP 协议由 SDK 协商，其版本与 RepoSteward 的 JSON envelope、Context Pack、数据库版本独立。
当前 `mcp.durable_async_tasks=false`；普通工具中的异步业务操作也不能自动被称作 MCP Tasks。

A2A 报告委派、开发任务、验证 attempt 和 GitHub PR 是不同对象。报告生成完成，
不代表代码修改、测试或 PR 已交付；新增传输必须继续复用既有权限和证据边界。

## 持久文档与版本

以下数值描述当前源码。运行时权威是 [CURRENT_SCHEMA_VERSIONS](../src/reposteward/core/protocol.py)
和 `capabilities.schemas`；改变版本时应同步维护本表及对应兼容测试。

| 契约 | 当前写出/目标版本 | 兼容规则与用途 |
| --- | --- | --- |
| CLI JSON envelope | 1 | `schema_version` 标识响应封装；`error=null` 不保证业务字段 `passed` 或 `eligible` 为真 |
| Context Pack | 3 | `schema_version` 分派；v1/v2 仍严格可读，未知未来版本拒绝；冻结来源、任务契约与修复上下文 |
| Context Bundle | 3 | `bundle_schema_version` 分派；v1/v2 仍严格可读；校验摘要和跨文档关联，摘要不等于来源认证 |
| Checkpoint | 1 | 开发状态与证据引用；不把 Agent 声明提升为已验证事实 |
| Task Contract | 1 | Context Pack v3 内的任务契约，绑定精确 Issue 来源或该版本的显式审阅 |
| 任务台账 SQLite | 24 | [storage/store.py](../src/reposteward/storage/store.py) 定义目标；本机实际版本可能更旧，升级单独规划 |
| 项目注册表 SQLite | 1 | [projects/registry.py](../src/reposteward/projects/registry.py) 定义；与任务台账分开管理 |
| 插件导出收据 | 2 | [plugins/bundle.py](../src/reposteward/plugins/bundle.py) 写出的 `export.json`；不同于包版本、客户端 manifest 和 MCP 协议版本 |

JSON 文档使用随包发布的 [schemas](../src/reposteward/schemas)，持久化和导入入口校验未知字段、
来源摘要和 work item/run 关联。历史 schema 可读，不代表可以自动降级新任务契约。
Context Pack v2 引入技能目录；v3 在此基础上加入任务契约和修复反馈绑定。
修改版本字段不能代替数据转换，也不能绕过来源或审阅校验。

Checkpoint 的 `evidence` 上限仍为 128。原生 ready/failed 检查点超限时，优先保留提交、
失败验证及其他验证，留下最多 127 条原证据和一条 `evidence_manifest`。清单包含完整序列
摘要、总数、省略数及失败计数；原始详情仍在 run 记录。清单摘要不能还原被省略内容，
跨机器交接若需要这些详情，必须另行取得并核验，不能把紧凑 Bundle 当作完整日志归档。

## 修改接口时的检查入口

| 修改内容 | 先核对的现有测试 |
| --- | --- |
| Context Pack / Bundle / Checkpoint | [test_protocol.py](../tests/test_protocol.py)、[test_task_contract.py](../tests/test_task_contract.py) |
| CLI envelope、错误码和能力发现 | [test_api_contract.py](../tests/test_api_contract.py)、[test_cli.py](../tests/test_cli.py) |
| MCP 输入、输出、作用域和真实传输 | [test_mcp_bridge.py](../tests/test_mcp_bridge.py) |
| 工作台 HTTP 边界 | [test_workbench_http.py](../tests/test_workbench_http.py) |
| 模块移动、安装资源及插件完整性 | [test_package_layout.py](../tests/test_package_layout.py)、[test_plugin_diagnostics.py](../tests/test_plugin_diagnostics.py) |
| 状态格式和运行时兼容 | [test_state_upgrade.py](../tests/test_state_upgrade.py)、[test_runtime_alignment.py](../tests/test_runtime_alignment.py) |

按照贡献指南在加固验证容器运行这些检查。新传输还须验证实际安装包，而不只从源码目录导入；
检查客户端发现能力、配置固定解释器、实际握手、重连及错误回执。
发布时分别记录“主线已合入”“安装已升级”“真实客户端已验证”，不能互相代替。

## 接下来如何收敛

工作台前置能力已交付，继续逐项发布 GitHub 同步、项目导入、扫描、持久操作与 A2A，
每项使用自己的 Issue、差异审阅和实际发布 HEAD 验证。合入时补齐本页对应的状态、
源码位置和契约，不将组合验收分支当作一个发布单元。
新增入口的薄适配层应把身份、输入和传输结果交给共享应用服务；业务规则、验证证据和
公开写入审计仍只有一套。源码分包后的进一步解耦边界见[源码指南](source-layout.zh-CN.md)。
