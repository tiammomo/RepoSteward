# 安全政策

## 报告安全问题

请不要为凭据泄露、权限绕过、命令注入、沙箱逃逸或其他安全问题创建公开 Issue。

优先使用 GitHub 的
[Private vulnerability reporting](https://github.com/tiammomo/RepoSteward/security/advisories/new)
提交报告。报告中请包含受影响版本、影响范围、最小复现和建议的缓解方式，但不要附带真实凭据或
无关的个人数据。

如果仓库尚未启用 Private vulnerability reporting，请先通过仓库所有者公开资料中的私有联系
方式告知维护者启用该入口，不要在 Issue、Discussion 或 PR 中披露漏洞细节。

## 支持范围

当前处于 `0.x` 阶段，仅最新的 `main` 和最新发布版本接受安全修复。安全修复会优先保护以下
边界：GitHub 与模型凭据隔离、公开写入确认、工作区权限、验证容器隔离和不可信仓库内容处理。
