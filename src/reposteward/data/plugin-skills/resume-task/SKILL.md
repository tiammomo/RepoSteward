---
name: resume-task
description: Resume a RepoSteward task after a break, context compaction or a change of coding client. Recover requirements, decisions and evidence, then save a scoped checkpoint.
---

# Resume a task

Read [connection.json](../../connection.json), call this plugin's `project` tool,
and confirm the requested workspace. Call `context` without a run ID to discover
its current task; use explicit `scope_paths` only when the work's scope is known.
Preserve required constraints, remaining work, decisions, blockers and the next
action. Retrieve omitted source evidence on demand; do not infer completion from
an old conversation or an exported package arriving late.

If there is no current task, inspect the user's requested Issue and project rules.
A new development task requires a reviewed open Issue and feature branch; use the
connection's `cli_prefix` argv plus `task start WORKSPACE --issue NUMBER
--reviewed-by LOGIN` only with the applicable existing authority. Do not invent
an Issue or task ID. Pure project reading can continue without creating a task.

Before handoff or after a meaningful milestone, inspect the current task and save
`checkpoint` with the returned run_id, expected_revision, current snapshot digest,
a new idempotency key and the concrete next action. Record completed/remaining
work, decisions and blockers; observed tests remain Agent claims until independently
verified. On conflict, re-read and reconcile; never overwrite a newer checkpoint.
Retry an interrupted checkpoint with the same key and identical payload.
