"""Fit the complete rendered prompt while retaining mandatory task requirements."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .agent import build_harness_prompt
from .context import ContextPack, _utf8_prefix
from .context_budget import ContextBudgetError, estimate_tokens


def fit_context(
    context: ContextPack, budget_tokens: int
) -> tuple[ContextPack, dict[str, Any]]:
    if not 512 <= budget_tokens <= 100_000:
        raise ValueError("context budget must be between 512 and 100000")
    original = context
    for _ in range(16):
        size = estimate_tokens(build_harness_prompt(context))
        if size <= budget_tokens:
            return context, {
                "budget_tokens": budget_tokens,
                "estimated_tokens": size,
                "coverage": list(context.coverage),
            }
        if context.task.description:
            description = _utf8_prefix(
                context.task.description,
                max(
                    0,
                    len(context.task.description.encode())
                    - (size - budget_tokens)
                    - 64,
                ),
            )
            omitted = (
                original.task.description_omitted_chars
                + len(original.task.description)
                - len(description)
            )
            coverage = tuple(
                {**value, "omitted": omitted, "reason": "complete_prompt_budget"}
                if value["field"] == "task.description"
                else value
                for value in context.coverage
            )
            context = replace(
                context,
                task=replace(
                    context.task,
                    description=description,
                    description_omitted_chars=omitted,
                ),
                coverage=coverage,
            )
            continue
        if context.handoff is not None:
            source = next(
                value
                for value in context.sources
                if value.kind == "reposteward_checkpoint"
            )
            context = replace(
                context,
                handoff=None,
                coverage=(
                    *context.coverage,
                    {
                        "field": "handoff",
                        "unit": "items",
                        "omitted": 1,
                        "reason": "complete_prompt_budget",
                        "locator": source.locator,
                        "digest": source.digest,
                    },
                ),
            )
            continue
        raise ContextBudgetError(
            "mandatory task contract and safety facts exceed the complete prompt budget; "
            "increase the budget or explicitly review a shorter source-bound contract"
        )
    raise ContextBudgetError("complete context prompt did not fit its budget")
