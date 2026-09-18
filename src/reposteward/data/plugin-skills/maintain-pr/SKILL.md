---
name: maintain-pr
description: Inspect CI, review feedback and merge blockers for a RepoSteward-managed pull request. Use for PR follow-up and scoped repair; preserve existing publication gates.
---

# Maintain a pull request

Read [connection.json](../../connection.json) and confirm the user's repository
with this plugin's `project` tool. Use its `context` to recover the current task.
For native managed PRs, use the connection's `cli_prefix` argv with `inspect RUN_ID`,
`follow-up RUN_ID`, `ci diagnose OWNER/REPO PR_NUMBER` and `merge-decision RUN_ID` as appropriate.
Read fresh structured facts and bounded failure evidence; an unknown result does
not mean success. These commands inspect or update local evidence, not the PR.

Distinguish failed CI, unresolved feedback, stale verification, missing review,
policy restrictions and a terminal PR. Propose or implement only the requested
repair under the repository's reviewed Issue and branch rules. Do not use native
repair on an external task that has not been adopted as a managed PR run.
An existing contributor PR without native registration remains a review task;
do not fabricate local run history or rewrite the contributor's branch.

Respect authorization already given for the exact action. Public writes use the
separate native review, submit and merge commands with their configured identity
and gates; MCP does not provide those operations. Do not change policy or substitute
raw API/Git writes to get past a rejection. After an ambiguous write, inspect its
durable attempt and exact remote state before deciding whether a retry is safe.
Report the current blocker and the concrete next action without repeatedly asking
for approval that the user has already provided.
