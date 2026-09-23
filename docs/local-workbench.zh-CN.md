# 本地维护工作台

安装后运行：

```bash
reposteward web
```

用同一台机器上的浏览器打开终端打印的完整链接。默认选择可用端口；可用
`--port 8765` 指定端口，用 `--expect-state-dir /absolute/path/to/state` 核对作用域。
按 Ctrl+C 停止。网页、样式和脚本都随 Python wheel 安装，日常使用无需 Node 或源码
checkout。读取本地内容无需启动模型、Docker 或 GitHub 认证；显式同步使用后端
配置的 GitHub 账号。`web --read-only` 可关闭本地命令和操作恢复。

## 从项目进入一次任务

先使用已有命令准备本地事实：

```bash
reposteward project link /absolute/path/to/project
reposteward understand scan /absolute/path/to/project
reposteward overview refresh
```

`project link` 登记本地工作区，不授予仓库维护权限；已有 clone/worktree 保持原位置。
需维护或贡献时，再配置对应仓库策略。`scan` 显式更新代码索引，`overview refresh`
显式读取 GitHub 并保存缓存。也可进入项目的 **GitHub 维护** 页，点击 **同步 GitHub**。
网页中的“重新读取”只读取已保存的本地状态；同步操作在 **本地操作** 页持久记录。
完整来源与恢复说明见 [GitHub 维护与同步](workbench-sync.md)。

| 页面 | 可以完成的事 | 事实边界 |
| --- | --- | --- |
| 维护总览 | 跨项目查看待办、来源时间、未知/省略和下一步，跳入任务 | 复用 overview；缓存不是实时 GitHub 状态 |
| 项目空间 | 查找项目、切换 worktree、查看角色/分支/HEAD、按关注点读导览和代码依据 | 导览过期时要求重新扫描；代码文字不执行 |
| GitHub 维护 | PR / Issue / 最近更新、CI / 评审与显式同步 | 分来源保留成功观测；列表消失不证明 PR 关闭 |
| 本地操作 | 查看同步进度、失败和历史，取消排队或重试 | 操作已持久化，失效租约可恢复；不消费原生提交与合并任务 |
| 任务接续 | 按 WorkItem 查看多次尝试、预览预算、保存/下载接续包、排队验证 | 外部任务复用 CLI/MCP 的预算与适用性核对；维护任务显示原 Context Pack/Checkpoint |
| 审阅依据 | 查看验证适用性、历史状态、任务轨迹和来源 | 历史通过/合并判定不是新的提交或合并授权 |
| 设置与诊断 | 查看安装、配置来源、状态目录和两个数据库的兼容性 | 复用 doctor --local，不认证、不升级 |

外部任务沿已有 `task start/checkpoint` 流程创建和接续。网页保留 Agent 自述与独立
验证的区别，也保留上下文的来源和省略信息。缺少任务上下文时仍可查看相应 Issue 的
生命周期轨迹。项目导览可选择 Codex、Claude Code 或 Copilot（VS Code）的 MCP 配置
预览命令；配置预览不等于实际客户端会话已验证。浏览器可保存接续包并通过现有队列请求受信任配置的隔离验证；其他 POSIX 终端
命令仍需手动执行。导览与任务接续命令保留当前配置路径，便于从其他目录使用。

任务列表最多显示某项目最近50次尝试，项目列表最多50项、工作区列表最多100项；
详情响应上限400KB。超限、无法读取或不兼容时显示明确状态，不截掉关键上下文后
宣称完整。异常详情不会删除其他项目。可继续使用 CLI 查看更大的范围。

## 保存接续包与验证

进入某次任务的“接续与验证”，选择上下文预算和 Codex、Claude Code 或 Copilot。
外部任务复用 CLI/MCP 的预算规则，保留目标、待办、决定、阻塞和证据；必要内容
装不下时返回错误并要求提高预算。原生任务保留完整 Context Pack/Checkpoint，
只有原生工作区已关联时才可保存带快照的接续包。tokens 是估算值，不代表模型账单。

“生成并保存接续包”核对账号、项目、任务、工作区快照、检查点与计划摘要，在本地
保存不可变 JSON；下载只读取已保存内容，不接受文件路径。快照或检查点变化时旧包
显示过期，仍可下载用于追溯。网页接续包用于客户端读入任务上下文；它的外层包含
工作台快照与客户端信息，不是 `context import` 的规范 bundle 文件。

四类记录分别展示：包已生成；使用人确认已交给客户端；检查点中的 Agent 执行声明；
隔离验证证据。下载不产生接收确认，人工确认也不证明客户端执行过。通过文件或
已有 scoped MCP/CLI 接续，无需自动启动客户端或修改客户端钩子。

外部任务的验证按钮只提供用户配置中的 profiles。请求绑定预览时的检查点、快照与
配置摘要，使用现有持久队列和 DockerVerifier；本地操作页展示进度、取消与历史结果。
重启会恢复队列；执行中断且结果未知时仍须按已有验证恢复流程核对原执行，不会
自动声称通过。验证完成不结束开发任务，也不授予提交或合并权限。原生维护任务
仍沿已有 CLI 验证流程执行。操作页同时显示原生队列的只读历史投影，旧记录没有
账号字段时明确标注，不把它当作当前账号的操作授权。

