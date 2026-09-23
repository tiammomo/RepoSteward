# 从陌生仓库到可回查的项目理解

RepoSteward 的 `understand` 用于先理解整个项目，再缩小到一次改动的相关代码。
人可以直接阅读 Markdown；已有的 coding agent 通过同一份 JSON/MCP 事实继续解释。
不需要把全仓代码一次性放进模型上下文，也不把模型的解释当成已验证的代码事实。

## 两种使用路径

- **管理自己的项目**：对已关联工作区扫描，先读项目目标、入口、核心模块、维护约束与测试。
  迭代后重新扫描，复用未变文件的解析结果；导览包含本轮新增、删除、修改数量。
- **首次向其他项目贡献 PR**：先取得本地 clone，直接扫描和阅读，不要求先关联项目、登录 GitHub、
  创建任务或 Issue。理解需求后再进入已有的 Issue 审阅、隔离实施与 PR 流程。

这是本地 CLI/MCP 能力；不会自动拉取远程分支，也未新增托管网页服务。
索引针对实际选定的本地版本。若要理解 GitHub 上的新版本，先自行更新 clone，再显式扫描。

## 开始阅读

```bash
reposteward understand scan /path/to/project
reposteward understand guide /path/to/project --mode contributor
reposteward understand query /path/to/project 'delivery retry' --limit 8
reposteward understand guide /path/to/project --format json
```

`scan` 是唯一会更新理解缓存的动作；不修改目标仓库。缓存使用独立 JSON 文件，
不创建或迁移任务数据库。默认放在配置的用户状态目录下的 `understanding/`；
没有配置时使用默认用户状态目录。可在上述每条命令末尾添加
`--cache-dir /user-owned/path/understanding` 显式选择缓存；必须位于目标仓库之外。
在尚未配置的环境中，首次阅读不需要执行 `init`。

`guide`、`query` 和 `evidence` 读取缓存并校验源文件，不写缓存、不调用模型、不执行项目命令。
尚未扫描时返回 `not_scanned`；发生变化时返回 `stale`，隐藏旧结论并提示重新扫描。
这些状态属于成功查询，CLI 返回 0；无效参数、损坏缓存等错误返回 2。

导览提供：

1. 本地 commit、扫描时间、工作区是否有改动，以及索引覆盖和排除原因。
2. 项目 manifest 的目的声明、启动入口，以及可定位的入口实现。
3. 从 README/贡献规范到入口、静态引用较多的实现、相关测试的建议阅读顺序。
4. 已选模块之间的静态导入关系、符号定义和 docstring 声明。
5. 每个来源的路径、行号、SHA-256 和可继续读取的证据 ID。

清洁工作区会生成指向 GitHub commit 的来源链接；该链接由本地 HEAD 推导，
不代表已联网确认此提交存在于 GitHub。脏工作区使用本地文件引用，避免把未提交内容链接成远程版本。

## 围绕一次问题深入

`query` 在路径、模块、符号和声明中匹配关键词，然后补充静态导入目标、使用方与测试线索。
使用具体符号或路径通常更有效；暂不具备跨语言语义检索、中文问题自动翻译或运行时调用追踪。
没有命中时明确返回空路线，不编造一条答案。

从导览复制 `code:...` 证据 ID，按引用行号继续读：

```bash
reposteward understand evidence /path/to/project code:FULL_DIGEST --start-line 40 --limit 80
```

`FULL_DIGEST` 是导览返回的 64 位摘要，占位符不能直接运行。证据 ID 同时绑定工作区、文件和内容，
不能用于读取任意路径或另一个 clone 的文件。一次最多返回 120 行、16,000 字符。
证据读取校验该文件与 Git 版本；全索引的新鲜度由 `guide`/`query` 校验。

给已有 Agent 的建议任务：

> 先读取 RepoSteward 项目导览，再查询这个 Issue 涉及的模块。按证据 ID 分段阅读实现与测试。
> 解释用户入口如何到达核心逻辑、数据如何流动、哪些边界影响这次修改。
> 每条关键结论引用文件和行号；区分代码事实、文档声明与推断，说明尚未确认的问题。
> 不将静态导入当作运行时执行链，也不将测试文件关联当作已经通过验证。

理解结果可进入现有 checkpoint；经过审阅的可复用结论再进入 `knowledge` 流程。
导览本身不自动提升为可信知识，不改变任务 Context Pack 的版本或发布权限。

## MCP 共用入口

在已关联、已配置策略的工作区启动已有 MCP 服务，增加的 `understanding` 工具支持：

```json
{"action":"guide","mode":"contributor","limit":12}
{"action":"query","focus":"delivery retry","limit":8}
{"action":"evidence","evidence_id":"code:FULL_DIGEST","start_line":40,"limit":80}
```

MCP 绑定服务启动时的那个工作区，不接收任意 `path` 或命令；没有 `scan` 动作。
先用 CLI 扫描到同一配置状态目录的 `understanding/`。若 CLI 使用了额外的 `--cache-dir`，
该独立缓存不会自动出现在 MCP 服务中。取消关联后，原服务的查询也会失效。

## 覆盖与版本边界

- 当前解析 Python AST 的模块、函数/类、静态导入和 main guard；读取 `pyproject.toml`、
  `package.json` 的声明与脚本，不安装依赖或运行脚本。其他语言只做文件分类和可用的清单/文档读取。
- 导入只在索引中唯一匹配的本地 Python 模块间建立关系。重复模块名、动态导入、外部依赖不猜测。
- Git 可见文件列表受 1 MB 元数据上限约束；每次最多考虑 2,000 个候选文件，单文件 512 KiB，
  文本读取预算 20 MiB。扫描和新鲜度检查重复读取以检测并发修改。超预算与不支持项都有覆盖计数。
- 跳过敏感路径、疑似凭据内容、符号链接、特殊文件、非 UTF-8/二进制内容和依赖/构建目录；
  内容筛选可能保守地排除带凭据示例的测试。被排除内容不属于“当前索引”保证范围。
- 每文件最多保留 128 个符号、256 个导入，AST 遍历上限 50,000 个节点；导览最多 20 个阅读项、
  24 条局部关系，JSON 导览最多 240,000 字节，达到上限会标明省略。证据按需分段。索引缓存最多 16 MiB，超限失败保留上次成功版本。
- 缓存按仓库身份和本地工作区指纹隔离；同仓库的两个 clone/worktree 也需要分别扫描。
  路径新增、删除、重命名、内容变化或 Git 版本变化会使旧导览失效。
  使用 `scan --rebuild` 重建不兼容或损坏的缓存。

导览减少的是重新定位代码的重复工作；本功能没有测量人工理解时间或模型开发质量提升百分比。
后续网页展示或 Agent 语义解释可消费相同查询结果，仍需保留来源、版本、覆盖范围与未知项。
