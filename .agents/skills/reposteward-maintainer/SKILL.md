---
name: reposteward-maintainer
description: Maintain RepoSteward or operate its reviewed Issue-to-PR workflow, including Issue proposals, focused implementation, CI and reviewer follow-up, and cross-harness handoff. Use for RepoSteward repository maintenance and RepoSteward-managed contributions; do not use it to bypass code-enforced publication, credential, or verification gates.
---

# RepoSteward Maintainer

Use this skill for maintenance judgment and handoff. Let RepoSteward code own the
state machine, credentials, digests, storage, verification, and GitHub writes.

## Invariants

- Start every code change from a reviewed open Issue. Keep security reports private.
- Work on a dedicated branch or worktree. Never commit or push directly to `main`.
- Keep one PR focused on one Issue and link it with `Closes #<number>`.
- Read the latest remote Issue, PR, review, and CI state before any public write.
- Use the explicit RepoSteward review and publication gates; never infer approval.
- Export a Context Pack and Checkpoint before changing account, machine, or harness.
- Never place credentials, local state, harness caches, or target repositories in Git.

For the end-to-end procedure, read [references/lifecycle.md](references/lifecycle.md).
