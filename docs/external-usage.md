# External Coding Agent usage collection

Use this local collector when developing with an existing Codex session. It
associates explicitly selected **turns** with a registered external task or an
adopted workspace run. It does not launch an agent, access GitHub, scan session
history, or save prompts, responses, tool arguments, or credentials.

## Collect and refresh

Start a reviewed external task with `reposteward task start`, or use the `RUN_ID`
from `reposteward adopt`. Select the local rollout file and the `turn_id` from
its `turn_context` metadata. Select only turns devoted to this Issue. A turn
mixing multiple tasks cannot be apportioned reliably and should be left unbound.

```bash
reposteward usage collect RUN_ID \
  --codex-session /path/to/selected-rollout.jsonl \
  --turn-id SELECTED_TURN_ID

# Register more explicit turns using another --turn-id (or a later invocation).
# Refresh all sources already bound to this WorkItem, without discovering others:
reposteward usage collect RUN_ID

reposteward usage external-report owner/repository --issue 123 --include-turns
reposteward usage external-report owner/repository --group-by model
```

Repeat collection at task checkpoints and after the selected turn completes.
There is no background watcher. Reports show the last successfully collected
snapshot, its collection time, and whether each turn completed, was interrupted,
or remains in progress. A running turn's values are provisional. A successor run
for the same WorkItem refreshes the same records; a different WorkItem cannot
claim an already bound session/turn pair. Cwd is checked against the task's
repository and configured GitHub host, allowing another clone or worktree of
that project. This verifies a local association, not that the source is authentic.

## Counting and uncertainty

Codex `token_count` updates contain cumulative **session** totals. The adapter
subtracts the latest baseline before the selected turn from its last observed
update, checking intermediate values for regression. Repeated updates are not
summed. Repeated `turn_context` records within a turn do not reset the baseline.
Identical `session_meta` records appended when resuming also preserve the baseline.
Missing baseline or individual counters produce `null` and explicit unknown
reasons; the collector never assumes that a partial session starts at zero.
Consequently, the first turn of a rollout may have unknown usage.

Cached input is a subset of input, and reasoning output is a subset of output.
Neither is added again to the total. Tool-call counts and active work duration
remain unknown in this adapter. A model change within a turn makes per-model
attribution and price unknown. A cache hit count alone does not establish Token
or monetary savings relative to another development workflow.

`cost_estimate` uses only the trusted user's configured `observability.prices`
entries with harness `codex-local`, a matching model, and effective date. Missing
prices or required metrics remain unknown; nonzero cache-write counts currently
have unsupported pricing. Estimates are for the observed counters and are not
subscription charges or provider invoices. This report is separate from native
`usage report`, avoiding a second count of managed Harness runs.

## Compatibility, persistence, and recovery

Adapter v1 supports the locally verified Codex CLI **0.153.0** rollout shape.
Rollouts are an internal client format. Other versions or malformed metadata
fail explicitly and require adapter review; they are not silently interpreted
as zero. The official [non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode)
and [app-server interface](https://learn.chatgpt.com/docs/app-server) expose other
usage events; this adapter does not claim their formats are interchangeable.

Collection streams at most 256 MiB, one million records, and 4 MiB per line from
one explicitly selected regular file. The unfinished final JSONL line is retried
on the next refresh. Previously read complete bytes must remain unchanged.
Truncation, rewriting, ambiguous turn ordering, changed source identity, and
counter resets fail without replacing existing observations. Restore the original
source before retrying; a moved source path is not automatically rebound.

The independent schema-v1 `external-usage.sqlite3` in the effective state
directory stores source paths, prefix hashes, session/turn identities, task
associations, counters, model/version/timestamps, and normalized observation
history. New databases use owner-only permissions. It does not migrate the core
task database. Read-only reports do not create either database or initialize a
GitHub client. Collection is atomic per source; retrying after a later source
fails is safe. Hashes support local integrity checks, not billing authenticity.

Keep this database and source files private and outside Git. Reports cover only
explicitly selected turns. Real workflow savings comparisons (P2), additional
agent adapters, and automatic collection should follow actual usage evidence.
