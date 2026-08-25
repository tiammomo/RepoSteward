from __future__ import annotations

import hashlib
import importlib.resources
import json
import platform
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .context_budget import (
    ContextBudgetError,
    build_follow_up_context,
    estimate_tokens,
)
from .dependencies import (
    build_dependency_plan,
    parse_dependency_declarations,
)
from .inbox import build_maintainer_inbox, render_inbox_text
from .store import Store, StoreError

BENCHMARK_REPORT_SCHEMA_VERSION = 1
BENCHMARK_CATEGORIES = ("safety", "context", "management", "recovery", "scale")
MIN_REPEATS = 2
MAX_REPEATS = 20

ScenarioResult = dict[str, dict[str, Any]]
Scenario = Callable[[], ScenarioResult]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _resource_json(*parts: str) -> dict[str, Any]:
    resource = importlib.resources.files("reposteward").joinpath(*parts)
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"benchmark resource must be an object: {'/'.join(parts)}")
    return value


def benchmark_manifest() -> dict[str, Any]:
    manifest = _resource_json("data", "benchmark-scenarios-v1.json")
    scenarios = manifest.get("scenarios")
    categories = manifest.get("categories")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(scenarios, list)
        or not isinstance(categories, list)
    ):
        raise ValueError("benchmark manifest has an unsupported shape")
    if tuple(categories) != BENCHMARK_CATEGORIES:
        raise ValueError("benchmark manifest categories differ from the CLI contract")
    identifiers = [str(value.get("id") or "") for value in scenarios]
    if not all(identifiers) or len(identifiers) != len(set(identifiers)):
        raise ValueError("benchmark scenario identifiers must be unique and non-empty")
    if set(identifiers) != set(_SCENARIOS):
        raise ValueError("benchmark manifest and scenario implementations differ")
    if any(value.get("category") not in categories for value in scenarios):
        raise ValueError("benchmark manifest contains an unknown category")
    for scenario in scenarios:
        assertions = scenario.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError("every benchmark scenario must declare metric assertions")
        for assertion in assertions:
            if (
                not isinstance(assertion, dict)
                or not str(assertion.get("metric") or "")
                or assertion.get("operator") not in {"eq", "ge", "gt", "le", "lt"}
                or not isinstance(assertion.get("value"), (int, float))
                or isinstance(assertion.get("value"), bool)
            ):
                raise ValueError("benchmark manifest contains an invalid assertion")
    return manifest


def benchmark_report_schema() -> dict[str, Any]:
    return _resource_json("schemas", "benchmark-report-v1.schema.json")


def validate_benchmark_report(report: dict[str, Any]) -> None:
    schema = benchmark_report_schema()
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: tuple(str(value) for value in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(value) for value in first.absolute_path) or "report"
        raise ValueError(f"invalid benchmark report at {location}: {first.message}")
    for row in report["scenarios"]:
        expected = _digest({"metrics": row["metrics"], "facts": row["facts"]})
        if row["passed"] and row["result_digest"] != expected:
            raise ValueError(
                f"benchmark result digest does not match scenario {row['id']!r}"
            )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _apply_metric_assertions(metadata: dict[str, Any], result: ScenarioResult) -> None:
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError("benchmark scenario metrics must be an object")
    operators: dict[str, Callable[[int | float, int | float], bool]] = {
        "eq": lambda actual, expected: actual == expected,
        "ge": lambda actual, expected: actual >= expected,
        "gt": lambda actual, expected: actual > expected,
        "le": lambda actual, expected: actual <= expected,
        "lt": lambda actual, expected: actual < expected,
    }
    for assertion in metadata["assertions"]:
        name = str(assertion["metric"])
        actual = metrics.get(name)
        expected = assertion["value"]
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            raise TypeError(f"metric {name!r} is missing or not numeric")
        if not operators[str(assertion["operator"])](actual, expected):
            raise AssertionError(
                f"metric {name!r} is {actual}; expected "
                f"{assertion['operator']} {expected}"
            )


