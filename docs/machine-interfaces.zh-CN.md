# Agent 可读取的接口契约

跨 CLI、MCP、HTTP、A2A 的交付范围与持久文档版本见
[协议与兼容性索引](protocol-map.zh-CN.md)。本页详述主线已有的 CLI/MCP 机器接口。

`reposteward capabilities` 离线列出当前安装的命令、数据 schema 和 MCP 输入/输出
契约。它不加载账号配置、不检查认证、不创建数据库、不启动客户端。依赖可用与
客户端实机通过分别表示；未实现的协议明确报告不支持。

CLI 保留默认输出。自动化程序可在命令前显式选择版本 1 的 JSON envelope：

```sh
reposteward --json-envelope capabilities
reposteward --json-envelope version
reposteward --json-envelope task inspect RUN_ID
```

结果为 `{"schema_version":1,"data":...,"error":null}`；参数错误和执行异常为
`{"schema_version":1,"data":null,"error":{...}}`。机器模式向 stdout 输出 JSON，
错误不回显原始异常文字；默认模式仍保留原有 stderr 诊断。`error` 包含稳定的
`code`、`message`、`retryable`、`next_action` 和 `cli_exit_code`。

退出码保留原语义：0 表示成功，1 可表示诊断未通过，2 可表示错误或计划不满足
条件。`error=null` 表示命令返回了有效数据，并不表示其中 `eligible`、`passed`
等业务判断为真；调用者必须同时检查退出码及业务字段。

机器模式强制已有格式选项使用 JSON。帮助、`--version`、长驻 `web`、`mcp serve`
和直接输出子进程日志的 `image` 不支持 envelope，会在执行前拒绝；使用不带
机器选项的帮助，或机器模式的 `version` 子命令。选项须放在子命令之前。

所有错误默认 `retryable=false`。客户端先根据 `next_action` 读取当前事实。
错误码不授予重放写操作的权限；已有幂等键、审阅摘要和独立发布门禁继续生效。

MCP 保留六类工具的既有成功结果，同时声明输出 JSON Schema，并在服务端检查
返回结构。错误同时提供 `isError`、结构化错误和旧客户端可读取的文本。
输出 schema 保证稳定的路由字段并允许追加业务字段；不是整个持久化模型的冻结。
Context Pack/Checkpoint 使用自己的版本；MCP 协议版本由 SDK 协商，不能用 CLI
envelope 版本或数据库版本替代。

官方依据：[MCP 工具与输出 schema](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。
这项交付不提供持久异步 MCP Tasks 或 A2A；后续接入将复用应用服务，分别声明能力。
