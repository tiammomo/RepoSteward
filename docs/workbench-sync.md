# GitHub maintenance in the local workbench

Start `reposteward web` and open the session link printed by the command. From a
registered project, select **GitHub 维护** and **同步 GitHub**. The command first
persists a local operation, then reads GitHub through the configured REST client.
The **本地操作** page shows progress, failures, retries and the execution history.
Reloading a page reads the local database. It does not schedule another GitHub sync.

The workbench supports registered projects without a checkout or an execution
policy when their host matches the configured GitHub API. Reading those projects
does not grant permission to run code or submit contributions. Project onboarding
continues to use `reposteward project link` until the separate import capability is
available.

Use `reposteward web --read-only` to disable local commands and queue consumption.
The normal server resumes previously accepted local operations after restart. It
never consumes native prepare, submit, merge or cleanup tasks, and never schedules
periodic GitHub synchronization.

## What the views mean

- **PR 状态** combines an open PR window with independent reads of known PRs.
  A PR can be open, closed or merged. Disappearance from a list is not terminal
  evidence. A remote observation never creates a native merge execution record.
- **开放 Issue** excludes PR records returned by GitHub's Issues endpoint.
- **最近更新** is a bounded window of PRs sorted by their latest update. It is not
  a complete event history or a guarantee that every intervening event was seen.
- CI and review detail is loaded from local observations when expanded. Evidence
  must match the PR head; older review commit IDs remain visible as history.
- Each source shows its last attempt, last success, errors and coverage. The
  15-minute freshness indicator is derived from the successful observation time;
  opening the page does not make old facts fresh. Failed reads preserve the last
  successful observation. A conditional HTTP 304 confirms the cached response.
- Completed PR feedback remains in the maintenance overview as feedback after
  completion. Local feedback status and meaningful local failures are preserved.

A sync reads at most two pages of 50 entries per list, independently reads up to
50 known PRs, and reads CI/reviews for up to 15 open PRs. Each detail source has a
100-entry window; incomplete windows are explicit. A sync has a 120-request budget.
Counts describe the observed window; unknown remote totals remain unknown. The
operation summary reports omitted known PR and check scopes. List cursors bind to
a local snapshot; a new snapshot requires returning to the first page.

## Local operation and API contract

FastAPI exports the Pydantic contract to `frontend/openapi.json`; generated
TypeScript is consumed by the React client. The new versioned routes are:

| Route | Behavior |
| --- | --- |
| `GET /api/v1/github` | Project PR, Issue, activity window or PR detail |
| `GET /api/v1/operations` | Current account's local operation summaries |
| `GET /api/v1/operation` | One operation and its bounded stage/attempt history |
| `POST /api/v1/commands/github/sync` | Persist a project synchronization; return 202 |
| `POST /api/v1/commands/operations/cancel` | Cancel a pending operation |
| `POST /api/v1/commands/operations/retry` | Explicitly requeue a failed/cancelled operation |

Commands require the exact local Origin, session capability, JSON body (at most
64 KB), and an Idempotency-Key. Cancellation and retry also require the current
operation revision. No route accepts shell commands, GitHub credentials or remote
mutation instructions. Running operations cannot be cancelled through the UI.
GitHub rate limits persist a retry time shared by the account, including after a
server restart. Other failures require an explicit retry; successful source reads
remain available.

The existing SQLite queue stores native v1 envelopes unchanged and local v2
envelopes with account, project, action and immutable plan identities. Plan and
request digests are checked before execution. Account leases serialize remote reads;
queue generations fence stale workers before observation persistence. Each resumed
attempt reads again, so a crash after a successful read cannot duplicate a remote
write. This capability performs no remote writes.

## Upgrade and packaging

This capability adds state schema 25, after task lifecycle and verification recovery.
The browser and worker refuse an old database
rather than migrating it implicitly. Stop processes using the state, inspect
`reposteward state plan --expect-state-dir /absolute/state/directory`, then run
`reposteward state upgrade` with that exact directory and the plan digest printed by
the plan. The existing upgrade service makes and verifies a backup before migration.
An empty installation creates its database only after an explicit local command.

The React bundle remains included in the Python wheel; daily use requires no Node
process. Source development uses Vite's loopback proxy with the same browser
session. GitHub authentication remains inside the backend REST client.