def _activity(*, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "pull_request": {
            "number": 12,
            "url": "https://example.test/pull/12",
            "state": "open",
            "draft": True,
            "head_sha": "a" * 40,
            "base_branch": "main",
            "base_sha": "b" * 40,
            "merged": False,
        },
        "comments": [],
        "reviews": [],
        "review_comments": [],
        "checks": checks or [],
    }


def _event(
    sequence: int,
    *,
    event_id: int | None = None,
    kind: str = "review_comment",
    body: str = "Handle this case.",
) -> dict[str, Any]:
    identifier = sequence if event_id is None else event_id
    return {
        "sequence": sequence,
        "event_type": kind,
        "external_id": str(identifier),
        "version_digest": f"{sequence:064x}",
        "payload": {
            "id": identifier,
            "author": "reviewer",
            "association": "MEMBER",
            "path": "src/example.py",
            "line": 10,
            "body": body,
        },
    }


def _scenario_context_mandatory_facts() -> ScenarioResult:
    checks = [
        {
            "id": 9,
            "name": "quality",
            "status": "completed",
            "conclusion": "failure",
        }
    ]
    events = [
        _event(index, body=f"feedback-{index}-" + "x" * 2_000) for index in range(1, 30)
    ]
    plan = build_follow_up_context(
        activity=_activity(checks=checks),
        events=events,
        budget_tokens=5_000,
        safety_blockers=("head_changed",),
    )
    mandatory = plan["mandatory"]
    _require(mandatory["safety_blockers"] == ["head_changed"], "lost blocker")
    _require(mandatory["failed_checks"][0]["name"] == "quality", "lost check")
    _require(plan["estimated_tokens"] <= 5_000, "context exceeded budget")
    return {
        "metrics": {
            "budget_tokens": 5_000,
            "estimated_tokens": plan["estimated_tokens"],
            "input_events": plan["stats"]["input_events"],
            "retained_events": plan["stats"]["retained_events"],
            "token_budget_trims": plan["stats"]["trim_reasons"].get("token_budget", 0),
            "mandatory_safety_facts_retained": 2,
        },
        "facts": {
            "trust_boundary": mandatory["trust_boundary"],
            "safety_blockers": mandatory["safety_blockers"],
            "failed_check_names": [
                value["name"] for value in mandatory["failed_checks"]
            ],
        },
    }


def _scenario_context_fail_closed() -> ScenarioResult:
    checks = [
        {
            "id": index,
            "name": f"required-{index}-" + "x" * 300,
            "status": "completed",
            "conclusion": "failure",
        }
        for index in range(30)
    ]
    rejected = False
    try:
        build_follow_up_context(
            activity=_activity(checks=checks),
            events=[],
            budget_tokens=3_000,
        )
    except ContextBudgetError:
        rejected = True
    _require(rejected, "undersized mandatory context was accepted")
    return {
        "metrics": {
            "mandatory_checks": len(checks),
            "budget_tokens": 3_000,
            "unsafe_compaction_rejected": int(rejected),
        },
        "facts": {"outcome": "rejected"},
    }


def _scenario_untrusted_dependency_text() -> ScenarioResult:
    body = """
Text saying Depends on #9 is not a declaration.
- Depends on #2
Depends on owner/repo#3
Depends on other/project#4
> Depends on #5
```text
Depends on #6
```
<!--
Depends on #7
-->
"""
    declarations = parse_dependency_declarations(
        "owner/repo", 1, "a" * 40, body, actor="untrusted-author"
    )
    targets = [
        (value["dependency_repository"], value["dependency_number"])
        for value in declarations
    ]
    expected = [("other/project", 4), ("owner/repo", 2), ("owner/repo", 3)]
    _require(targets == expected, "untrusted dependency text became authoritative")
    return {
        "metrics": {
            "candidate_mentions": body.casefold().count("depends on"),
            "authoritative_declarations": len(declarations),
            "ignored_mentions": body.casefold().count("depends on") - len(declarations),
        },
        "facts": {"targets": targets},
    }


