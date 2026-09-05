# RepoSteward

<p align="right"><a href="README.zh-CN.md">简体中文</a></p>

> Govern GitHub repositories with coding models under rules you own.

RepoSteward is a local-first control plane between GitHub, coding harnesses, and an
isolated verifier. It keeps repository policy, task state, review evidence, and public
write gates outside the model session.

The current 0.1 release turns reviewed GitHub Issues into verified, human-reviewed pull
requests. The long-term direction is broader: models should be able to triage Issues,
implement and review changes, and advance low-risk pull requests under goals and risk
limits set by the maintainer. That autonomous steward is a roadmap, not a feature in
the current release.

<p align="center">
  <img src="docs/assets/reposteward-lifecycle.svg" width="100%" alt="RepoSteward workflow: a reviewed GitHub Issue enters a credential-free coding workspace, isolated verification produces evidence, a maintainer reviews the result, and a separate gate publishes a Draft PR for CI and reviewer follow-up.">
</p>

<p align="center"><sub>The diagram has an editable <a href="docs/assets/reposteward-lifecycle.excalidraw">Excalidraw source</a>.</sub></p>

## Understand a project before changing it

Read a local clone or worktree with `reposteward understand scan PATH`, then
`reposteward understand guide PATH` or `reposteward understand query PATH "symbol or problem"`.
The guide links project declarations, Python static relationships and a suggested reading
route to versioned source evidence. Reading does not require a linked task or GitHub login.
See the [project understanding guide](docs/project-understanding.zh-CN.md) for limits and MCP access.

## Why RepoSteward exists

Coding harnesses are good at understanding code and editing a workspace. A model
session is a poor place to own durable repository policy, GitHub credentials, public
write authority, or the record of what was reviewed.

RepoSteward keeps those responsibilities in a deterministic control plane:

- it freezes the Issue, base commit, repository instructions, and policy before work;
- it gives the harness a credential-free worktree and a bounded Context Pack;
- it runs allowed verification commands in an isolated container;
- it records checkpoints, evidence, resource use, and GitHub facts;
- it rechecks identity and remote state before a public write;
- it separates preparing a change from publishing or merging it.

The project is designed for individual maintainers and small teams that maintain
several repositories with coding agents. Contributor workflows are supported, but the
maintainer workflow is the primary product path.

RepoSteward works alongside coding models, CI, and GitHub project management. Its job
is to govern the maintenance workflow, not to produce a large number of pull requests.

## Current product and long-term direction

| Area | Available in 0.1 | Direction |
| --- | --- | --- |
| Issue intake | Local drafts, duplicate search, reviewed Project Draft promotion | Model triage, value scoring, reversible closing, and policy-gated creation |
| Implementation | Codex CLI or optional Codex SDK prepares a focused local commit | Separate Steward, Builder, and Reviewer model roles |
| Verification | Allowed commands run in a credential-free, no-network verifier | Risk tiers and evidence-based autonomy levels |
| Publication | A maintainer reviews a packet and runs a separate submit command | Low-risk PR stages can advance under standing repository policy |
| Follow-up | CI and review changes are ingested incrementally for repair | Routine repository operation with exception-based human supervision |
| Merge | Read-only eligibility, optional owner attestation, explicit merge gate | Merge permission earned per repository and revoked after poor outcomes |

