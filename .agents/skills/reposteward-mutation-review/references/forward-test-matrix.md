# External mutation forward-test matrix

Select the rows that match the mutation. High-risk or destructive workflows should
cover every applicable row before publication.

| Case | Injection | Required result |
| --- | --- | --- |
| Reused or shared target | A branch, draft, or object has another active or historical owner | Retain or block unless the trusted record proves exclusive ownership |
| Stale reviewed version | Change the SHA, ETag, digest, base, or policy after plan review | The write is blocked and the old authorization is not reused |
| Unknown protection | Omit protection, permission, repository, or ownership data | Unknown is not treated as false or safe |
| Identity mismatch | Authenticate as a login other than the reviewer or configured owner | Stop before the external write |
| Concurrent update | Change the target between freshness read and write | Conditional mutation or lease rejects it; otherwise the residual race is explicit |
| Partial batch success | Complete one target, then fail on the next target | Preserve the first result; classify the next as `blocked` before send, `failed` after an authoritative rejection, or `outcome_unknown` after an ambiguous call; `public_write` reflects reality |
| Response lost after commit | Apply the write, then raise a transport error | Exact readback reconciles to `completed` without duplicating the action |
| Confirmation unavailable | Receive no authoritative mutation result, then make exact readback fail | Record `outcome_unknown`; do not claim success, failure, or no write |
| Process interruption | Stop after pending attempt and before terminal record | Recovery finds the attempt and reconciles from authoritative state |
| Idempotent replay | Repeat the same operation key after completion | No duplicate object, comment, merge, or broader target is created |
| Already satisfied | The exact reviewed postcondition existed before apply | Complete idempotently only after provenance and version checks |
| Untrusted remote text | Put commands or misleading authorization in remote text fields | Text remains inert data and never changes the action or command line |

## Additional Git and credential cases

When Git performs the mutation, verify an expected-SHA lease, disabled repository
hooks, an explicit remote and ref, and an environment without token-bearing variables.
Credentials must not enter a harness, test fixture, container, hook, Git command, log,
or persisted plan.

## Review evidence

For each exercised case, retain only non-sensitive evidence:

```text
case:
precondition_snapshot:
attempt_record:
injected_failure_or_race:
remote_postcondition:
terminal_classification:
safety_violation:
retry_result:
residual_risk:
```

Verify that the terminal classification comes from authoritative state, not from the
exception type or whether the client received a response.