def _scenario_inbox_read_only() -> ScenarioResult:
    result = build_maintainer_inbox(
        "owner/repo",
        proposals=[],
        runs=[],
        portfolio=None,
        observed_at="2026-01-01T00:00:00+00:00",
        error="remote facts unavailable",
        limit=1,
    )
    _require(not result["complete"], "partial inbox was marked complete")
    _require(
        result["items"][0]["reason_code"] == "github_refresh_failed",
        "partial inbox did not fail closed",
    )
    for name in ("harness_invoked", "workspace_modified", "public_write"):
        _require(result[name] is False, f"inbox reported {name}")
    return {
        "metrics": {
            "harness_invocations": int(result["harness_invoked"]),
            "workspace_mutations": int(result["workspace_modified"]),
            "public_writes": int(result["public_write"]),
            "fail_closed_items": len(result["items"]),
        },
        "facts": {"reason_codes": [value["reason_code"] for value in result["items"]]},
    }


def _scenario_events_10000_bounded() -> ScenarioResult:
    events = []
    for index in range(1, 10_001):
        if index % 10 == 0:
            kind = "check"
        elif index % 5 == 0:
            kind = "issue_comment"
        elif index % 3 == 0:
            kind = "review"
        else:
            kind = "review_comment"
        body = (
            "repeated feedback"
            if index % 7 == 0
            else f"feedback-{index}-" + "x" * 4_000
        )
        events.append(_event(index, kind=kind, body=body))
    plan = build_follow_up_context(
        activity=_activity(), events=events, budget_tokens=5_000
    )
    stats = plan["stats"]
    _require(stats["input_events"] == 10_000, "input event count changed")
    _require(plan["estimated_tokens"] <= 5_000, "context exceeded budget")
    _require(stats["retained_events"] < 10_000, "events were not bounded")
    return {
        "metrics": {
            "budget_tokens": 5_000,
            "estimated_tokens": plan["estimated_tokens"],
            "input_events": stats["input_events"],
            "retained_events": stats["retained_events"],
            "retention_basis_points": round(
                stats["retained_events"] * 10_000 / stats["input_events"]
            ),
            "trimmed_duplicate_events": stats["trim_reasons"].get(
                "duplicate_event_content", 0
            ),
        },
        "facts": {
            "actionable": plan["actionable"],
            "trim_reasons": stats["trim_reasons"],
        },
    }


def _scenario_utf8_estimate() -> ScenarioResult:
    text = "管理" * 1_000
    estimated = estimate_tokens(text)
    encoded_bytes = len(text.encode("utf-8"))
    _require(estimated == encoded_bytes, "UTF-8 estimate stopped being conservative")
    return {
        "metrics": {
            "characters": len(text),
            "utf8_bytes": encoded_bytes,
            "estimated_tokens": estimated,
        },
        "facts": {"estimate_basis": "utf8_bytes"},
    }


def _pull(
    number: int,
    *,
    checks: list[dict[str, Any]] | None = None,
    review: str = "APPROVED",
    unresolved: int = 0,
    complete: bool = True,
) -> dict[str, Any]:
    return {
        "number": number,
        "updated_at": "2026-01-01T00:00:00Z",
        "facts_complete": complete,
        "checks": checks or [],
        "review_decision": review,
        "unresolved_conversations": unresolved,
    }


def _run(
    run_id: str, status: str, *, issue: int, pull_number: int = 0
) -> dict[str, Any]:
    return {
        "id": run_id,
        "issue_number": issue,
        "status": status,
        "updated_at": "2026-01-01T00:00:00Z",
        "details": {
            "pr_url": (
                f"https://github.com/owner/repo/pull/{pull_number}"
                if pull_number
                else ""
            )
        },
        "submission_pr_url": "",
    }


