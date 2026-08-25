from __future__ import annotations

import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from reposteward.benchmark import (
    BENCHMARK_CATEGORIES,
    benchmark_manifest,
    run_benchmark,
    validate_benchmark_report,
)
from reposteward.cli import main


class RepoStewardBenchTests(unittest.TestCase):
    def test_complete_suite_passes_hard_gates_and_all_categories(self) -> None:
        report = run_benchmark()

        validate_benchmark_report(report)
        self.assertEqual(report["summary"]["scenario_count"], 13)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertTrue(report["summary"]["hard_gate_pass"])
        self.assertTrue(report["summary"]["deterministic"])
        self.assertEqual(
            set(report["summary"]["categories"]), set(BENCHMARK_CATEGORIES)
        )
        self.assertFalse(report["harness_invoked"])
        self.assertFalse(report["network_access"])
        self.assertFalse(report["public_write"])
        self.assertTrue(report["summary"]["timing_is_informational"])

    def test_manifest_is_versioned_and_has_unique_scenarios(self) -> None:
        manifest = benchmark_manifest()
        identifiers = [value["id"] for value in manifest["scenarios"]]

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["suite_id"], "repostewardbench-v0")
        self.assertEqual(tuple(manifest["categories"]), BENCHMARK_CATEGORIES)
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_selection_and_baseline_compare_semantic_metrics_only(self) -> None:
        baseline = run_benchmark(scenario_ids=("context.utf8_estimate",))
        baseline = deepcopy(baseline)
        baseline_row = baseline["scenarios"][0]
        baseline_row["metrics"]["estimated_tokens"] -= 10
        digest_payload = {
            "metrics": baseline_row["metrics"],
            "facts": baseline_row["facts"],
        }
        baseline_row["result_digest"] = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        report = run_benchmark(
            categories=("context",),
            scenario_ids=("context.utf8_estimate",),
            baseline=baseline,
        )
        comparison = report["baseline_comparison"]

        self.assertEqual(comparison["matched_scenarios"], 1)
        self.assertEqual(
            comparison["metric_deltas"]["context.utf8_estimate"]["estimated_tokens"],
            10,
        )
        self.assertFalse(comparison["timing_compared"])

    def test_cli_writes_machine_readable_report_without_project_config(self) -> None:
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "reports" / "benchmark.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "benchmark",
                        "run",
                        "--scenario",
                        "context.utf8_estimate",
                        "--output",
                        str(output_path),
                    ]
                )
            printed = json.loads(stdout.getvalue())
            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(printed, saved)
        self.assertEqual(printed["summary"]["scenario_count"], 1)

    def test_invalid_repeat_and_empty_selection_fail_before_running(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeat"):
            run_benchmark(repeat=1)
        with self.assertRaisesRegex(ValueError, "unknown benchmark scenarios"):
            run_benchmark(scenario_ids=("missing",))

    def test_report_schema_rejects_a_claimed_public_write(self) -> None:
        report = run_benchmark(scenario_ids=("context.utf8_estimate",))
        report["public_write"] = True

        with self.assertRaisesRegex(ValueError, "public_write"):
            validate_benchmark_report(report)


if __name__ == "__main__":
    unittest.main()
