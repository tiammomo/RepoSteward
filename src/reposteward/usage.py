from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from .config import UsagePrice
from .models import AgentMetrics

USAGE_SCHEMA_VERSION = 1
METRIC_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "prompt_chars",
    "event_bytes",
    "stderr_bytes",
    "event_count",
    "tool_call_count",
    "duration_seconds",
)
GROUP_FIELDS = {
    "work-item": "work_item_id",
    "issue": "issue_number",
    "pull-request": "pull_number",
    "stage": "run_stage",
    "harness": "harness",
    "model": "model",
}


def compact_usage_metrics(metrics: AgentMetrics) -> dict[str, Any]:
    """Keep bounded counters only; paths, warnings, and prompts never enter the ledger."""
    return {name: getattr(metrics, name) for name in METRIC_FIELDS}


def compact_usage_budget(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}

    def value(name: str) -> int | None:
        candidate = raw.get(name)
        return (
            candidate
            if isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 0
            else None
        )

    trim_reasons: dict[str, int] = {}
    for key, initial_name, retained_name in (
        ("events", "initial_events", "retained_events"),
        ("diff_snippets", "initial_diff_snippets", "retained_diff_snippets"),
    ):
        initial = value(initial_name)
        retained = value(retained_name)
        if initial is not None and retained is not None and initial > retained:
            trim_reasons[key] = initial - retained
    omitted = value("issue_description_omitted_chars")
    if omitted:
        trim_reasons["issue_description_chars"] = omitted
    return {
        "budget_tokens": value("budget_tokens"),
        "estimated_tokens": value("estimated_tokens"),
        "trim_reasons": trim_reasons,
    }


def session_resume_outcome(
    requested_session_id: str,
    returned_session_id: str,
    warnings: tuple[str, ...],
) -> str:
    if not requested_session_id:
        return "not_requested"
    if any("could not resume" in value.casefold() for value in warnings):
        return "fallback_new_session"
    if returned_session_id == requested_session_id:
        return "resumed"
    if returned_session_id:
        return "fallback_new_session"
    return "unavailable"


def _select_price(
    row: dict[str, Any], prices: tuple[UsagePrice, ...]
) -> UsagePrice | None:
    harness = str(row["harness"]).casefold()
    model = str(row["model"]).casefold()
    run_date = str(row["created_at"])[:10]
    exact = [
        value
        for value in prices
        if value.harness.casefold() == harness
        and value.model.casefold() == model
        and value.effective_from <= run_date
    ]
    candidates = exact or [
        value
        for value in prices
        if value.harness.casefold() == harness
        and value.model == "*"
        and value.effective_from <= run_date
    ]
    return max(candidates, key=lambda value: value.effective_from, default=None)


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.quantize(Decimal("0.000000000001")), "f")
    return rendered.rstrip("0").rstrip(".") or "0"