def _scenario_inbox_gold_set() -> ScenarioResult:
    failed_check = [{"name": "quality", "required": True, "conclusion": "failure"}]
    portfolio = {
        "snapshot": {
            "complete": True,
            "pull_requests": [
                _pull(10, checks=failed_check),
                _pull(11, unresolved=1),
                _pull(12),
                _pull(99),
            ],
        }
    }
    result = build_maintainer_inbox(
        "owner/repo",
        proposals=[
            {
                "project_item_id": "proposal-1",
                "updated_at": "2025-12-31T00:00:00Z",
            }
        ],
        runs=[
            _run("failed", "failed", issue=1),
            _run("ready", "ready", issue=2),
            _run("ci", "submitted", issue=3, pull_number=10),
            _run("review", "submitted", issue=4, pull_number=11),
            _run("merge", "submitted", issue=5, pull_number=12),
            _run("noise", "running", issue=6),
        ],
        portfolio=portfolio,
        observed_at="2026-01-01T01:00:00+00:00",
    )
    actual = [value["reason_code"] for value in result["items"]]
    expected = [
        "required_ci_failed",
        "review_feedback_required",
        "run_failed",
        "issue_proposal_review_required",
        "local_review_required",
        "merge_check_required",
        "untracked_pull_request",
    ]
    actual_set = set(actual)
    expected_set = set(expected)
    true_positives = len(actual_set & expected_set)
    precision = true_positives / len(actual_set)
    recall = true_positives / len(expected_set)
    top_two_recall = len(set(actual[:2]) & set(expected[:2])) / 2
    _require(actual == expected, "inbox differs from the reviewed gold ordering")
    _require(
        "run:noise" not in {value["id"] for value in result["items"]}, "noise leaked"
    )
    return {
        "metrics": {
            "gold_items": len(expected),
            "selected_items": len(actual),
            "precision_basis_points": round(precision * 10_000),
            "recall_basis_points": round(recall * 10_000),
            "top_two_blocker_recall_basis_points": round(top_two_recall * 10_000),
            "irrelevant_items": len(actual_set - expected_set),
        },
        "facts": {"reason_codes": actual},
    }


def _snapshot(numbers: list[int]) -> dict[str, Any]:
    return {
        "repository": "owner/repo",
        "snapshot_digest": "f" * 64,
        "complete": True,
        "pull_requests": [
            {"number": number, "head_sha": f"{number:040x}"} for number in numbers
        ],
        "overlaps": [],
    }


def _declaration(pull_number: int, dependency_number: int) -> dict[str, Any]:
    values = parse_dependency_declarations(
        "owner/repo",
        pull_number,
        f"{pull_number:040x}",
        f"Depends on #{dependency_number}",
    )
    return values[0]


def _scenario_dependency_order() -> ScenarioResult:
    count = 100
    declarations = [_declaration(number, number - 1) for number in range(2, count + 1)]
    plan = build_dependency_plan(
        _snapshot(list(reversed(range(1, count + 1)))), declarations, [], {}
    )
    expected = list(range(1, count + 1))
    _require(plan["suggested_merge_order"] == expected, "dependency order is wrong")
    _require(plan["dependency_ready_pull_requests"] == [1], "ready set is wrong")
    return {
        "metrics": {
            "pull_requests": count,
            "dependency_edges": len(declarations),
            "ordered_pull_requests": len(plan["suggested_merge_order"]),
            "ready_pull_requests": len(plan["dependency_ready_pull_requests"]),
        },
        "facts": {
            "first": plan["suggested_merge_order"][0],
            "last": plan["suggested_merge_order"][-1],
            "plan_digest": plan["plan_digest"],
        },
    }


def _scenario_dependency_cycle() -> ScenarioResult:
    declarations = [
        _declaration(1, 2),
        _declaration(2, 1),
        _declaration(3, 2),
    ]
    plan = build_dependency_plan(_snapshot([3, 1, 2]), declarations, [], {})
    _require(plan["cycles"] == [[1, 2]], "cycle was not identified exactly")
    _require(plan["unscheduled_pull_requests"] == [1, 2, 3], "cycle leaked work")
    return {
        "metrics": {
            "cycles": len(plan["cycles"]),
            "cycle_members": len(plan["cycles"][0]),
            "unscheduled_pull_requests": len(plan["unscheduled_pull_requests"]),
        },
        "facts": {
            "cycles": plan["cycles"],
            "unscheduled": plan["unscheduled_pull_requests"],
        },
    }


