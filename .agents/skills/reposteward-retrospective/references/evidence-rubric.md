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

An assertion in a request or summary that events were independent does not establish
independence. Require non-sensitive provenance for the observation date, action
attempt, and root condition. If those facts cannot be checked without retaining
sensitive material, quarantine or reject the candidate instead of counting it.

## Evidence, decision, and route

First classify `evidence_state` independently:

- `weak`: the source, independence, current binding, or outcome cannot be checked;
- `corroborated`: at least two independently attributable incidents support the same
  bounded condition and result;
- `deterministic-high-risk`: one isolated reproducer establishes a high-risk failure
  and a nearby counterexample establishes its boundary.

Then set `decision: promote` only when all of the following hold:

- it is supported by at least two independent incidents, or one deterministically
  reproduced high-risk incident;
- it still applies after checking every current binding relevant to the named action;
- following it changes an observable decision or prevents a concrete failure;
- its trigger and exclusions distinguish it from existing skills;
- it has a measurable validation method and an explicit stale condition;
- the durable artifact contains no raw transcript, secret, or machine-local detail.

Set `decision: quarantine` when a candidate is plausible but has only one
non-deterministic example, its sources cannot establish independence, its scope or
counterexample is unclear, or current validation is incomplete. Record the evidence
needed to decide later; do not add provisional requirements to an active skill.

Set `decision: reject` when a candidate is generic good practice, a personal
preference, a repository-independent answer Codex already handles, duplicated by
current guidance, contradicted by current facts, inseparable from sensitive data, or
useful only on one machine. Evidence may be weak and the decision still be `reject`
when a current authoritative rule already makes the proposed repository change a
duplicate.

Record the durable route separately; whether anything changes now is controlled by the
decision. Use `route: none` when no artifact should be created, `existing-skill` for a
missing boundary inside an existing trigger, `new-skill` for a distinct operator
request surface, `product-issue` when the lesson requires deterministic behavior,
persistence, or protocol support, and `private-security` for a possible vulnerability
or credential exposure governed by `SECURITY.md`. A quarantined candidate may name a
route for later evaluation, but it must not change an active artifact.

## Candidate record

Use a compact record rather than copying source material:

```text
candidate:
trigger:
sources: <non-sensitive IDs, scopes, dates, action attempts>
authority_levels:
independence_check:
evidence_state: weak | corroborated | deterministic-high-risk
observed_failure_or_gain:
current_validation:
counterexample:
stale_when:
existing_overlap:
decision: reject | quarantine | promote
route: none | existing-skill | new-skill | product-issue | private-security
verification:
```

Use `existing-skill` only for a new validated boundary within an existing trigger. For
`product-issue`, state whether a matching reviewed Issue already exists so a duplicate
is not created. When only some actions lack code coverage, list those exact actions;
do not infer a system-wide gap from uneven implementations.

## Forward tests

Use realistic held-out prompts and inspect decisions and side effects. Do not reuse the
candidate's wording or provide the evaluator with the intended answer. Include:

- one supported candidate whose decision changes after checking current facts;
- one apparent repetition that turns out to be a retry, downstream symptom, or rule
  already enforced elsewhere;
- one candidate whose provenance is too weak or sensitive to establish independence;
- one negative case from a different failure class that should not trigger this skill;
- for a high-risk single incident, both a deterministic reproducer and a nearby case
  where the reproducer's preconditions do not hold.

Treat a test that only checks headings, keywords, or generated prose as insufficient.
