# Workspace scans in the local workbench

Open a registered project's workspace, choose **预览扫描范围**, inspect the
file/byte coverage and omissions, then choose **确认扫描工作区**. The operation
page persists progress across refreshes and links back to the reading guide.
Changing the selected workspace creates a different plan and index identity.

A preview reads static source inventory without creating an index or ledger and
without constructing a GitHub client. The explicit authenticated command queues
`workspace.scan` in the local operation family. It binds the account, project,
binding, directory identity, HEAD, dirty state, source content, parser version and
limits. Reusing a request key returns the original accepted operation; using it
with a different plan fails. Changed source requires a fresh preview.

Scans reuse the bounded static index: at most 2,000 files, 512 KiB per file and
20 MiB total. Sensitive paths, links, generated content, binary files and material
outside the inventory limits are omitted with coverage reasons. Repository code
is not executed. Dirty content is included and can invalidate a guide even when
HEAD is unchanged. References from another workspace are rejected.

`scan_started`, `index_published` and the final summary are stored in the operation
ledger. Parsing checks the worker lease, cancellation and account scope; index
publication holds the queue transaction across the atomic file replacement. A
worker that lost its lease cannot publish over a newer generation. Readers see a
complete index generation. A failure before publication preserves the previous
index; an interruption after publication can leave an index with an incomplete
operation, which a retry reconciles by scanning the same bound source again.
Source edits after publication make that evidence stale, not verified current.

The worker can resume pending scans after restart and process them during GitHub
rate backoff. Native submit/merge tasks remain outside the local worker. A damaged
index requires the explicit rebuild option; stale source requires a new plan.
GET, navigation and ordinary server startup never enqueue or scan a workspace.

The API exposes `GET /api/v1/scan-plan` and
`POST /api/v1/commands/workspaces/scan`. The latter requires the local session,
Origin, idempotency key and exact preview revision. Read-only sessions can preview
but cannot enqueue. No endpoint accepts a shell command or an arbitrary index
output path. The existing `reposteward understand scan` CLI remains available.
