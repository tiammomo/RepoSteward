from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from reposteward.config import RepositoryPolicy, UsagePrice
from reposteward.models import AgentMetrics
from reposteward.pipeline import Pipeline
from reposteward.usage import (
    build_usage_report,
    compact_usage_budget,
    compact_usage_metrics,
    session_resume_outcome,
)


def _row(
    run_id: str,
    *,
    created_at: str,
    model: str = "model-a",
    metrics: dict | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "work_item_id": "work-1",
        "repository": "owner/repo",
        "issue_number": 7,
        "pull_number": 12,
        "run_stage": "repair" if run_id == "run-2" else "prepare",
        "current_stage": "pull_request",
        "status": "submitted",
        "harness": "codex-sdk",
        "model": model,
        "created_at": created_at,
        "metrics": metrics
        or {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 900_000,
            "output_tokens": 100_000,
            "reasoning_output_tokens": 20_000,
            "prompt_chars": 2_000,
            "event_bytes": 100,
            "stderr_bytes": 0,
            "event_count": 3,
            "tool_call_count": 2,
            "duration_seconds": 5.5,
        },
        "budget": {
            "budget_tokens": 24_000,
            "estimated_tokens": 20_000,
            "trim_reasons": {"events": 1} if run_id == "run-2" else {},
        },
        "session_resume": "resumed" if run_id == "run-2" else "not_requested",
        "portable_context_fallback": False,
        "source": "ledger",
    }


class UsageLedgerTests(unittest.TestCase):
    def test_compaction_never_keeps_paths_warnings_or_prompt_text(self) -> None:
        metrics = AgentMetrics(
            input_tokens=100,
            cached_input_tokens=90,
            output_tokens=10,
            log_path="/secret/path/log.jsonl",
            warnings=("contains provider detail",),
        )
        compact = compact_usage_metrics(metrics)

        self.assertNotIn("log_path", compact)
        self.assertNotIn("warnings", compact)
        self.assertNotIn("prompt", compact)

    def test_resume_and_portable_budget_outcomes_are_deterministic(self) -> None:
        self.assertEqual(session_resume_outcome("", "new", ()), "not_requested")
        self.assertEqual(session_resume_outcome("old", "old", ()), "resumed")
        self.assertEqual(
            session_resume_outcome("old", "new", ()), "fallback_new_session"
        )
        self.assertEqual(
            session_resume_outcome(
                "old", "new", ("SDK could not resume the previous thread",)
            ),
            "fallback_new_session",
        )
        self.assertEqual(session_resume_outcome("old", "", ()), "unavailable")
        self.assertEqual(
            compact_usage_budget(
                {
                    "budget_tokens": 100,
                    "estimated_tokens": 90,
                    "initial_events": 4,
                    "retained_events": 2,
                    "initial_diff_snippets": 3,
                    "retained_diff_snippets": 1,
                    "issue_description_omitted_chars": 12,
                    "prompt": "must not be retained",
                }
            ),
            {
                "budget_tokens": 100,
                "estimated_tokens": 90,
                "trim_reasons": {
                    "events": 2,
                    "diff_snippets": 2,
                    "issue_description_chars": 12,
                },
            },
        )

    def test_report_uses_dated_prices_and_preserves_unknown_metrics(self) -> None:
        prices = (
            UsagePrice(
                harness="codex-sdk",
                model="model-a",
                effective_from="2026-01-01",
                currency="USD",
                input_per_million=Decimal(1),
                cached_input_per_million=Decimal("0.1"),
                output_per_million=Decimal(2),
            ),
            UsagePrice(
                harness="codex-sdk",
                model="model-a",
                effective_from="2026-08-01",
                currency="USD",
                input_per_million=Decimal(2),
                cached_input_per_million=Decimal("0.2"),
                output_per_million=Decimal(4),
            ),
        )
        missing = dict(_row("run-3", created_at="2026-08-10T00:00:00+00:00"))
        missing["metrics"] = {**missing["metrics"], "cached_input_tokens": None}
        report = build_usage_report(
            [
                _row("run-1", created_at="2026-07-01T00:00:00+00:00"),
                _row("run-2", created_at="2026-08-10T00:00:00+00:00"),
                missing,
            ],
            prices=prices,
            filters={"repository": "owner/repo"},
            group_by="stage",
            include_runs=True,
            merge_outcomes={12: "merged"},
        )

        self.assertEqual(report["summary"]["costs"]["USD"]["value"], "1.17")
        self.assertEqual(report["summary"]["unknown_cost_runs"], 1)
        self.assertEqual(
            report["summary"]["metrics"]["cached_input_tokens"],
            {"value": 1_800_000, "known_runs": 2, "unknown_runs": 1},
        )
        self.assertEqual(report["summary"]["trim_reasons"], {"events": 1})
        self.assertEqual(
            [value["key"] for value in report["groups"]], ["prepare", "repair"]
        )
        self.assertFalse(report["raw_prompts_stored"])
        self.assertNotIn("log_path", str(report))
        self.assertNotIn("provider detail", str(report))

    def test_reasoning_price_replaces_the_reasoning_part_of_output_cost(self) -> None:
        price = UsagePrice(
            harness="codex-sdk",
            model="*",
            effective_from="2026-01-01",
            currency="USD",
            input_per_million=Decimal(0),
            cached_input_per_million=Decimal(0),
            output_per_million=Decimal(2),
            reasoning_output_per_million=Decimal(4),
        )
        report = build_usage_report(
            [_row("run-1", created_at="2026-08-01T00:00:00+00:00")],
            prices=(price,),
            filters={},
            group_by="none",
            include_runs=True,
        )

        self.assertEqual(report["runs"][0]["cost"]["value"], "0.24")

    def test_pipeline_normalizes_filters_without_calling_a_harness(self) -> None:
        class UsageStore:
            def __init__(self) -> None:
                self.filters: dict = {}

            def harness_usage_rows(self, _repository: str, **filters) -> list[dict]:
                self.filters = filters
                return []

            @staticmethod
            def latest_merge_outcomes(_repository: str) -> dict[int, str]:
                return {}

        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="owner/repo")},
            observability=SimpleNamespace(prices=()),
        )
        pipeline.store = UsageStore()

        report = pipeline.usage_report(
            "OWNER/REPO",
            issue_number=7,
            pull_number=12,
            run_stage="repair",
            harness="codex-sdk",
            model="model-a",
            since="2026-08-01",
            until="2026-08-02",
            group_by="none",
        )

        self.assertEqual(report["summary"]["runs"], 0)
        self.assertFalse(report["public_write"])
        self.assertEqual(pipeline.store.filters["since"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(pipeline.store.filters["until"], "2026-08-03T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            pipeline.usage_report("owner/repo", since="2026/08/01")


if __name__ == "__main__":
    unittest.main()