def _run_cost(row: dict[str, Any], prices: tuple[UsagePrice, ...]) -> dict[str, Any]:
    price = _select_price(row, prices)
    if price is None:
        return {"status": "unknown", "reason": "price_not_configured"}
    metrics = row["metrics"]
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    missing = [name for name in required if metrics.get(name) is None]
    if (
        price.reasoning_output_per_million is not None
        and metrics.get("reasoning_output_tokens") is None
    ):
        missing.append("reasoning_output_tokens")
    if missing:
        return {
            "status": "unknown",
            "reason": "metrics_missing",
            "missing": sorted(missing),
            "currency": price.currency,
            "price_effective_from": price.effective_from,
        }
    input_tokens = int(metrics["input_tokens"])
    cached_tokens = int(metrics["cached_input_tokens"])
    output_tokens = int(metrics["output_tokens"])
    reasoning_tokens = int(metrics.get("reasoning_output_tokens") or 0)
    if cached_tokens > input_tokens or reasoning_tokens > output_tokens:
        return {
            "status": "unknown",
            "reason": "inconsistent_metrics",
            "currency": price.currency,
            "price_effective_from": price.effective_from,
        }
    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens - cached_tokens) * price.input_per_million
        + Decimal(cached_tokens) * price.cached_input_per_million
    ) / million
    if price.reasoning_output_per_million is None:
        cost += Decimal(output_tokens) * price.output_per_million / million
    else:
        cost += (
            Decimal(output_tokens - reasoning_tokens) * price.output_per_million
            + Decimal(reasoning_tokens) * price.reasoning_output_per_million
        ) / million
    return {
        "status": "known",
        "value": _decimal_text(cost),
        "currency": price.currency,
        "price_effective_from": price.effective_from,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name in METRIC_FIELDS:
        values = [row["metrics"].get(name) for row in rows]
        known = [value for value in values if value is not None]
        total: int | float | None = sum(known) if known else None
        if name == "duration_seconds" and total is not None:
            total = round(float(total), 3)
        metrics[name] = {
            "value": total,
            "known_runs": len(known),
            "unknown_runs": len(values) - len(known),
        }
    costs: dict[str, Decimal] = {}
    known_cost_runs: Counter[str] = Counter()
    unknown_costs: Counter[str] = Counter()
    for row in rows:
        cost = row["cost"]
        if cost["status"] == "known":
            currency = str(cost["currency"])
            costs[currency] = costs.get(currency, Decimal(0)) + Decimal(
                str(cost["value"])
            )
            known_cost_runs[currency] += 1
        else:
            unknown_costs[str(cost["reason"])] += 1
    trim_reasons: Counter[str] = Counter()
    for row in rows:
        trim_reasons.update(row["budget"].get("trim_reasons", {}))
    return {
        "runs": len(rows),
        "ledger_runs": sum(row["source"] == "ledger" for row in rows),
        "legacy_runs": sum(row["source"] == "legacy" for row in rows),
        "metrics": metrics,
        "session_resume": dict(
            sorted(Counter(row["session_resume"] for row in rows).items())
        ),
        "portable_context_fallback": {
            "used": sum(row["portable_context_fallback"] is True for row in rows),
            "not_used": sum(row["portable_context_fallback"] is False for row in rows),
            "unknown": sum(row["portable_context_fallback"] is None for row in rows),
        },
        "trim_reasons": dict(sorted(trim_reasons.items())),
        "costs": {
            currency: {
                "value": _decimal_text(value),
                "known_runs": known_cost_runs[currency],
            }
            for currency, value in sorted(costs.items())
        },
        "unknown_cost_runs": sum(unknown_costs.values()),
        "unknown_cost_reasons": dict(sorted(unknown_costs.items())),
    }


def build_usage_report(
    rows: list[dict[str, Any]],
    *,
    prices: tuple[UsagePrice, ...],
    filters: dict[str, Any],
    group_by: str,
    include_runs: bool,
    merge_outcomes: dict[int, str] | None = None,
) -> dict[str, Any]:
    if group_by != "none" and group_by not in GROUP_FIELDS:
        raise ValueError(f"unsupported usage group: {group_by!r}")
    enriched = [{**row, "cost": _run_cost(row, prices)} for row in rows]
    groups: list[dict[str, Any]] = []
    if group_by != "none":
        field = GROUP_FIELDS[group_by]
        values: dict[str, list[dict[str, Any]]] = {}
        for row in enriched:
            raw_key = row.get(field)
            key = str(raw_key) if raw_key not in {None, ""} else "unknown"
            values.setdefault(key, []).append(row)
        for key in sorted(values):
            group = {"key": key, "summary": _summary(values[key])}
            if group_by == "pull-request" and key.isdigit():
                group["merge_outcome"] = (merge_outcomes or {}).get(int(key), "unknown")
            groups.append(group)
    report = {
        "schema_version": USAGE_SCHEMA_VERSION,
        "filters": filters,
        "group_by": group_by,
        "summary": _summary(enriched),
        "groups": groups,
        "runs_included": include_runs,
        "raw_prompts_stored": False,
    }
    if include_runs:
        report["runs"] = enriched
    return report
