from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .agent import build_harness_prompt
from .config import RepositoryPolicy
from .context import ContextPack, build_repair_context_pack
from .context_budget import ContextBudgetError, estimate_tokens
from .models import Candidate

MAX_PROMPT_FIT_PASSES = 16
EVENT_PRIORITIES = {"review_comment": 40, "review": 30, "issue_comment": 20}


def _prompt_material(
    candidate: Candidate,
    policy: RepositoryPolicy,
    *,
    work_item_id: str,
    run_id: str,
    worktree: Path,
    base_commit: str,
    harness: str,
    model: str,
    previous_checkpoint: dict[str, Any],
    pull_request_url: str,
    head_commit: str,
    event_watermark: int,
    event_batch_digest: str,
    repair_context: dict[str, Any],
    description_max_bytes: int | None,
) -> tuple[ContextPack, int]:
    """Render until the prompt estimate recorded in its own plan is stable."""
    context: ContextPack | None = None
    prompt_tokens = 0
    for _ in range(12):
        context = build_repair_context_pack(
            candidate,
            policy,
            work_item_id=work_item_id,
            run_id=run_id,
            worktree=worktree,
            base_commit=base_commit,
            harness=harness,
            model=model,
            previous_checkpoint=previous_checkpoint,
            pull_request_url=pull_request_url,
            head_commit=head_commit,
            event_watermark=event_watermark,
            event_batch_digest=event_batch_digest,
            repair_context=repair_context,
            task_description_max_bytes=description_max_bytes,
        )
        prompt_tokens = estimate_tokens(build_harness_prompt(context))
        payload_tokens = estimate_tokens(repair_context)
        transport_tokens = max(0, prompt_tokens - payload_tokens)
        if (
            repair_context.get("estimated_tokens") == prompt_tokens
            and repair_context.get("transport_overhead_tokens") == transport_tokens
        ):
            return context, prompt_tokens
        repair_context["estimated_tokens"] = prompt_tokens
        repair_context["transport_overhead_tokens"] = transport_tokens
    raise ContextBudgetError("complete repair prompt estimate did not stabilize")


def build_budgeted_repair_context_pack(
    candidate: Candidate,
    policy: RepositoryPolicy,
    *,
    work_item_id: str,
    run_id: str,
    worktree: Path,
    base_commit: str,
    harness: str,
    model: str,
    previous_checkpoint: dict[str, Any],
    pull_request_url: str,
    head_commit: str,
    event_watermark: int,
    event_batch_digest: str,
    repair_context: dict[str, Any],
    budget_tokens: int,
) -> tuple[ContextPack, dict[str, Any]]:
    """Fit the complete rendered repair prompt into one conservative budget."""
    if budget_tokens < 1:
        raise ValueError("repair prompt budget must be positive")
    plan = deepcopy(repair_context)
    events = plan.get("events")
    snippets = plan.get("diff_snippets")
    if (
        not isinstance(events, list)
        or not all(isinstance(value, dict) for value in events)
        or not isinstance(snippets, list)
        or not all(isinstance(value, dict) for value in snippets)
    ):
        raise ContextBudgetError("repair context has invalid optional records")
    mandatory = plan.get("mandatory")
    if not isinstance(mandatory, dict):
        raise ContextBudgetError("repair context has no mandatory safety facts")
    initial_events = len(events)
    initial_snippets = len(snippets)
    description_max_bytes: int | None = None
    description_was_capped = False
    trimmed_events = 0
    trimmed_snippets = 0

    def update_stats(description_omitted_chars: int = 0) -> None:
        stats = plan.get("stats")
        if not isinstance(stats, dict):
            stats = {}
            plan["stats"] = stats
        stats["retained_events"] = len(events)
        stats["retained_diff_snippets"] = len(snippets)
        stats["final_prompt_budget"] = {
            "budget_tokens": budget_tokens,
            "events_omitted": trimmed_events,
            "diff_snippets_omitted": trimmed_snippets,
            "issue_description_omitted_chars": description_omitted_chars,
        }
        reasons = stats.get("trim_reasons")
        if not isinstance(reasons, dict):
            reasons = {}
            stats["trim_reasons"] = reasons
        trimmed = trimmed_events + trimmed_snippets
        if trimmed:
            reasons["final_prompt_budget"] = trimmed
        else:
            reasons.pop("final_prompt_budget", None)

    def materialize() -> tuple[ContextPack, int]:
        return _prompt_material(
            candidate,
            policy,
            work_item_id=work_item_id,
            run_id=run_id,
            worktree=worktree,
            base_commit=base_commit,
            harness=harness,
            model=model,
            previous_checkpoint=previous_checkpoint,
            pull_request_url=pull_request_url,
            head_commit=head_commit,
            event_watermark=event_watermark,
            event_batch_digest=event_batch_digest,
            repair_context=plan,
            description_max_bytes=description_max_bytes,
        )

    update_stats()
    for _ in range(MAX_PROMPT_FIT_PASSES):
        context, prompt_tokens = materialize()
        omitted_chars = context.task.description_omitted_chars
        update_stats(omitted_chars)
        context, prompt_tokens = materialize()
        if prompt_tokens <= budget_tokens:
            return context, {
                "budget_tokens": budget_tokens,
                "estimated_tokens": prompt_tokens,
                "initial_events": initial_events,
                "retained_events": len(events),
                "initial_diff_snippets": initial_snippets,
                "retained_diff_snippets": len(snippets),
                "issue_description_omitted_chars": omitted_chars,
            }

        excess = prompt_tokens - budget_tokens
        if not description_was_capped:
            description_was_capped = True
            current_description_bytes = len(context.task.description.encode("utf-8"))
            description_max_bytes = min(current_description_bytes, budget_tokens // 4)
            if description_max_bytes < current_description_bytes:
                continue

        removed_bytes = 0
        while removed_bytes < excess and (snippets or len(events) > 1):
            event_priority = (
                EVENT_PRIORITIES.get(str(events[-1].get("kind") or ""), 10)
                if len(events) > 1 and isinstance(events[-1], dict)
                else 100
            )
            if snippets and event_priority >= 35:
                removed_bytes += estimate_tokens(snippets.pop())
                trimmed_snippets += 1
            else:
                removed_bytes += estimate_tokens(events.pop())
                trimmed_events += 1
        if removed_bytes:
            update_stats(omitted_chars)
            continue

        current_description_bytes = len(context.task.description.encode("utf-8"))
        if current_description_bytes:
            description_max_bytes = max(0, current_description_bytes - excess)
            continue
        raise ContextBudgetError(
            "complete repair prompt cannot fit mandatory facts and one actionable "
            f"feedback item within the {budget_tokens} token budget"
        )
    raise ContextBudgetError(
        "complete repair prompt did not converge within its token budget"
    )
