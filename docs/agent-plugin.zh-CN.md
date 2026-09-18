# 导出本机 Agent 插件

RepoSteward 可以把一个已关联工作区的 MCP 连接和四类 skills 导出成 Codex
插件目录。CLI、MCP 和插件共用项目、任务与验证服务。导出不修改客户端设置、
不联网安装插件，也不代表真实模型已经完成接续。

## 准备和预览

在日常使用的独立环境安装 `reposteward[mcp]`，按已有流程配置仓库并关联项目。
首次只读理解项目仍可直接使用 `understand` CLI，无需 Issue 或插件。
插件需要已有的项目关联及启用的仓库策略。

```sh
reposteward project inspect /absolute/path/project
mkdir -p "$HOME/plugins"
reposteward plugin plan /absolute/path/project \
  --output "$HOME/plugins/reposteward-my-project"
```

输出包括每个文件的内容和摘要、绑定身份、运行时源码摘要、配置摘要及 MCP
依赖诊断。输出目录名称必须为小写 kebab-case、最多 64 个字符，与插件名称一致。
目录必须尚不存在，且位于关联工作区之外；父目录需要先创建。

检查输出后，将返回的 `plan_digest` 传给独立导出命令：

```sh
reposteward plugin export /absolute/path/project \
  --output "$HOME/plugins/reposteward-my-project" \
  --plan-digest REVIEWED_PLAN_DIGEST
```

配置、代码资源、输出路径或项目身份变化会使旧计划失效。诊断显示缺少 MCP 时，
先在执行此命令的 Python 环境安装可选依赖，再重新预览。导出不读取或复制凭据。

## 包内的能力

- `understand-project`：读导览、定位实现与测试、按来源读取代码；索引缺失或过期时提示显式扫描。
- `resume-task`：恢复要求、剩余工作、决定与下一步，并用 revision/snapshot 保存检查点。
- `verify-change`：选择可信用户配置中的验证 profile，检查结果是否适用于当前代码。
- `maintain-pr`：读取原生管理 PR 的 CI、评审与合并阻塞，沿用既有发布门禁。

`connection.json` 保存本机工作区和 CLI 参数；`.mcp.json` 保存相同的解释器与
配置路径，并固定账号、状态目录、项目、工作区及其关联版本的 scope digest。
取消关联、重新关联、替换 clone 或切换账号后，旧绑定会被拒绝；需要重新导出。
运行中的服务也会检查工作区关联是否变化。普通提交或切换 feature branch 不改变绑定。

原有 `mcp config` 和未设置 `--expected-scope` 的 `mcp serve` 配置仍可使用。
生成包内的 MCP 额外设置了预期 scope；它不是发布/合并凭证，也不让 Agent 绕过仓库规则。

## 客户端安装与升级

通过本机 Codex 支持的本地插件/marketplace 流程安装该目录。不同客户端版本的
安装命令和配置能力应分别核对；导出结果明确标记 `client_installation: not_attempted`
与 `actual_session_validation: not_run`。安装后在新会话验证 project 指向预期工作区，
再尝试读取导览或恢复任务。能够列出工具不等于已经完成真实开发任务。

该包包含机器路径，**仅供本机使用，不要提交到 Git 或跨机器分享**。在另一台机器
重新安装 CLI、关联工作区并重新导出。不要删除包所引用的 Python 安装。

升级 CLI、改变关联或配置后，重新预览并导出到新目录，再更新客户端安装。
旧目录保留可用于核对与回退。插件版本带内容摘要，可辨别不同导出内容。
先从客户端卸载，再移除自己创建的导出目录；导出器不修改已有 AGENTS.md、
CLAUDE.md、Copilot 指令或 marketplace。

## 中断与恢复

导出使用独占的新目录，文件权限默认仅当前用户可访问。客户端 manifest 最后写入。
若写入失败，保留已创建内容供检查；没有成功结果的目录不能作为完整插件安装。
再次导出请选择新目录，不通过覆盖来修复不明来源的文件。
`export.json` 记录计划摘要和文件摘要，用于核对内容，不是来源签名或安装回执。

本能力没有新增数据库 schema、后台监控、自动 hooks 或公开写入 MCP 工具。
Claude Code/Copilot 继续使用已有的客户端配置预览与文件接入；本导出格式只声明
Codex 支持，不代表已经完成其他客户端的插件分发。
