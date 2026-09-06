# 从独立安装开始使用 RepoSteward

日常维护时可以直接打开目标项目。RepoSteward 的安装环境、用户配置、台账和目标
工作区分别管理；开发 RepoSteward 本身时才需要进入它的源码 checkout。

## 固定安装来源

选择已审阅提交构建的 wheel，并保存提交 SHA 与 wheel 的 SHA256。当前包版本为
`0.1.0`，多个开发提交可能使用相同版本号，单独比较版本字符串不足以确认代码相同。
这里不假定 PyPI 已发布包含本文命令的版本。

在构建源码的目录执行 `uv build` 后，从绝对 wheel 路径安装到 uv 的独立工具环境：

```bash
uv tool install --python 3.12 /absolute/path/reposteward-0.1.0-py3-none-any.whl
uv tool update-shell
reposteward --version
reposteward version
```

`uv tool update-shell` 提示的 PATH 变化可能需要新开终端。已有安装时，先完成下文的
状态检查与备份，再使用 `uv tool install --force` 安装已核验的替换 wheel。工具安装
环境的替换不等于数据库回退；保留之前的 wheel 和状态备份。

要使用 MCP，在安装时明确选择同一个 wheel 的 `mcp` extra，例如：

```bash
uv tool install --python 3.12 'reposteward[mcp] @ file:///absolute/path/reposteward-0.1.0-py3-none-any.whl'
```

本地导览不要求 GitHub 登录、Docker 或某个特定 Agent 已登录；联网维护需要 GitHub
身份与仓库策略，独立验证需要 Docker/Runner，实际 Agent 会话使用其自身客户端认证。
CLI/MCP 的相关依赖及客户端实机验收状态见[已有 Agent 使用指南](coding-agent-assistance.zh-CN.md)。

## 检查当前安装与状态

`version` 不加载项目配置、不联网，输出包版本、Python、模块与安装位置；存在安装
元数据时还显示 editable 标记和合法格式的 VCS commit。普通 wheel 未提供源码提交时
`source_revision` 为 null，应与保存的构建记录核对。安装 URL 不进入输出。

```bash
reposteward doctor --local
reposteward doctor --local --expect-state-dir /absolute/path/to/effective/state
```

本地诊断只显示允许的配置字段及其加载来源：用户层、项目层、默认值。路径显示最终
生效值，包括命名空间；API 地址仅显示 origin，不显示凭据和查询参数。后续手工构造
或修改 AppConfig 的调用方应将来源视为加载时记录，而非另一次实时配置读取。

API URL 带 userinfo、查询或 fragment 时，本地诊断会先要求修正 URL，并停止输出由它
派生的路径；认证应通过单独的凭据来源配置。

期望状态目录不匹配时，诊断返回失败并且不读取那个目录的数据库。没有配置时返回
结构化说明；可通过 `reposteward init` 配置身份，或修复所选配置文件后重试。

| 数据库结果 | 含义与下一步 |
| --- | --- |
| missing | 尚未创建，可在确有需要时显式关联项目或创建任务 |
| compatible | 观察到的 schema 与当前安装一致，且基础表存在 |
| migration_required | 需要备份和升级；本次诊断没有执行迁移 |
| newer_than_supported | 当前安装太旧，先切换到能支持该 schema 的版本 |
| snapshot_required | 有未结算的 WAL/journal 或文件变化；先让写入者正常退出，再检查 |
| uninitialized / unrecognized / invalid_database / unreadable / unsupported_path | 空库、非预期台账、损坏、读取失败或链接路径；先检查来源或恢复备份 |

`compatible` 不代表所有业务行完整、认证成功、测试通过或 PR 可以合并。诊断使用
无副作用的稳定 SQLite 视图；遇到活动 WAL 时保守返回未知，不忽略 WAL 报告旧结果。
任务数据库与项目注册表分别检查。缺少数据库不创建目录；诊断不初始化 Store，不执行
迁移、Harness、Docker、认证命令或 GitHub 请求。需要完整执行环境检查时单独运行
既有 `reposteward doctor`。

## 在目标项目继续工作

```bash
cd /absolute/path/to/your-project
reposteward project link .
reposteward project inspect .
reposteward understand scan .
reposteward understand guide .
```

关联身份不会自动创建仓库维护权限。需要任务与交付时，再用
`reposteward repo add owner/repo --mode maintainer` 或 `--mode contributor` 配置对应
角色和仓库策略。用户已有的 clone/worktree 保持原位置。

按[任务接续指南](coding-agent-assistance.zh-CN.md)创建或选择任务，然后在该目标项目
打开平时的 Agent。`reposteward mcp config . --client codex|claude-code|copilot-vscode`
表示三个可选客户端之一，实际执行时替换为具体名称；它输出本机配置预览。修改策略、
更换安装或更新绑定后重启对应 MCP 服务。无 MCP 的客户端可使用 CLI 与文件接续。

## 升级前检查

先暂停会写入同一台账的 CLI、MCP 或队列进程，核对最终状态目录，使用 SQLite 的一致性
备份保存数据库，并保留关联注册表、用户配置及需要的证据文件。在副本演练新版本迁移
和必要的恢复核验，再切换日常安装。不要只复制活动数据库的主文件而忽略 WAL。

当前 Store 的既有写入口可能自动迁移旧 schema；`doctor --local` 不改变这一行为。
读取旧台账时先诊断，不要通过随意创建任务来探测版本。本文没有提供自动回退命令，
发生新写入后的回退需要核对新增数据，不能仅覆盖旧备份。

工具安装行为参考 [uv 工具管理文档](https://docs.astral.sh/uv/guides/tools/)。
