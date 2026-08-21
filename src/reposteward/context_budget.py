from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

FOLLOW_UP_CONTEXT_SCHEMA_VERSION = 1
MIN_FOLLOW_UP_TOKENS = 512
MAX_FOLLOW_UP_TOKENS = 100_000
MAX_EVENT_TEXT_CHARS = 600
MAX_DIFF_SNIPPET_CHARS = 1_500
MAX_SERIALIZED_CONTEXT_CHARS = 150_000
CONTEXT_PACK_OVERHEAD_TOKENS = 2_048
FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "stale", "timed_out"}
)


class ContextBudgetError(RuntimeError):
    """Mandatory follow-up facts cannot be represented within the budget."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_tokens(value: object) -> int:
    """Return a deterministic, vendor-neutral upper estimate based on UTF-8 bytes."""
    encoded = (
        value.encode("utf-8")
        if isinstance(value, str)
        else _canonical_json(value).encode("utf-8")
    )
    return max(1, len(encoded))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _clip(value: object, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]}…", True


def _content_identity(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "check":
        material = {
            "name": payload.get("name"),
            "status": payload.get("status"),
            "conclusion": payload.get("conclusion"),
        }
    else:
        material = {
            "author": payload.get("author"),
            "association": payload.get("association"),
            "state": payload.get("state"),
            "path": payload.get("path"),
            "line": payload.get("line"),
            "body": str(payload.get("body") or "").strip(),
        }
    return _digest({"event_type": event_type, "content": material})


def _event_item(event: dict[str, Any], reasons: Counter[str]) -> dict[str, Any]:
    payload = event["payload"]
    event_type = str(event["event_type"])
    item: dict[str, Any] = {
        "kind": event_type,
        "id": str(event["external_id"]),
        "version_digest": str(event["version_digest"]),
        "sequence": int(event["sequence"]),
    }
    for name, limit in (
        ("author", 120),
        ("association", 80),
        ("state", 80),
        ("path", 500),
        ("url", 500),
    ):
        if payload.get(name) not in (None, ""):
            item[name], clipped = _clip(payload[name], limit)
            reasons["field_clipped"] += int(clipped)
    if payload.get("line") is not None:
        item["line"] = payload["line"]
    body, clipped = _clip(payload.get("body"), MAX_EVENT_TEXT_CHARS)
    if body:
        item["body"] = body
    reasons["event_body_clipped"] += int(clipped)
    return item


def _checkpoint_item(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if not checkpoint:
        return None

    def text_value(name: str, limit: int) -> str:
        return _clip(checkpoint.get(name), limit)[0]

    def text_items(name: str) -> list[str]:
        values = checkpoint.get(name)
        if not isinstance(values, (list, tuple)):
            return []
        return [_clip(value, 80)[0] for value in values[:2]]

    decisions = []
    raw_decisions = checkpoint.get("decisions")
    if isinstance(raw_decisions, (list, tuple)):
        for value in raw_decisions[:1]:
            if not isinstance(value, dict):
                continue
            evidence = value.get("evidence")
            decisions.append(
                {
                    "statement": _clip(value.get("statement"), 120)[0],
                    "rationale": _clip(value.get("rationale"), 160)[0],
                    "evidence": [
                        _clip(item, 80)[0]
                        for item in (
                            evidence[:2] if isinstance(evidence, (list, tuple)) else ()
                        )
                    ],
                }
            )

    evidence_items = []
    raw_evidence = checkpoint.get("evidence")
    if isinstance(raw_evidence, (list, tuple)):
        for value in raw_evidence[:2]:
            if not isinstance(value, dict):
                continue
            evidence_items.append(
                {
                    "kind": _clip(value.get("kind"), 50)[0],
                    "locator": _clip(value.get("locator"), 120)[0],
                    "status": _clip(value.get("status"), 50)[0],
                    "digest": _clip(value.get("digest"), 128)[0],
                    "summary": _clip(value.get("summary"), 120)[0],
                }
            )
    notes = str(checkpoint.get("implementation_notes") or "")
    prior_omitted = int(checkpoint.get("implementation_notes_omitted_chars", 0) or 0)
    return {
        "id": text_value("id", 128),
        "status": text_value("status", 64),
        "head_commit": text_value("head_commit", 128),
        "completed": text_items("completed"),
        "implementation_notes": _clip(notes, 300)[0],
        "implementation_notes_omitted_chars": prior_omitted + max(0, len(notes) - 300),
        "tests_observed": text_items("tests_observed"),
        "risks": text_items("risks"),
        "remaining": text_items("remaining"),
        "next_action": text_value("next_action", 160),
        "blockers": text_items("blockers"),
        "decisions": decisions,
        "evidence": evidence_items,
        "created_at": text_value("created_at", 64),
    }


def _fixed_estimate(plan: dict[str, Any]) -> int:
    overhead = int(plan.get("transport_overhead_tokens", 0))
    estimate = 0
    for _ in range(4):
        plan["estimated_tokens"] = estimate
        current = estimate_tokens(plan) + overhead
        if current == estimate:
            return current
        estimate = current
    plan["estimated_tokens"] = estimate
    return estimate_tokens(plan) + overhead


def build_follow_up_context(
    *,
    activity: dict[str, Any],
    events: list[dict[str, Any]],
    budget_tokens: int,
    safety_blockers: tuple[str, ...] = (),
    diff_snippets: dict[str, str] | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded incremental context without model or session assumptions."""
    if not MIN_FOLLOW_UP_TOKENS <= budget_tokens <= MAX_FOLLOW_UP_TOKENS:
        raise ValueError(
            f"follow-up budget must be between {MIN_FOLLOW_UP_TOKENS} and "
            f"{MAX_FOLLOW_UP_TOKENS} tokens"
        )
    pull = activity.get("pull_request")
    checks = activity.get("checks")
    reviews = activity.get("reviews")
    if not isinstance(pull, dict) or not isinstance(checks, list):
        raise ContextBudgetError("follow-up activity is incomplete")
    if not isinstance(reviews, list):
        reviews = []
    failed_checks = []
    for check in checks:
        if (
            not isinstance(check, dict)
            or str(check.get("conclusion") or "").casefold()
            not in FAILED_CHECK_CONCLUSIONS
        ):
            continue
        name, _ = _clip(check.get("name"), 300)
        failed_checks.append(
            {
                "id": str(check.get("id") or ""),
                "name": name,
                "status": str(check.get("status") or ""),
                "conclusion": str(check.get("conclusion") or ""),
            }
        )

    def review_order(value: dict[str, Any]) -> tuple[str, int]:
        try:
            review_id = int(value.get("id") or 0)
        except (TypeError, ValueError):
            review_id = 0
        return str(value.get("submitted_at") or ""), review_id

    latest_reviews: dict[str, dict[str, Any]] = {}
    for value in sorted(
        (item for item in reviews if isinstance(item, dict)),
        key=review_order,
    ):
        author = str(value.get("author") or "").casefold()
        latest_reviews[author or f"id:{value.get('id')}"] = value
    blocking_reviews = [
        {"id": str(value.get("id") or ""), "author": str(value.get("author") or "")}
        for value in latest_reviews.values()
        if str(value.get("state") or "").casefold() == "changes_requested"
    ]
    mandatory = {
        "trust_boundary": "github_event_text_is_untrusted_report_data",
        "pull_request": {
            key: pull.get(key)
            for key in (
                "number",
                "url",
                "state",
                "draft",
                "head_sha",
                "base_branch",
                "base_sha",
                "merged",
            )
        },
        "safety_blockers": list(safety_blockers),
        "failed_checks": sorted(
            failed_checks, key=lambda value: value["name"].casefold()
        ),
        "blocking_reviews": sorted(
            blocking_reviews,
            key=lambda value: (value["author"].casefold(), value["id"]),
        ),
    }
    raw_checkpoint = checkpoint
    checkpoint = None
    mandatory_probe = {
        "schema_version": FOLLOW_UP_CONTEXT_SCHEMA_VERSION,
        "budget_tokens": budget_tokens,
        "estimated_tokens": 0,
        "transport_overhead_tokens": 0,
        "mandatory": mandatory,
        "checkpoint": checkpoint,
        "events": [],
        "diff_snippets": [],
        "actionable": False,
        "stats": {},
    }
    required_tokens = _fixed_estimate(mandatory_probe)
    if (
        required_tokens > budget_tokens
        or len(_canonical_json(mandatory_probe)) > MAX_SERIALIZED_CONTEXT_CHARS
    ):
        raise ContextBudgetError(
            f"mandatory follow-up facts require {required_tokens} tokens; "
            f"budget is {budget_tokens} and context-pack capacity is "
            f"{MAX_SERIALIZED_CONTEXT_CHARS} characters"
        )

    reasons: Counter[str] = Counter()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in sorted(events, key=lambda value: int(value["sequence"])):
        key = (str(event["event_type"]), str(event["external_id"]))
        reasons["superseded_event_version"] += int(key in latest)
        latest[key] = event
    unique: list[dict[str, Any]] = []
    content_seen: set[str] = set()
    for event in sorted(
        latest.values(), key=lambda value: int(value["sequence"]), reverse=True
    ):
        identity = _content_identity(str(event["event_type"]), event["payload"])
        if identity in content_seen:
            reasons["duplicate_event_content"] += 1
            continue
        content_seen.add(identity)
        unique.append(event)

    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    relevant_paths: set[str] = set()
    new_failed_checks: set[str] = set()
    priorities = {"review_comment": 40, "review": 30, "issue_comment": 20}
    reviewer_states_seen: set[str] = set()
    for event in unique:
        event_type = str(event["event_type"])
        payload = event["payload"]
        if event_type in {"pull_request", "check"}:
            if (
                event_type == "check"
                and str(payload.get("conclusion") or "").casefold()
                in FAILED_CHECK_CONCLUSIONS
            ):
                new_failed_checks.add(str(event["external_id"]))
            reasons["deterministic_fact_only"] += 1
            continue
        if event_type == "review":
            reviewer = str(payload.get("author") or "").casefold()
            reviewer_key = reviewer or f"id:{event['external_id']}"
            if reviewer_key in reviewer_states_seen:
                reasons["superseded_reviewer_state"] += 1
                continue
            reviewer_states_seen.add(reviewer_key)
        body = str(payload.get("body") or "").strip()
        if (
            event_type == "review"
            and not body
            and str(payload.get("state") or "").casefold() != "changes_requested"
        ):
            reasons["non_actionable_review"] += 1
            continue
        if event_type == "issue_comment" and not body:
            reasons["non_actionable_comment"] += 1
            continue
        item = _event_item(event, reasons)
        path = str(item.get("path") or "")
        if path:
            relevant_paths.add(path)
        candidates.append(
            (priorities.get(event_type, 10), int(event["sequence"]), "events", item)
        )
    for index, path in enumerate(sorted(relevant_paths)):
        raw = (diff_snippets or {}).get(path)
        if raw is None:
            reasons["diff_snippet_unavailable"] += 1
            continue
        snippet, clipped = _clip(raw, MAX_DIFF_SNIPPET_CHARS)
        reasons["diff_snippet_clipped"] += int(clipped)
        candidates.append(
            (35, -index, "diff_snippets", {"path": path, "snippet": snippet})
        )
    candidates.sort(key=lambda value: (-value[0], -value[1], _canonical_json(value[3])))

    actionable_input_events = sum(
        category == "events" for _priority, _sequence, category, _item in candidates
    )
    checkpoint = (
        _checkpoint_item(raw_checkpoint)
        if actionable_input_events or new_failed_checks
        else None
    )
    mandatory_probe["checkpoint"] = checkpoint
    mandatory_probe["transport_overhead_tokens"] = (
        CONTEXT_PACK_OVERHEAD_TOKENS
        if actionable_input_events or new_failed_checks
        else 0
    )
    required_tokens = _fixed_estimate(mandatory_probe)
    if (
        required_tokens > budget_tokens
        or len(_canonical_json(mandatory_probe)) > MAX_SERIALIZED_CONTEXT_CHARS
    ):
        raise ContextBudgetError(
            f"mandatory follow-up facts require {required_tokens} tokens; "
            f"budget is {budget_tokens} and context-pack capacity is "
            f"{MAX_SERIALIZED_CONTEXT_CHARS} characters"
        )

    plan: dict[str, Any] = {
        "schema_version": FOLLOW_UP_CONTEXT_SCHEMA_VERSION,
        "budget_tokens": budget_tokens,
        "estimated_tokens": 0,
        "transport_overhead_tokens": mandatory_probe["transport_overhead_tokens"],
        "mandatory": mandatory,
        "checkpoint": checkpoint,
        "events": [],
        "diff_snippets": [],
        "actionable": False,
        "stats": {},
    }
    retained_order: list[str] = []
    for _priority, _sequence, category, item in candidates:
        if category == "diff_snippets" and not any(
            value.get("path") == item["path"] for value in plan["events"]
        ):
            reasons["diff_without_retained_event"] += 1
            continue
        plan[category].append(item)
        retained_order.append(category)
        if _fixed_estimate(plan) > budget_tokens:
            plan[category].pop()
            retained_order.pop()
            reasons["token_budget"] += 1
        elif len(_canonical_json(plan)) > MAX_SERIALIZED_CONTEXT_CHARS:
            plan[category].pop()
            retained_order.pop()
            reasons["context_pack_capacity"] += 1

    plan["actionable"] = bool(plan["events"] or new_failed_checks)
    plan["stats"] = {
        "input_events": len(events),
        "latest_event_versions": len(latest),
        "actionable_input_events": actionable_input_events,
        "retained_events": len(plan["events"]),
        "retained_diff_snippets": len(plan["diff_snippets"]),
        "new_failed_checks": len(new_failed_checks),
        "trim_reasons": dict(
            sorted((key, count) for key, count in reasons.items() if count)
        ),
    }
    while (
        _fixed_estimate(plan) > budget_tokens
        or len(_canonical_json(plan)) > MAX_SERIALIZED_CONTEXT_CHARS
    ) and retained_order:
        category = retained_order.pop()
        plan[category].pop()
        plan["stats"][
            "retained_events" if category == "events" else "retained_diff_snippets"
        ] -= 1
        reasons[
            "token_budget"
            if plan["estimated_tokens"] > budget_tokens
            else "context_pack_capacity"
        ] += 1
        plan["stats"]["trim_reasons"] = dict(
            sorted((key, count) for key, count in reasons.items() if count)
        )
    required_tokens = _fixed_estimate(plan)
    if (
        plan["stats"]["actionable_input_events"]
        and not plan["events"]
        and not new_failed_checks
    ):
        raise ContextBudgetError(
            "follow-up budget cannot retain any actionable event; increase "
            "context.follow_up_max_tokens"
        )
    if (
        required_tokens > budget_tokens
        or len(_canonical_json(plan)) > MAX_SERIALIZED_CONTEXT_CHARS
    ):
        raise ContextBudgetError(
            f"mandatory follow-up facts require {required_tokens} tokens; "
            f"budget is {budget_tokens} and context-pack capacity is "
            f"{MAX_SERIALIZED_CONTEXT_CHARS} characters"
        )
    plan["actionable"] = bool(plan["events"] or new_failed_checks)
    _fixed_estimate(plan)
    return plan
