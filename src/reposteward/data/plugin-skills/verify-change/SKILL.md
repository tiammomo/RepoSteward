---
name: verify-change
description: Verify changes in a linked RepoSteward task or assess whether existing verification evidence still applies. Use trusted verification profiles and exact task snapshots.
---

# Verify a change

Read [connection.json](../../connection.json), call this plugin's `project` and
`context` tools, and confirm the target workspace and current run. Retrieve
`verification` with `action: profiles` to list the user's configured profiles.
Choose a profile relevant to the requested change. If none is suitable, report the
missing configuration; do not invent shell commands or edit policy to make it pass.

Inspect current evidence first. To execute a selected profile, use `verification`
with `action: request`, the current revision/snapshot and a fresh idempotency key.
The existing service runs in an isolated verifier; do not run public-repository
tests directly in the development directory. Read evidence by ID for failures.

If the task or code changes, a historical pass may no longer apply. Report both
outcome and current applicability, along with what was actually tested. After an
interrupted request, inspect the existing evidence; an idempotent retry returns
that attempt and does not prove tests ran again. A new verification attempt needs
a new key. Verification alone grants no publish or merge authority.
