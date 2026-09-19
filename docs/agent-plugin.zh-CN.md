# 使用 RepoSteward Agent 插件

RepoSteward 可以把一个已关联工作区的 MCP 连接和四类 skills 导出成 Codex
插件目录。CLI、MCP 和插件共用项目、任务与验证服务。导出不修改客户端设置、
不联网安装插件，也不代表真实模型已经完成接续。

## 名称与适用范围

默认名称用 **`reposteward`**，不需要 `-local`。名称来自导出目录的最后一段；
本机运行是连接方式，不是必须写进名称的后缀。`reposteward-local` 等旧名称仍有效。

一个实例固定关联一个工作区，不会随 Codex 的当前目录自动切换。若同时使用多个项目，
分别关联并导出为 `reposteward-project-a`、`reposteward-project-b`，调用前核对项目身份。
不要把同名插件覆盖到另一个项目上。

## 准备和预览

在日常使用的独立环境安装 `reposteward[mcp]`，按已有流程配置仓库并关联项目。
首次只读理解项目仍可直接使用 `understand` CLI，无需 Issue 或插件。
插件需要已有的项目关联及启用的仓库策略。

```sh
reposteward project inspect /absolute/path/project
mkdir -p "$HOME/plugins"
reposteward plugin plan /absolute/path/project \
  --output "$HOME/plugins/reposteward"
```

输出包括每个文件的内容和摘要、绑定身份、运行时源码摘要、配置摘要及 MCP
依赖诊断。输出目录名称必须为小写 kebab-case、最多 64 个字符，与插件名称一致。
目录必须尚不存在，且位于关联工作区之外；父目录需要先创建。

检查输出后，将返回的 `plan_digest` 传给独立导出命令：

```sh
reposteward plugin export /absolute/path/project \
  --output "$HOME/plugins/reposteward" \
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

## 在 Codex 安装

导出与注册 marketplace 是独立步骤。让 Codex 的 `$plugin-creator` 将已导出的
`$HOME/plugins/reposteward` 注册到个人 marketplace，保留原有插件条目；不要重新生成
或覆盖已审阅的包。个人目录是 `~/.agents/plugins/marketplace.json`，默认市场名为
`personal`，其中本例的 `source.path` 为 `./plugins/reposteward`。
如果你已有的市场采用其他名称，以实际文件中的名称为准。

本机支持 `codex plugin add` 的版本中，注册完成后执行：

```sh
codex plugin add reposteward@personal
codex plugin list --json
```

确认该项为 `installed: true`、`enabled: true`，然后新开会话发送：

> 使用 reposteward 插件，先确认绑定项目，再读取项目导览与已有任务上下文，给出下一步建议。

客户端没有此命令时，先查看 `codex plugin --help`，在支持个人 marketplace 的插件界面
选择对应插件。不要把导出成功当成安装成功。具体客户端流程以
[官方插件文档](https://developers.openai.com/plugins/build/plugins)及当前版本帮助为准。

## 从 reposteward-local 改名

1. 对同一已关联工作区重新预览、导出到尚不存在的 `$HOME/plugins/reposteward`。
2. 注册新条目并安装 `reposteward@personal`，确认其 project 和原实例一致。
3. 从客户端卸载旧实例 `codex plugin remove reposteward-local@personal`，避免两个实例
   同时提供重复工具。保留旧导出目录与解释器，必要时可重新安装回退。
4. 新开会话。只改目录名或 manifest 不会完成迁移，还会破坏已有文件摘要。

## 升级与回退

插件实例名称保持不变，版本通过清单中的 `version` 及摘要辨认。再次导出时，
使用新的父目录，例如：

```sh
mkdir -p "$HOME/plugin-builds/upgrade-001"
reposteward plugin plan /absolute/path/project \
  --output "$HOME/plugin-builds/upgrade-001/reposteward"
reposteward plugin export /absolute/path/project \
  --output "$HOME/plugin-builds/upgrade-001/reposteward" \
  --plan-digest REVIEWED_PLAN_DIGEST
```

检查新包后，通过客户端支持的市场管理流程更新同一条目的来源，或将个人市场已登记的
`$HOME/plugins/reposteward` 目录归档到私有备份位置，再放入已核验的新包。先停止旧客户端
连接，保留原包，不覆盖合并文件；切换后核对 `export.json` 中的每个文件摘要，再运行
`codex plugin add reposteward@personal`。`connection.json` 中的工作区和解释器路径
不会因为移动插件目录而自动更新，必须仍然存在并通过绑定检查。

回退时恢复旧来源、重新安装并新开会话。不要对审阅过的导出包手工修改版本号来刷新缓存；
重新导出会生成反映内容的新摘要。项目版本与正式发布规则见[版本管理指南](releases.md)。

## 安装状态与本机限制

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

## 检查已有包和预览安装

使用预期的 CLI 安装环境，检查导出包与当前工作区是否仍匹配：

```sh
reposteward plugin doctor /absolute/path/project \
  --bundle "$HOME/plugins/reposteward-my-project"
reposteward plugin install-plan /absolute/path/project \
  --bundle "$HOME/plugins/reposteward-my-project"
```

这两个命令只读、离线，不启动包中的 MCP 命令，不调用 Codex，不修改客户端
设置或迁移数据库。检查涵盖文件完整性、可信导出模板、工作区与账号绑定、
Python 运行环境和配置文件摘要。符号链接、非普通文件、超大文件、额外文件和
不完整导出会被拒绝。摘要本身不是来源签名，因此还会与当前 CLI 的模板核对。

默认观察 `~/.agents/plugins/marketplace.json` 与 `CODEX_HOME`（未设置时为
`~/.codex`）。可用 `--marketplace /absolute/path/.agents/plugins/marketplace.json`
和 `--codex-home /absolute/path/codex-home` 显式选择。仅检查所选插件的本地来源、
用户级启用设置，以及所选 marketplace 下对应版本的缓存；其他缓存布局、仓库级
或托管配置可能改变客户端有效状态，因此 `effective_installation`、`mcp_health`
仍为 `not_probed`。用户配置的其他内容不会写入报告。

`doctor` 返回码 0 表示包与当前环境兼容，客户端观察仍可能有 warning；返回码 2
表示包检查失败。`install-plan` 只有在包兼容、所选 marketplace 已指向该包且
允许安装、用户未显式禁用该插件时，才给出安装命令参数数组和 `CODEX_HOME`；
否则返回码 2 并保留诊断原因。非默认 marketplace 的计划先列出注册步骤。
计划及其摘要是当时状态下的建议，不是自动执行授权；执行前应重新检查。

遇到运行时不一致，先核对是否用了另一套 CLI；这不证明原有安装已损坏。
配置或绑定变化后，审阅新导出再安装。新版导出回执记录配置摘要；旧回执仍可
检查，但会提示无法确认导出时的配置字节。安装、更新、回退和新会话试用仍由
各自流程完成，诊断通过不计为实际任务成功或 token 节省证据。

## 中断与恢复

导出使用独占的新目录，文件权限默认仅当前用户可访问。客户端 manifest 最后写入。
若写入失败，保留已创建内容供检查；没有成功结果的目录不能作为完整插件安装。
再次导出请选择新目录，不通过覆盖来修复不明来源的文件。
`export.json` 记录计划摘要和文件摘要，用于核对内容，不是来源签名或安装回执。

本能力没有新增数据库 schema、后台监控、自动 hooks 或公开写入 MCP 工具。
Claude Code/Copilot 继续使用已有的客户端配置预览与文件接入；本导出格式只声明
Codex 支持，不代表已经完成其他客户端的插件分发。