本能力增加状态 schema 29，需通过 `state plan` / `state upgrade` 显式升级后使用。
网页不会自动升级现有日常数据库。接续包列表展示最近 10 项；任务分组基于最近
50 次尝试，省略数量仍按尝试计数，不代表完整历史。

## 本机入口与会话

服务固定绑定 `127.0.0.1`，只接受精确 Host 与同源请求，API 还需当前进程随机生成的
会话。链接中的会话放在 URL fragment 中，由页面取出并移除；同一标签页使用
sessionStorage 保持刷新后的连接，API 通过 Authorization 请求头携带会话。
访问日志不记录请求或会话。链接相当于本次会话的本机工作台权限，不放入公开 Issue/PR。

会话最长12小时，进程退出即失效。无会话的页面只有静态外壳，不能读取项目数据。
API 没有 CORS 授权。只有声明的本地命令接受带同源信息和幂等编号的 POST，
不提供远程写入或任意执行；拒绝任意文件路径和跨项目任务请求。所有仓库文字按
文本显示，页面使用 CSP 限定脚本和资源。接口重新核对登记身份与工作区绑定。
已加载的配置文件变化会要求重启；新增配置层也应重启后重新核对生效配置。

这是单机维护入口，使用 FastAPI + Uvicorn 的本地适配层。启动一个服务进程，
前后端同源；默认不会启用反向代理头信任、多进程 worker 或远程账户隔离。

启动和 GET 不创建数据库，首次显式本地命令可初始化空状态；旧数据库不由网页迁移。需要升级时退出相关写入客户端，按
[升级与备份说明](state-upgrades.zh-CN.md)操作，再启动工作台。更换安装参见
[独立安装说明](local-installation.zh-CN.md)。

## 接口与验证

`web.workbench.Workbench` 组合现有只读应用服务；`web.api` 提供 FastAPI 查询与 Pydantic
契约，`web_server` 管理本机 socket 与服务生命周期。React 前端调用 `/api/v1/overview`、
`projects`、`workspace`、`code`、`tasks`、`task`、`review` 和 `settings`。
`local_operations` 使用现有队列执行显式本地操作，GitHub 查询与命令见同步说明。
旧 `/api/*` 读取入口保留兼容；新接口使用 `data/meta` 响应。
接口只接受声明参数，项目/工作区/任务均使用登记 ID；不暴露 Pipeline、
SQL、shell、原始配置或任意文件服务。没有新增网页任务表或另一套状态机。

回归测试覆盖会话/请求来源、固定路由、未声明写方法拒绝、跨项目隔离、绑定变化、旧库、
读取不变性以及 CLI/MCP 与网页查询的一致性。浏览器交互与打包检查在加固验证容器中
执行；用于浏览器验证的额外工具不成为日常安装依赖。

## 前端开发与分发

前端源码在 `frontend/`，采用 TypeScript、React、React Router、TanStack Query 和 Vite。
项目选择进入浏览器路由；Query 缓存只代表本地读取缓存，不能替代 GitHub 观测时间。
切换项目后旧请求结果不会进入新项目页面。

源码开发需要 Node 22.12+ 与 npm。`uv sync --locked` 的构建步骤安装锁定前端依赖，
从 FastAPI 导出 OpenAPI、生成 TypeScript 类型并构建静态页面；`uv build` 将页面与
逐文件摘要清单打入 wheel / sdist。已构建发行包的日常安装和运行不需要 Node。
构建使用清理后的环境，不把 GitHub 凭据传给 Node；依赖脚本默认禁用。

`frontend/openapi.json` 和 `frontend/src/api/generated/schema.ts` 是生成契约；
后端协议测试核对 OpenAPI 漂移。`npm run check` 在前端目录执行类型、ESLint 和交互检查，
Python 回归也包含这项检查。测试应在加固 verifier 中运行。构建产物缺失或摘要
不匹配会明确失败，避免安装包静默使用旧页面。

需要热更新时，先启动 `reposteward web --port 8787`，在 `frontend/` 运行
`npm run dev`。浏览器打开 `http://127.0.0.1:5173/#session=<终端链接中的会话>`，
会话仅由浏览器携带；不要将它设置成 Vite 环境变量或写入文件。开发代理固定在
127.0.0.1:5173，校验来源后将 `/api/` 转发到本机后端。后端端口可通过
`REPOSTEWARD_DEV_BACKEND=http://127.0.0.1:<port>` 指定。正式服务仍直接提供打包页面。

设计依据：[FastAPI](https://fastapi.tiangolo.com/tutorial/bigger-applications/)、
[Vite 后端集成](https://vite.dev/guide/backend-integration)、
[CSP](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy)、
[Fetch Metadata](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Site)。
