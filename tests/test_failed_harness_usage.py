from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_codex_sdk import FakeCodexError, FakeSdk, _context
from test_context import _candidate
from test_projects import repository

from reposteward.agents.agent import AgentError, CodexCliHarness
from reposteward.agents.codex_sdk import CodexSdkHarness
from reposteward.agents.harness import HarnessRequest
from reposteward.core.config import AgentConfig, load_config
from reposteward.core.models import AgentMetrics
from reposteward.telemetry.usage import build_usage_report
from reposteward.workflows.pipeline import Pipeline


class FailedHarnessUsageTests(unittest.TestCase):
    def test_cli_errors_retain_observed_usage_and_partial_timeout_counters(self):
        events = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "tool-1", "type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 80,
                            "output_tokens": 30,
                        },
                    }
                ),
            ]
        )
        for mode in ("nonzero", "invalid-result", "timeout"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                request = HarnessRequest(root, root / "run", _context(root))
                harness = CodexCliHarness(AgentConfig(model="fixture"))
                if mode == "timeout":
                    output = events.splitlines()[0].encode()
                    process = patch(
                        "reposteward.agents.agent.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(
                            "codex", 1, output=output
                        ),
                    )
                else:
                    process = patch(
                        "reposteward.agents.agent.subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            [], 1 if mode == "nonzero" else 0, stdout=events, stderr=""
                        ),
                    )
                with process, self.assertRaises(AgentError) as raised:
                    harness.run(request)
                error = raised.exception
                self.assertEqual(error.harness, "codex-cli")
                self.assertEqual(error.model, "fixture")
                self.assertEqual(
                    error.metrics.input_tokens, None if mode == "timeout" else 120
                )
                self.assertEqual(error.metrics.tool_call_count, 1)
                self.assertGreater(error.metrics.prompt_chars, 0)
                self.assertGreaterEqual(error.metrics.duration_seconds, 0)
                self.assertTrue((request.run_dir / "codex.log").is_file())

    def test_sdk_invalid_results_keep_usage_and_native_identity(self):
        for response in ("", "invalid JSON"):
            with (
                self.subTest(response=response),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                sdk = FakeSdk()
                sdk.response = response
                harness = CodexSdkHarness(
                    AgentConfig(harness="codex-sdk", model="fixture"), sdk_module=sdk
                )
                with self.assertRaises(AgentError) as raised:
                    harness.run(HarnessRequest(root, root / "run", _context(root)))
                error = raised.exception
                self.assertEqual(error.metrics.input_tokens, 120)
                self.assertEqual(error.metrics.cached_input_tokens, 80)
                self.assertEqual(error.native_session_id, "thread-new")

    def test_prepare_failures_are_durable_idempotent_and_prompt_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(
                '[github]\nlogin="owner"\n[repositories."owner/repo"]\nmode="maintainer"\n'
            )
            config = replace(
                load_config(config_path, include_user=False),
                state_dir=root / "state",
                workspace_dir=root / "workspaces",
            )
            error = AgentError(
                "private provider diagnostic",
                metrics=AgentMetrics(
                    input_tokens=120, cached_input_tokens=80, output_tokens=30
                ),
            )
            harness = Mock(name="harness")
            harness.name = "fixture"
            harness.run.side_effect = error
            pipeline = Pipeline(config, harness=harness)
            pipeline.ensure_candidate = Mock(return_value=_candidate())
            pipeline._validate_contribution_contract = Mock()
            pipeline._ensure_no_competing_work = Mock()
            pipeline.workspaces.clone = Mock(return_value=repository(root / "repo"))
            for _ in range(2):
                with self.assertRaises(AgentError):
                    pipeline.prepare("owner/repo", 7)
            rows = pipeline.store.harness_usage_rows("owner/repo")
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(row["metrics"]["input_tokens"] for row in rows), 240)
            self.assertTrue(
                all(
                    row["source"] == "ledger" and row["status"] == "failed"
                    for row in rows
                )
            )
            request = harness.run.call_args.args[0]
            saved = pipeline._record_harness_usage(
                run_id=request.context.provenance.run_id,
                run_stage="prepare",
                requested_session_id="",
                context=request.context,
                execution=error,
            )
            self.assertEqual(len(pipeline.store.harness_usage_rows("owner/repo")), 2)
            self.assertTrue(saved)
            self.assertNotIn("private provider diagnostic", json.dumps(rows))
            self.assertNotIn("Reproduce the bug", json.dumps(rows))
            harness.run.side_effect = RuntimeError("No provider metrics were returned")
            with self.assertRaisesRegex(RuntimeError, "No provider metrics"):
                pipeline.prepare("owner/repo", 7)
            rows = pipeline.store.harness_usage_rows("owner/repo")
            unknown = rows[-1]["metrics"]
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "event_count",
                "tool_call_count",
            ):
                self.assertIsNone(unknown[field])
            self.assertGreater(unknown["prompt_chars"], 0)
            self.assertGreaterEqual(unknown["duration_seconds"], 0)
            report = build_usage_report(
                rows, prices=(), filters={}, group_by="none", include_runs=True
            )
            self.assertEqual(len(report["runs"]), 3)
            self.assertTrue(all(row["status"] == "failed" for row in report["runs"]))
            self.assertEqual(
                report["summary"]["metrics"]["input_tokens"],
                {
                    "value": 240,
                    "known_runs": 2,
                    "unknown_runs": 1,
                },
            )

    def test_sdk_provider_failure_keeps_unobserved_counters_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness = CodexSdkHarness(
                AgentConfig(harness="codex-sdk"), sdk_module=FakeSdk()
            )
            with (
                patch(
                    "test_codex_sdk.FakeTurn.run",
                    side_effect=FakeCodexError("provider failed"),
                ),
                self.assertRaises(AgentError) as raised,
            ):
                harness.run(HarnessRequest(root, root / "run", _context(root)))
            error = raised.exception
            self.assertIsNone(error.metrics.input_tokens)
            self.assertIsNone(error.metrics.tool_call_count)
            self.assertIsNone(error.metrics.event_count)
            self.assertGreater(error.metrics.prompt_chars, 0)
            self.assertEqual(error.native_session_id, "thread-new")


if __name__ == "__main__":
    unittest.main()