def _publication_identity(run_id: str) -> dict[str, Any]:
    return {
        "attempt_id": "attempt-1",
        "step_id": "step-1",
        "run_id": run_id,
        "repository": "owner/repo",
        "issue_number": 7,
        "actor": "alice",
        "action": "push",
        "destination": "owner/repo",
        "branch": "alice/feat/example",
        "head_sha": "a" * 40,
        "base_branch": "main",
        "expected_remote_sha": "b" * 40,
        "target_pull_number": 12,
    }


def _scenario_publication_reconciliation() -> ScenarioResult:
    duplicate_rejected = False
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "state.sqlite3")
        run_id = store.start_run("owner/repo", 7, "benchmark")
        identity = _publication_identity(run_id)
        first_lease = store.acquire_run_lease("issue:owner/repo#7", owner="worker-1")
        store.append_publication_attempt(
            **identity,
            stage="applying",
            outcome="pending",
            lease=first_lease,
            payload={},
        )
        pending_before = len(store.incomplete_publication_attempts(run_id))
        store.release_run_lease(first_lease)
        second_lease = store.acquire_run_lease("issue:owner/repo#7", owner="worker-2")
        store.append_publication_attempt(
            **identity,
            stage="completed",
            outcome="reconciled",
            lease=second_lease,
            payload={
                "public_write": False,
                "remote_head_sha": "a" * 40,
                "reconciliation": "remote_branch_read",
            },
        )
        try:
            store.append_publication_attempt(
                **identity,
                stage="completed",
                outcome="succeeded",
                lease=second_lease,
                payload={"public_write": True},
            )
        except StoreError:
            duplicate_rejected = True
        pending_after = len(store.incomplete_publication_attempts(run_id))
        records = store.publication_attempts(run_id)
        completed = store.publication_action_completed(
            run_id, action="push", target_pull_number=12
        )
    _require(pending_before == 1 and pending_after == 0, "intent was not reconciled")
    _require(completed, "reconciled action was not recognized")
    _require(duplicate_rejected, "duplicate publication completion was accepted")
    return {
        "metrics": {
            "pending_before_recovery": pending_before,
            "pending_after_recovery": pending_after,
            "audit_records": len(records),
            "duplicate_completions_rejected": int(duplicate_rejected),
        },
        "facts": {"stages": [value["stage"] for value in records]},
    }


def _scenario_stale_lease_rejected() -> ScenarioResult:
    rejected = False
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "state.sqlite3")
        run_id = store.start_run("owner/repo", 7, "benchmark")
        first_lease = store.acquire_run_lease("issue:owner/repo#7", owner="worker-1")
        store.release_run_lease(first_lease)
        store.acquire_run_lease("issue:owner/repo#7", owner="worker-2")
        try:
            store.append_publication_attempt(
                **_publication_identity(run_id),
                stage="applying",
                outcome="pending",
                lease=first_lease,
                payload={},
            )
        except StoreError:
            rejected = True
        records = store.publication_attempts(run_id)
    _require(rejected, "stale lease appended a publication intent")
    _require(not records, "stale lease left an audit record")
    return {
        "metrics": {
            "stale_actions_rejected": int(rejected),
            "records_written": len(records),
        },
        "facts": {"outcome": "rejected"},
    }


def _scenario_inbox_1000_bounded() -> ScenarioResult:
    count = 1_000
    result = build_maintainer_inbox(
        "owner/repo",
        proposals=[],
        runs=[],
        portfolio={
            "snapshot": {
                "complete": True,
                "pull_requests": [_pull(number) for number in range(1, count + 1)],
            }
        },
        observed_at="2026-01-01T01:00:00+00:00",
        limit=50,
    )
    rendered_bytes = len(render_inbox_text(result).encode("utf-8"))
    _require(len(result["items"]) == 50, "inbox item limit changed")
    _require(result["omitted_count"] == 950, "inbox omission count is wrong")
    _require(rendered_bytes < 20_000, "rendered inbox is not bounded")
    return {
        "metrics": {
            "input_pull_requests": count,
            "retained_items": len(result["items"]),
            "omitted_items": result["omitted_count"],
            "rendered_bytes": rendered_bytes,
        },
        "facts": {"complete": result["complete"]},
    }


