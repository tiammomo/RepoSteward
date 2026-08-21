# Issue-to-PR lifecycle

## 1. Review the work item

1. Fetch the latest default branch, open Issues, PRs, and contribution guidance.
2. Confirm the problem is reproducible, in scope, not already fixed or claimed, and
   safe for public discussion.
3. For a new proposal, stage it as a GitHub Project Draft Issue. Re-read the online
   body, inspect duplicate and security results, then promote the exact reviewed digest.
4. Record observable behavior, evidence, scope, acceptance criteria, and exclusions.

## 2. Prepare a focused change

1. Create a short-lived branch and separate worktree from the latest intended base.
2. Reproduce first. Implement the smallest maintainable change and a regression test.
3. Preserve credential isolation, no-network verification, explicit public-write gates,
   and versioned protocol compatibility.
4. Avoid unrelated formatting, dependency, generated-file, or refactoring changes.

## 3. Review and verify

1. Inspect the complete diff, repository status, and commit range.
2. Run the focused regression test and the repository's full lint, format, test, CLI
   smoke, build, schema, and security checks that apply.
3. Check performance and compatibility when a hot path, persistence format, public CLI,
   or harness contract changes.
4. Create a signed, DCO-compliant Conventional Commit only after checks pass.

## 4. Publish through a PR

1. Re-read the remote Issue and competing PR state immediately before publishing.
2. Push only the feature branch. Open a Draft PR with `Closes #<number>` and include the
   problem, focused change, verification evidence, risks, and automation disclosure.
3. Do not merge while required checks, reviewer questions, or unresolved conversations
   remain. Never rewrite another contributor's branch without explicit permission.

## 5. Follow up and hand off

1. Classify CI failures as introduced, flaky, or inherited from the base branch before
   changing code. Record evidence when rerunning a job.
2. Answer review comments with the relevant code and test evidence; update the same
   focused PR when changes stay in scope, otherwise open a follow-up Issue.
3. Refresh the Context Pack and Checkpoint after material decisions or verification.
4. Before switching harness, account, or maintainer, export the portable bundle and
   state the exact next action, blockers, risks, HEAD, and observed tests.
