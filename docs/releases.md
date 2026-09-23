# Versions, releases and rollback / 版本、发布与回退

RepoSteward has a **0.1.0 development baseline**, not an implied stable release.
[CHANGELOG.md](../CHANGELOG.md) records user-visible changes. A source checkout,
a wheel, an installed CLI, and an exported plugin are different deliverables.
A passing check or merged PR does not publish a release.

## What identifies an installation

| Identifier | Purpose |
| --- | --- |
| `src/reposteward/__init__.py::__version__` | Single project version source; Hatch reads it for wheel/sdist metadata; CLI and plugin export reuse it |
| Git commit + artifact SHA256 | Exact source and built artifact; necessary while multiple development commits share the baseline version |
| `reposteward version` | Installed module/interpreter and package version; a wheel may have no `source_revision` |
| Plugin `name`, normally `reposteward` | Stable instance identity, not a release number or portability claim |
| Plugin `version`, e.g. `0.1.0+bundle.<digest>` | Project version plus exported content identity; different paths/configuration can produce different digests |
| `export.json` and `connection.json` | Reviewed file hashes, workspace binding and runtime code digest; not signatures or publication receipts |

Do not use plugin digest suffixes as an ordered update channel. Do not hand-edit an
exported manifest or add a timestamp to an audited bundle: that invalidates its recorded
file hashes. Re-export through a fresh plan. A plugin upgrade does not migrate databases.

## Version policy

- Keep one `MAJOR.MINOR.PATCH` value in the version source. Do not add a second literal
  version in `pyproject.toml`; `uv.lock` is generated and refreshed with `uv lock`.
- Patch increments cover compatible fixes; minor increments cover new capabilities.
  Before 1.0, incompatible CLI/configuration/protocol changes require a minor increment
  and explicit migration notes. After 1.0, breaking changes require a major increment.
- Do not reuse a published version for different bytes or move a published tag.
  Prepare every release through a reviewed Issue and focused PR.
- Development changes go under `Unreleased`. In a release-preparation PR, set the version
  and move the applicable entries to `## [X.Y.Z] - YYYY-MM-DD`, with a real release date.
  The current checker deliberately accepts stable numeric versions only; prerelease
  channels need a separately reviewed policy and implementation.

## Maintainer verification

Run these commands in RepoSteward's hardened verifier, using a clean output directory.
Dependency/build preparation follows the configured trusted install phase; tests and
artifact checks run without network access or GitHub credentials.

```sh
uv sync --locked --python 3.12
uvx ruff check .
uvx ruff format --check .
uv run python -W error::ResourceWarning -m unittest discover -s tests -v
uv run reposteward --help
uv build --out-dir dist/release-candidate
uv run python scripts/check_release.py --dist-dir dist/release-candidate
```

The check opens archives without extracting or importing their code. It compares source,
resources, documentation and metadata, and returns SHA256 values. A reused directory with
multiple wheels or sdists fails instead of silently choosing one. The existing CI runs the checker's regression tests and builds distributions.
Run the archive check above separately and retain its result with the release candidate;
CI does not yet invoke that extra archive-check command. Changes to GitHub Actions remain
subject to the repository's protected-workflow policy. This is artifact consistency
verification, not a signature or a complete provenance/supply-chain audit.

For an actual release candidate, also run (replace `X.Y.Z`):

```sh
uv run python scripts/check_release.py --dist-dir dist/release-candidate --tag vX.Y.Z
```

The tag argument verifies the intended name and dated changelog, **not the existence of
that Git tag**. Complete separate installed-wheel acceptance from outside the checkout:
`version`, `doctor --local`, CLI smoke, plugin export, MCP project identity and tool reads.
Record commit, Python/client versions, commands/results, artifact hashes and schema
compatibility. Rebuild the sdist into a wheel to check source distribution usability.

After the PR is merged, release publication is a separate maintainer action: verify the
merged commit and CI, create a signed `vX.Y.Z` tag for that exact commit, and attach the
reviewed artifacts, hashes, changelog and migration notes to the GitHub Release.
The project does not currently automate tag creation, GitHub Releases, or PyPI upload;
never infer that a package with the same name on a registry is the reviewed artifact.

## User upgrades / 用户升级

1. 保存当前 CLI 的版本、提交、wheel SHA256、插件目录及其解释器路径。
   使用 `reposteward doctor --local` 核对状态目录和 schema；按
   [独立安装](local-installation.zh-CN.md)与[状态升级](state-upgrades.zh-CN.md)
   指南保存一致性备份。
2. 在独立环境安装已核验的新 wheel，保留旧解释器。先检查 CLI、依赖与状态兼容性，
   再切换日常入口；不要直接覆盖唯一可回退的环境。
3. 按[插件指南](agent-plugin.zh-CN.md)重新 plan/export。保持插件名称不变，使用新的
   父目录；核对工作区、账号、scope、解释器和文件摘要，再切换 marketplace 来源并重装。
4. 在新 Codex 会话读取 project 和导览，核对身份后再接续任务。
   配置、关联或账号变化可能使旧 scope 失效，普通代码提交不改变绑定。

回退时将客户端来源切回保留的旧包并重新安装，恢复匹配的 CLI 环境，然后新开会话核对。
回退插件或解释器不会回退数据库；旧版本不支持当前 schema 时，应停止并使用已验证的恢复流程。
机器路径、账号和绑定已改变的旧包不能直接回退使用，需要针对当前环境重新导出。