def _scenario_dependency_chain_1000() -> ScenarioResult:
    count = 1_000
    declarations = [_declaration(number, number - 1) for number in range(2, count + 1)]
    plan = build_dependency_plan(
        _snapshot(list(reversed(range(1, count + 1)))), declarations, [], {}
    )
    order = plan["suggested_merge_order"]
    _require(len(order) == count, "large dependency chain was not fully ordered")
    _require(order[0] == 1 and order[-1] == count, "large order is incorrect")
    _require(not plan["cycles"], "acyclic large chain reported a cycle")
    return {
        "metrics": {
            "pull_requests": count,
            "dependency_edges": len(declarations),
            "ordered_pull_requests": len(order),
            "cycles": len(plan["cycles"]),
        },
        "facts": {
            "first": order[0],
            "last": order[-1],
            "plan_digest": plan["plan_digest"],
        },
    }


_SCENARIOS: dict[str, Scenario] = {
    "safety.context_mandatory_facts": _scenario_context_mandatory_facts,
    "safety.context_fail_closed": _scenario_context_fail_closed,
    "safety.untrusted_dependency_text": _scenario_untrusted_dependency_text,
    "safety.inbox_read_only": _scenario_inbox_read_only,
    "context.events_10000_bounded": _scenario_events_10000_bounded,
    "context.utf8_estimate": _scenario_utf8_estimate,
    "management.inbox_gold_set": _scenario_inbox_gold_set,
    "management.dependency_order": _scenario_dependency_order,
    "management.dependency_cycle": _scenario_dependency_cycle,
    "recovery.publication_reconciliation": _scenario_publication_reconciliation,
    "recovery.stale_lease_rejected": _scenario_stale_lease_rejected,
    "scale.inbox_1000_bounded": _scenario_inbox_1000_bounded,
    "scale.dependency_chain_1000": _scenario_dependency_chain_1000,
}


def _git_sha() -> str:
    source_root = Path(__file__).resolve().parents[2]
    if not (source_root / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=source_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    value = result.stdout.strip().casefold()
    if result.returncode == 0 and len(value) == 40:
        return value
    return ""


def _run_scenario(metadata: dict[str, Any], *, repeat: int) -> dict[str, Any]:
    scenario_id = str(metadata["id"])
    implementation = _SCENARIOS[scenario_id]
    durations = []
    digests = []
    results = []
    failures = []
    for index in range(repeat):
        started = time.perf_counter_ns()
        try:
            result = implementation()
            _apply_metric_assertions(metadata, result)
            digests.append(_digest(result))
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - benchmark must report all failures
            failures.append(f"repeat {index + 1}: {type(exc).__name__}: {exc}")
        finally:
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
    deterministic = bool(digests) and len(digests) == repeat and len(set(digests)) == 1
    if not deterministic and not failures:
        failures.append("scenario results changed between repeats")
    passed = not failures and deterministic
    first = results[0] if results else {"metrics": {}, "facts": {}}
    return {
        "id": scenario_id,
        "category": str(metadata["category"]),
        "critical": bool(metadata["critical"]),
        "description": str(metadata["description"]),
        "assertions": metadata["assertions"],
        "passed": passed,
        "deterministic": deterministic,
        "repeat": repeat,
        "median_duration_ms": round(statistics.median(durations), 3),
        "result_digest": digests[0] if deterministic else "",
        "metrics": first["metrics"],
        "facts": first["facts"],
        "failures": failures,
    }


def _baseline_comparison(
    current: list[dict[str, Any]], baseline: dict[str, Any]
) -> dict[str, Any]:
    baseline_rows = {
        str(value.get("id") or ""): value
        for value in baseline.get("scenarios", [])
        if isinstance(value, dict)
    }
    metric_deltas: dict[str, dict[str, int | float]] = {}
    new_failures = []
    changed_digests = []
    missing_from_baseline = []
    for row in current:
        scenario_id = str(row["id"])
        previous = baseline_rows.get(scenario_id)
        if previous is None:
            missing_from_baseline.append(scenario_id)
            continue
        if bool(previous.get("passed")) and not row["passed"]:
            new_failures.append(scenario_id)
        previous_digest = str(previous.get("result_digest") or "")
        if previous_digest and previous_digest != row["result_digest"]:
            changed_digests.append(scenario_id)
        previous_metrics = previous.get("metrics")
        if not isinstance(previous_metrics, dict):
            continue
        deltas = {}
        for key, value in row["metrics"].items():
            old = previous_metrics.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isinstance(old, (int, float))
                and not isinstance(old, bool)
            ):
                deltas[key] = value - old
        if deltas:
            metric_deltas[scenario_id] = deltas
    return {
        "baseline_suite_id": str(baseline.get("suite_id") or ""),
        "baseline_git_sha": str(baseline.get("git_sha") or ""),
        "matched_scenarios": len(current) - len(missing_from_baseline),
        "missing_from_baseline": missing_from_baseline,
        "new_failures": new_failures,
        "changed_result_digests": changed_digests,
        "metric_deltas": metric_deltas,
        "timing_compared": False,
    }


