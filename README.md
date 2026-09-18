# RepoSteward

**English** · [简体中文](README.zh-CN.md)

**Give your coding agent project context that lasts beyond a conversation.**

RepoSteward helps individual maintainers and small teams work across GitHub projects:
understand code, preserve task progress, resume agent sessions, verify changes, and follow
Issues and pull requests. Keep coding with Codex, Claude Code or Copilot; RepoSteward
stores project facts, decisions, verification evidence and maintenance rules outside the session.

[Quick start](#quick-start) · [Codex plugin](#connect-the-codex-plugin) · [Multiple projects](#manage-existing-projects) · [Documentation](#documentation)

## What it helps you do

| When you need to… | RepoSteward provides… |
| --- | --- |
| Join an unfamiliar project or prepare a contribution | Local code guides with implementation, test and source references |
| Switch conversations, models or clients | Task goals, decisions, remaining work and a concrete next step |
| Assess an agent's changes | Isolated verification, with agent claims, historical results and applicable evidence kept distinct |
| Maintain several repositories | Existing clone/worktree bindings and shared views of tasks, blockers and cached GitHub facts |
| Keep Issues and PRs moving | Reviewed intake, change preparation, CI/review follow-up and separate publication/merge gates |

RepoSteward runs locally. Its CLI, MCP server, Codex plugin and local workbench share
application services and persistent state.

## Quick start

Try a code guide with **Python 3.12+, uv, Git** and an existing local repository.
You can do this before configuring a GitHub identity, Docker or an agent.

```bash
git clone https://github.com/tiammomo/RepoSteward.git
cd RepoSteward
uv sync --locked --python 3.12

uv run reposteward understand scan /absolute/path/to/project
uv run reposteward understand guide /absolute/path/to/project
uv run reposteward understand query /absolute/path/to/project "symbol_or_path"
```

Replace the path with your clone/worktree. `scan` writes an understanding cache outside
the repository; `guide` returns entry points, a suggested reading route and source evidence.
`query` narrows the route using symbols, paths or keywords. Rescan after edits: stale guides
are not presented as current facts.

Coverage currently centers on Python static structure, project manifests and documentation;
it does not establish full cross-language or runtime understanding.
See the [project understanding guide (中文)](docs/project-understanding.zh-CN.md).

For everyday use from other project directories, install a verified wheel using the
[standalone installation guide (中文)](docs/local-installation.zh-CN.md). The remaining
`reposteward` examples assume that installation; do not rely on this checkout's `uv run`
environment after moving to another project. Install the wheel's `mcp` extra for MCP or plugins.

## Connect the Codex plugin

The recommended plugin name is **`reposteward`**. Four skills help you understand a project,
resume a task, verify changes and maintain a PR within your existing Codex workflow.

### 1. Link the target project

For first-time identity setup, authenticate with GitHub CLI and run `reposteward init`.
Skip initialization if you already have user configuration. This example uses a repository
you maintain; choose `--mode contributor` when contributing to someone else's project.

```bash
cd /absolute/path/to/project
reposteward repo add owner/project --mode maintainer
reposteward project link .
reposteward project inspect .
reposteward understand scan .
```

Replace `owner/project`; skip `repo add` for an already configured repository. It creates
a machine-local policy skeleton. Configure allowed dependency setup and verification commands
before running verification or delivery workflows.

### 2. Review and export the plugin

Stay in the target project directory. The output must be a new directory outside the repository.

```bash
mkdir -p "$HOME/plugins"
reposteward plugin plan . --output "$HOME/plugins/reposteward"
```

Review the workspace, account, interpreter path and file contents, then use the returned
`plan_digest` in a separate export:

```bash
reposteward plugin export . --output "$HOME/plugins/reposteward" \
  --plan-digest REVIEWED_PLAN_DIGEST
```

### 3. Register, install and use it

Follow the [plugin guide (中文)](docs/agent-plugin.zh-CN.md#在-codex-安装) to register the
exported directory in your personal marketplace. Export alone does not install a client plugin.
For Codex versions supporting plugin commands, with the default marketplace name `personal`:

```bash
codex plugin add reposteward@personal
codex plugin list --json
```

Confirm that it is installed and enabled, then start a new conversation:

> Use the reposteward plugin. Confirm the bound project, read its code guide and current task context, and suggest the next step.

The [rename, upgrade and rollback guide (中文)](docs/agent-plugin.zh-CN.md) also covers an
existing `reposteward-local` installation. Generated bundles contain local paths; install,
link and export again on another machine.

## Manage existing projects

Keep your projects where they are. Clone them through their usual workflow, then register them:

```bash
reposteward project link /absolute/path/to/project-a
reposteward project link /absolute/path/to/project-b
reposteward project list
reposteward overview show --format text
reposteward web
```

- **Repository identity and workspace binding are separate.** Worktrees sharing a remote belong to one project, with distinct workspace bindings.
- **Linking is not maintenance authority.** Configure the corresponding repository role and policy for maintenance or contributions.
- **Each plugin instance binds one workspace.** Use distinct names such as `reposteward-project-a` for additional projects; changing Codex's directory does not switch the binding.
- **Refresh GitHub facts explicitly.** `overview refresh` fetches and caches facts with timestamps and error states. `overview show` and the current web view read local records.

The current workbench is a local, read-only view of cross-project work, code guides,
task continuity and review evidence. The FastAPI/React workbench and importing projects
by pasting a URL into the web interface remain follow-up work.
See [workbench (中文)](docs/local-workbench.zh-CN.md) and [agent assistance (中文)](docs/coding-agent-assistance.zh-CN.md).

## From task continuity to GitHub maintenance

Continue coding in your own agent. Start development tasks from reviewed open Issues on
feature branches: use `task start`, then CLI/MCP context, checkpoints and constrained
verification. To delegate change preparation to a configured Codex harness, use the
`gate` and `prepare` workflow instead.

<p align="center">
  <img src="docs/assets/reposteward-lifecycle.svg" width="100%" alt="A reviewed Issue enters a separate workspace; an agent prepares changes, isolated verification produces evidence, and a maintainer reviews before separately publishing a PR and following CI and review feedback.">
</p>

<p align="center"><sub><a href="docs/assets/reposteward-lifecycle.excalidraw">Editable diagram source</a></sub></p>

| Stage | Common entry points |
| --- | --- |
| Continue work in an existing agent | `task start`, `task current`, `task context`, checkpoints |
| Delegate a reviewed change | `gate`, `prepare`; `adopt` registers an existing clean commit |
| Inspect results | `inspect`, `logs`, independent verification records |
| Publish and follow up | Separate `submit`, `follow-up`, `repair` |
| Assess merging | `merge-decision`; review and execute a merge separately if eligible |

Online maintenance requires a GitHub identity, repository policy and SSH. Isolated
verification requires Docker/Runner; delegated coding also requires the chosen harness's
authentication. `submit` requires a separate invocation, `REPOSTEWARD_ENABLE_SUBMIT=1`
and a matching `--reviewed-by` identity. MCP exposes no publication or merge tools.

See the [operator guide (中文)](docs/operator-guide.zh-CN.md) for full commands,
Project Draft intake and maintenance gates.

## Context, verification and usage

RepoSteward supplies budgeted Context Packs with task requirements, decisions, remaining
work and sources. Omitted optional material has coverage notes and retrieval hints;
mandatory requirements exceeding the budget cause an explicit failure. Checkpoints support
continuity without reloading the entire conversation history.

Agent claims of completion or passing tests remain distinct from independently verified
evidence. Verification results bind a code snapshot and policy; after edits, older results
remain historical. Harnesses and tests receive no GitHub credentials. Verification runs
offline inside isolated containers, with dependency preparation in a separate credential-free phase.

[Usage collection](docs/external-usage.md) covers explicitly selected tasks and turns;
it does not imply access to every project's conversations. Missing metrics stay unknown.
There is no established token-savings percentage to promise.

## Client support and current limits

| Integration | Current support |
| --- | --- |
| Codex plugin | Four skills and six MCP tools, scoped to a workspace and account |
| Codex CLI / optional Codex SDK | Built-in coding harnesses invoked by RepoSteward |
| Claude Code / Copilot (VS Code) | CLI/file handoff and MCP configuration previews; native plugin packaging, managed runners and further client validation remain separate work |
| Local workbench | Read-only access to local projects and tasks; no public multi-user deployment |

The [client pilot record (中文)](docs/handoff-pilot-2026-09-05.zh-CN.md) specifies the versions
and scope of that trial. The long-term direction is more repository automation under
maintainer-owned rules: evidence-backed recommendations first, then broader actions.
[RFC #70](https://github.com/tiammomo/RepoSteward/issues/70) describes that direction;
unattended autonomous maintenance is not a current capability.

## Documentation

Most operational guides are currently in Chinese.

| What you need | Documentation |
| --- | --- |
| Installation, diagnostics and state upgrades | [Standalone installation](docs/local-installation.zh-CN.md) · [Backup and migration](docs/state-upgrades.zh-CN.md) |
| Plugins and session continuity | [Codex plugin](docs/agent-plugin.zh-CN.md) · [Existing agents](docs/coding-agent-assistance.zh-CN.md) |
| Code understanding and project browsing | [Reading guide](docs/project-understanding.zh-CN.md) · [Workbench](docs/local-workbench.zh-CN.md) |
| GitHub maintenance, queues, portfolio and cleanup | [Operator guide](docs/operator-guide.zh-CN.md) · [Configuration example](reposteward.example.toml) |
| Architecture, persistence and verification boundaries | [Architecture](docs/architecture.md) · [Project Draft Actions](docs/github-actions.md) |
| Usage, versions and releases | [Usage collection](docs/external-usage.md) · [Changelog](CHANGELOG.md) · [Release and rollback policy](docs/releases.md) |

## Version and contributing

The source is a **0.1.0 development baseline**. Configuration, schemas and public interfaces
may change before 1.0. Use `reposteward version` to inspect an installation and retain its
commit and wheel hash. Plugin content digests distinguish exports; they are not formal
release versions. Tags, GitHub Releases and changelog entries follow the [version policy](docs/releases.md).

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) before contributing.
Start from a reviewed Issue and use a focused PR on a separate branch. Run the required
verification in the hardened container. Code, documentation, reproducible bug reports
and feedback from actual use are welcome.

Licensed under [MIT](LICENSE). Report vulnerabilities privately through [SECURITY.md](SECURITY.md).
Legacy configuration and state locations from the former Starfix name remain readable where documented.
