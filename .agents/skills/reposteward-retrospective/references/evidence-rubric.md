# Evidence rubric

Use this rubric only after a candidate has been derived from the minimum relevant
history and checked for secrets.

## Authority levels

1. **Authoritative current fact:** current repository policy and source, fresh GitHub
   Issue or PR state, exact CI logs, signed commits, and RepoSteward audit records.
2. **Rebuildable projection:** a bounded history excerpt, final summary, thread index,
   or derived incident note with enough provenance to recheck.
3. **Unverified inference:** an interpretation that lacks current confirmation or an
   independent outcome.

Only current facts can satisfy a gate. Projections may explain why a candidate exists.
Inferences remain labeled and cannot become mandatory instructions.

All provenance is subject to the privacy boundary, not only transcript text. Replace
private project names, usernames, URLs, session identifiers, and local paths with a
non-sensitive scope label or pseudonym before they enter a versioned or public report.

Independent incidents must come from separate action attempts and must not be retries,
replays, summaries, or downstream symptoms of the same root event. Independent
verification is a separate observation of current authoritative state, not another
historical summary. Treat an incident as high-risk only when it can cause unauthorized
publication, credential disclosure, external state loss or corruption, or a materially
false audit of a write.

## Decision

Promote a candidate when all of the following hold:

- it is supported by at least two independent incidents, or one deterministically
  reproduced high-risk incident;
- it still applies after checking every current binding relevant to the named action;
- following it changes an observable decision or prevents a concrete failure;
- its trigger and exclusions distinguish it from existing skills;
- it has a measurable validation method and an explicit stale condition;
- the durable artifact contains no raw transcript, secret, or machine-local detail.

Quarantine a candidate when it is plausible but has only one non-deterministic example,
its scope or counterexample is unclear, or current validation is incomplete. Record
the evidence needed to decide later; do not add provisional requirements to an active
skill.

Reject a candidate when it is generic good practice, a personal preference, a
repository-independent answer Codex already handles, duplicated by current guidance,
contradicted by current facts, inseparable from sensitive data, or useful only on one
machine.

## Candidate record

Use a compact record rather than copying source material:

```text
candidate:
trigger:
sources: <non-sensitive IDs, projects, dates>
observed_failure_or_gain:
current_validation:
counterexample:
stale_when:
existing_overlap:
decision: reject | quarantine | update-existing | new-skill | product-issue
verification:
```

Use `update-existing` only for a new validated boundary within an existing trigger.
Use `reject` with no repository change when current guidance already contains the same
rule. For `product-issue`, state whether a matching reviewed Issue already exists so a
duplicate is not created.

## Forward tests

Use realistic prompts and inspect decisions and side effects.

- Duplicate case: repeated incidents show that a stale remote snapshot invalidates an
  approved action, and current code and maintainer guidance already enforce fresh
  bindings. Expected result: reject the duplicate repository change and route the live
  action to the existing maintenance workflow.
- Positive case: independent histories expose a decision-changing failure class that
  current code, documentation, and skills do not cover. Expected result: update the
  existing trigger or propose one focused skill or product Issue, with provenance,
  current validation, a counterexample, and a stale condition.
- Negative case: one session used a particular local path or command alias. Expected
  result: reject it as machine-specific rather than adding a project rule.
- Boundary case: a single high-risk failure has a deterministic reproducer. Expected
  result: it may be promoted, but only with the reproducer, current validation, narrow
  trigger, and reviewed Issue.

Treat a test that only checks headings, keywords, or generated prose as insufficient.
