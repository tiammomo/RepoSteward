# RepoSteward 自身的接续试点 · 2026-09-05

用户选择 RepoSteward 自身，任务为已审阅开放 [Issue #104](https://github.com/tiammomo/RepoSteward/issues/104)。这是同一开发任务的上下文恢复实验，没有让两个客户端各自实现功能。独立任务台账保留原始 Issue、三项未完成工作、一项决定及开发快照。

| 观测 | Codex CLI 0.153.0 | Claude Code 2.1.81 |
| --- | --- | --- |
| 真实会话 | 成功退出，125.557 秒墙钟耗时 | 195.432 秒后退出 1 |
| 原始 Issue | 从 source 证据取回六项验收要求 | 未执行模型调用 |
| MCP 操作 | context → evidence → checkpoint → context，四次完成 | 未取得成功调用证据 |
| 检查点 | revision 1 → 2 | 没有新检查点 |
| 未完成事项 | 三项全部保留，台账回读相同 | 未验证 |
| 决定 | 使用 RepoSteward 自身试点，理由和来源保留 | 未验证 |
| 后继客户端读取前任说明 | Codex 留下了接续说明 | 被服务端 429 阻断 |
| 客户端执行测试 | 没有 | 没有 |
| 公开写入 | 没有 | 没有 |

Codex 从完整源正文恢复了验收要求；该任务 contract 为 source_bound，因此结构化 acceptance_criteria 数组为空并不表示没有验收条件。下一客户端必须读取完整 source_requirements 或原始证据。

Claude Code 使用机器现有供应商与模型配置，服务返回“已达到 Token Plan 用量上限”（429，2056）。结果带有 is_error=true；外层 subtype 虽为 success，也不能据此判为成功。没有静默更换模型或服务。恢复现有服务额度后，需要重新读取同一任务的当前 revision，再继续保存和回读，不能重放旧 snapshot 身份。

人工接续分钟、重复解释次数、人工返工、配置分钟均未测量，记为未知。客户端墙钟耗时包含等待，不能当成人工时间。新增试点配置为独立的用户配置、项目策略和 Claude MCP 文件三份；Codex 的 MCP 配置经启动参数传入。初始化时用户状态目录覆盖了临时项目设置，已将试点记录迁出、恢复日常台账 schema 17，并核对完整性及既有业务表摘要；后续从用户层指定隔离目录，启动前检查生效路径。

离线 benchmark 首轮暴露两处新 fixture 接口误用（反馈 report 与 events 混用、VerificationResult 位置参数顺序），修订后须以容器结果验收。这属于评测脚手架修正，不能统计成产品带来的返工节省。真实代码验证结果单独保存在版本绑定的 verifier 证据中，不采用客户端自述。

本轮证明了 Codex 经 MCP 读取、保存与回读的链路，以及模型用量异常能够明确暴露。双客户端接续与人工维护收益尚未得到完整验证；不得据此宣称效率提升百分比、Claude 实机通过或 Copilot 已验证。

原始模型输出仅保存在操作者本地，摘要如下，便于对照证据：

- Codex stdout SHA-256：`cd6c8f6417e2d84fe263da1ec44258bc55488e9790015423cd4497a5ee5a7af7`
- Claude stdout SHA-256：`fa81dfeb217be89ad8d50cf7d18d42c118a7e0197b5e4363062bbf84db699796`
- 任务：`2912b3eba72c472989f787b1a07dc61e`，试点 HEAD：`da3599ae0ed8b18d74f0338d47dac5abd2dfa297`；dirty 快照：`bfe71c7a64354f6109dda3da0817894940f8ffff559a9b4af71a5df5662d1adc`。