Current safety gates remain authoritative until a reviewed Issue changes the
implementation. [RFC #70](https://github.com/tiammomo/RepoSteward/issues/70) records
the model-governed repository direction.

## Install

RepoSteward requires Python 3.12 or newer, uv, Git, Docker, GitHub CLI, and a logged-in
Codex CLI.

```bash
git clone https://github.com/tiammomo/RepoSteward.git
cd RepoSteward
uv sync
uv run reposteward init
uv run reposteward --help
```

`init` reads the current `gh auth` and Git identity, then writes user-owned settings
to `~/.config/reposteward/config.toml`. It does not store a GitHub token in that
file.

Add a repository in maintainer mode:

```bash
cd /path/to/repository
uv run reposteward repo add owner/repository --mode maintainer
```

The command creates a machine-local `.reposteward.toml` and excludes it through
`.git/info/exclude`. Fill in the repository's bootstrap and verification allowlists,
then build the verifier and check the environment:

```bash
uv run reposteward image build
uv run reposteward doctor
```

See the [example configuration](reposteward.example.toml) and the
[detailed Chinese operator guide](docs/operator-guide.zh-CN.md) for the full setup.

## Prepare the first reviewed change

Start with an open, reviewed Issue in a repository you maintain:

```bash
uv run reposteward gate owner/repository 123
uv run reposteward prepare owner/repository 123
```

`prepare` clones the latest default branch into an isolated workspace, invokes the
configured harness, runs the allowed verifier commands, checks the diff, and creates a
local commit. It returns a compact Review Packet with the commit, diff size, risks,
verification status, logs, and resource use.

Inspect the result without publishing it:

```bash
uv run reposteward inspect RUN_ID
uv run reposteward logs RUN_ID
```

After reviewing the exact diff and evidence, publish through a separate command:

```bash
REPOSTEWARD_ENABLE_SUBMIT=1 \
  uv run reposteward submit owner/repository 123 \
  --reviewed-by your-github-login
```

The default result is a Draft PR. The environment gate, configured identity, actual
GitHub identity, current head, base, policy, and remote PR state must all match.

After publication:

```bash
uv run reposteward follow-up RUN_ID
uv run reposteward repair RUN_ID
uv run reposteward merge-decision RUN_ID
```

`follow-up` ingests only changed GitHub facts. `repair` prepares a new verified
commit when actionable feedback needs code changes. `merge-decision` records a
deterministic eligibility result without merging.

## Maintainer views

RepoSteward also exposes repository-level, mostly read-only views:

```bash
uv run reposteward inbox --repo owner/repository --format text
uv run reposteward portfolio inspect owner/repository --format text
uv run reposteward portfolio plan owner/repository --format text
uv run reposteward branch-cleanup plan owner/repository --format text
uv run reposteward batch plan owner/repository --format text
uv run reposteward trace owner/repository 123 --format text
uv run reposteward usage report owner/repository
uv run reposteward storage stats --repo owner/repository
uv run reposteward benchmark run --output .artifacts/benchmark.json
```

The persistent queue and batch planner store bounded control-plane intent. They do not
turn on submit, owner-attestation, or merge permissions.

For maintainer same-repository policies, `branch-cleanup plan` builds a read-only,
digest-bound backlog from submitted runs and successful merge audits. Applying a
reviewed plan additionally requires `branch_cleanup = true`, the
`REPOSTEWARD_ENABLE_BRANCH_CLEANUP=1` gate, matching configured/reviewed/authenticated
identity, and fresh exact branch/PR facts. The leased Git delete uses the host SSH
identity; ambiguous results stay pending for read-only reconciliation and never change
the authoritative merge result.

`benchmark run` executes RepoStewardBench v0 entirely offline. Its versioned fixtures
cover safety gates, context bounds, maintainer attention, recovery, and scale. Every
scenario is repeated to detect nondeterminism, critical safety and recovery scenarios
form a hard gate, and machine-specific duration is reported only as informational data.
Use `--baseline PREVIOUS.json` to compare semantic metric deltas between commits.

## Trust boundaries

- GitHub credentials stay in the control plane. They are not passed to the harness,
  tests, repository hooks, Git push, or Docker containers.
- Git clone and push use the host SSH identity. GitHub API calls use the configured
  maintainer identity.
- Issue bodies, repository files, comments, reviews, and imported context are treated
  as untrusted input.
- Verification commands run without network access. Dependency bootstrap can use
  network access in a separate credential-free phase.
- Project configuration can tighten user-owned limits but cannot loosen credential,
  path, identity, or public-write controls.
- Incomplete GitHub facts, changed heads, changed policy, competing work, high-risk
  paths, or oversized diffs fail closed.
- Public writes have separate environment gates and fresh-state checks. A queue cannot
  enable those gates on its own.

GitHub writes use the configured maintainer identity. RepoSteward keeps model,
policy, evidence, intent, and result provenance in its local audit state.

## Portable task state

Every prepared change has a versioned Context Pack, append-only Checkpoints, and a
harness run record. Native harness sessions can speed up recovery, but they are not the
source of truth.

```bash
uv run reposteward context inspect RUN_ID
uv run reposteward context export RUN_ID --output handoff.json
uv run reposteward context import handoff.json
```

The bundle contains digests and bounded task facts, not account credentials. Imported
content remains untrusted.

## Roadmap: Shadow Steward first

The first milestone toward model-governed maintenance is a read-only Shadow Steward.
It will inspect Issues, pull requests, CI, reviews, and a human-owned governance
charter, then propose actions with evidence, confidence, and the policy used.

Autonomy is intended to advance by repository:

1. shadow recommendations with no GitHub writes;
2. reversible Issue labeling and closing;
3. policy-gated Issue creation and Draft PR publication;
4. independent model review and Ready transitions;
5. low-risk merge after CI, freshness checks, and an observation period.

Each level must be earned from measured results and can be downgraded automatically.
Security, permissions, releases, governance changes, and other high-risk work continue
to escalate to a person. Models may propose policy changes but cannot change their own
authority.

## Documentation

| Need | Document |
| --- | --- |
| Chinese product entry | [README.zh-CN.md](README.zh-CN.md) |
| Detailed commands and operating procedures | [Chinese operator guide](docs/operator-guide.zh-CN.md) |
| Components, persistence, and harness contracts | [Architecture](docs/architecture.md) |
| Project Draft Issue review with GitHub Actions | [GitHub Actions](docs/github-actions.md) |
| Full project configuration | [Example TOML](reposteward.example.toml) |
| Contribution workflow | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Private security reporting | [SECURITY.md](SECURITY.md) |
| Long-term positioning decision | [RFC #70](https://github.com/tiammomo/RepoSteward/issues/70) |

## Project status

RepoSteward is at version 0.1. Configuration, schemas, and public interfaces may change
before 1.0. The built-in harnesses are Codex CLI and the optional Codex SDK.
Claude Code, DeepSeek, and autonomous repository governance do not have built-in
implementations yet.

The project is MIT licensed. It was previously named Starfix; legacy configuration and
state locations remain readable where documented.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uv run reposteward --help
uv run reposteward benchmark run
uv build
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Report vulnerabilities
through [SECURITY.md](SECURITY.md), not a public Issue.

For using RepoSteward with existing coding agents, see the [assistance guide (中文)](docs/coding-agent-assistance.zh-CN.md) and [actual client pilot](docs/handoff-pilot-2026-09-05.zh-CN.md).
