# 源码目录与模块归属

仓库目录 `RepoSteward/` 是整个项目，包含文档、测试、构建配置和源码；
`src/reposteward/` 是安装后由 Python 导入的包。两者同名是标准的 **src layout**，
不是嵌套了两个项目。保留这层隔离，可以避免源码目录意外替代安装包被导入。
命令名、发行包名和 Python 包名都保持 `reposteward`。

包根目录只保留 `__init__.py`（版本及元数据）和 `cli.py`（命令入口）。
入口仍是 `reposteward` 和 `python -m reposteward.cli`，MCP 与插件的启动配置不变。

```text
src/reposteward/
├── __init__.py
├── cli.py
├── core/           # 配置、协议类型、机器契约、安装与诊断
├── agents/         # coding harness 及 Codex 适配
├── context/        # 上下文包、预算及修复提示
├── evaluation/     # 确定性基准与交接评测
├── github/         # API 客户端、Issue 发现与提案
├── workflows/      # 流水线、生命周期、策略与审阅
├── maintenance/    # PR 组合视图、CI、合入与分支清理
├── projects/       # 项目登记、源码索引、理解与知识
├── tasks/          # 外部任务、工作项及终态记录
├── verification/   # 隔离验证、执行回执与中断恢复
├── storage/        # SQLite、迁移、快照与工作区存储
├── plugins/        # 插件包导出及完整性诊断
├── integrations/   # MCP 传输、连接配置及客户端接续
├── telemetry/      # 用量记录与归一化
├── web/            # 本地工作台、维护总览与静态资源
├── data/           # 随包发布的数据与技能模板
└── schemas/        # 版本化 JSON Schema
```

## 从功能找到入口

| 要理解或修改的行为 | 先读 |
| --- | --- |
| 命令参数、命令分发 | `cli.py` |
| 配置覆盖、安装诊断、能力发现 | `core/config.py`、`core/runtime.py`、`core/capabilities.py` |
| Issue 到 PR 的流程和公开写入门禁 | `workflows/pipeline.py`、`github/issues.py`、`maintenance/merge.py` |
| 已有项目关联与代码阅读 | `projects/registry.py`、`projects/understanding.py` |
| coding agent 任务接续 | `tasks/external.py`、`context/pack.py` |
| 验证及恢复未知结果 | `verification/external.py`、`verification/recovery.py` |
| MCP 接入或插件导出 | `integrations/mcp.py`、`plugins/bundle.py` |
| 本地可视化维护 | `web/server.py`、`web/workbench.py` |

路径相对本包；相应测试仍位于仓库根 `tests/`，按行为命名。

## 新代码如何放置

- 优先放入负责该行为的子包；只有程序入口和包元数据放在包根。
- `__init__.py` 保持轻量，不批量导入模块或触发配置、数据库和网络访问。
- 跨包显式导入实现模块，例如 `from reposteward.projects.registry import ProjectRegistry`。
  不增加同名转发文件、通配导出或动态导入别名来保留旧的平铺结构。
- 不为每个类再建一层目录，也不把领域逻辑塞入通用 `utils`；同一能力的服务、回执和恢复模块可放在一起。
- 凭据、状态及公开写入仍由原有服务和门禁控制；移动文件不改变服务职责和权限。
- 随包资源通过 `importlib.resources.files("reposteward")` 定位。插件运行时摘要递归覆盖
  所有子包的 Python 源码，忽略字节码缓存。

这是内部实现路径的调整，旧的 `reposteward.config` 等内部 Python 导入路径不再提供。
现有 CLI、机器响应、MCP 工具、插件启动命令、配置和数据库格式保持不变。
持有旧安装的插件继续使用其原安装；更新后需要重新导出插件包，不能复用旧运行时摘要。

## 尚未合入的功能分支

FastAPI/React、队列、导入和 A2A 等独立分支应在发布前合并此重构，保留各自 Issue 范围。
Git 可以识别大多数文件移动；仍需更新新增的 import、测试 mock 字符串和构建脚本中的路径。
工作台 HTTP 代码归 `web/`，项目导入归 `projects/`，持久操作归 `tasks/`，
A2A 与 MCP 传输归 `integrations/`，队列及迁移归 `storage/`。
更新后重新生成 HTTP 类型并验证实际发布 HEAD，不能沿用旧目录下的测试回执。