def run_benchmark(
    *,
    categories: tuple[str, ...] = (),
    scenario_ids: tuple[str, ...] = (),
    repeat: int = 2,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not MIN_REPEATS <= repeat <= MAX_REPEATS:
        raise ValueError(
            f"benchmark repeat must be between {MIN_REPEATS} and {MAX_REPEATS}"
        )
    manifest = benchmark_manifest()
    if baseline is not None:
        validate_benchmark_report(baseline)
        if baseline.get("suite_id") != manifest["suite_id"]:
            raise ValueError("baseline benchmark report belongs to a different suite")
    known_categories = {str(value) for value in manifest["categories"]}
    unknown_categories = sorted(set(categories) - known_categories)
    if unknown_categories:
        raise ValueError(
            "unknown benchmark categories: " + ", ".join(unknown_categories)
        )
    unknown_scenarios = sorted(set(scenario_ids) - set(_SCENARIOS))
    if unknown_scenarios:
        raise ValueError("unknown benchmark scenarios: " + ", ".join(unknown_scenarios))
    selected = [
        value
        for value in manifest["scenarios"]
        if (not categories or value["category"] in categories)
        and (not scenario_ids or value["id"] in scenario_ids)
    ]
    if not selected:
        raise ValueError("benchmark selection contains no scenarios")
    rows = [_run_scenario(value, repeat=repeat) for value in selected]
    category_summary = {}
    for category in manifest["categories"]:
        values = [row for row in rows if row["category"] == category]
        if values:
            category_summary[category] = {
                "scenarios": len(values),
                "passed": sum(bool(value["passed"]) for value in values),
                "failed": sum(not value["passed"] for value in values),
            }
    critical = [row for row in rows if row["critical"]]
    failed = [row for row in rows if not row["passed"]]
    critical_failed = [row for row in critical if not row["passed"]]
    report: dict[str, Any] = {
        "schema_version": BENCHMARK_REPORT_SCHEMA_VERSION,
        "suite_id": manifest["suite_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "git_sha": _git_sha(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "selection": {
            "categories": list(categories),
            "scenario_ids": list(scenario_ids),
            "repeat": repeat,
        },
        "summary": {
            "scenario_count": len(rows),
            "passed": len(rows) - len(failed),
            "failed": len(failed),
            "critical_scenarios": len(critical),
            "critical_failed": len(critical_failed),
            "hard_gate_pass": not critical_failed,
            "all_passed": not failed,
            "deterministic": all(row["deterministic"] for row in rows),
            "categories": category_summary,
            "timing_is_informational": True,
        },
        "scenarios": rows,
        "harness_invoked": False,
        "network_access": False,
        "public_write": False,
    }
    if baseline is not None:
        report["baseline_comparison"] = _baseline_comparison(rows, baseline)
    validate_benchmark_report(report)
    return report


def load_benchmark_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("baseline benchmark report must be an object")
    validate_benchmark_report(value)
    return value


def write_benchmark_report(path: Path, report: dict[str, Any]) -> None:
    validate_benchmark_report(report)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)
