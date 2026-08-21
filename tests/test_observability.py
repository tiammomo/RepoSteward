from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.agent import _parse_metrics
from reposteward.config import RunnerConfig, load_config
from reposteward.models import CommandResult, VerificationResult
from reposteward.pipeline import Pipeline
from reposteward.review import compact_run
from reposteward.store import Store
from reposteward.verifier import DockerVerifier

ROOT = Path(__file__).resolve().parents[1]


def _bind_test_context(store: Store, run_id: str) -> None:
    work_item = store.ensure_work_item(
        "owner/repo",
        kind="github_issue",
        external_id="7",
        title="Example issue",
    )
    sources = [
        {
            "kind": "repository_policy",
            "locator": "test-policy",
            "digest": "a" * 64,
            "trust": "operator_trusted",
            "updated_at": "",
        }
    ]
    source_digest = hashlib.sha256(
        json.dumps(
            sources,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    context = {
        "id": "pack-1",
        "schema_version": 1,
        "work_item_id": work_item["id"],
        "project": {
            "repository": "owner/repo",
            "default_branch": "main",
            "base_commit": "a" * 40,
            "policy_digest": "b" * 64,
            "verification_prefixes": [],
            "required_verification_markers": [],
            "instruction_sources": [],
        },
        "task": {
            "kind": "github_issue",
            "external_id": "7",
            "title": "Example issue",
            "description": "Example",
            "description_omitted_chars": 0,
            "url": "https://github.com/owner/repo/issues/7",
            "updated_at": "2026-08-20T00:00:00Z",
            "acceptance_criteria": [],
        },
        "constraints": [],
        "sources": sources,
        "handoff": None,
        "source_digest": source_digest,
        "provenance": {
            "run_id": run_id,
            "harness": "codex-cli",
            "model": "",
            "created_at": "2026-08-20T00:00:00Z",
            "generator": "reposteward",
        },
    }
    store.save_context_run(
        pack_id="pack-1",
        work_item_id=work_item["id"],
        run_id=run_id,
        schema_version=1,
        source_digest=source_digest,
        base_commit="a" * 40,
        payload=context,
        harness="codex-cli",
    )


class AgentMetricsTests(unittest.TestCase):
    def test_jsonl_usage_and_tool_calls_are_recorded(self) -> None:
        events = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item-1", "type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 51_000,
                            "cached_input_tokens": 49_000,
                            "output_tokens": 300,
                            "reasoning_output_tokens": 120,
                        },
                    }
                ),
            )
        )

        metrics = _parse_metrics(
            events,
            prompt_chars=1234,
            stderr_bytes=42,
            duration_seconds=3.5,
            log_path=Path("/tmp/codex.log"),
        )

        self.assertEqual(metrics.input_tokens, 51_000)
        self.assertEqual(metrics.cached_input_tokens, 49_000)
        self.assertEqual(metrics.tool_call_count, 1)
        self.assertEqual(metrics.event_count, 3)
        self.assertTrue(any("input tokens" in value for value in metrics.warnings))


class VerificationLogTests(unittest.TestCase):
    def test_full_log_is_retained_while_default_output_is_compact(self) -> None:
        runner = RunnerConfig(
            max_output_chars=12_000,
            passed_output_chars=2_000,
            max_log_chars=20_000,
        )
        verifier = DockerVerifier(SimpleNamespace(runner=runner))
        full_output = "prefix-" + "x" * 5_000
        completed = subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout=full_output, stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "run" / "verification" / "01-command.log"
            with patch("reposteward.verifier.subprocess.run", return_value=completed):
                result = verifier._run_container(
                    root, "pytest -q", network=False, log_path=log_path
                )
            retained = log_path.read_text(encoding="utf-8")

        self.assertEqual(len(result.output), 2_000)
        self.assertTrue(result.output_truncated)
        self.assertFalse(result.log_truncated)
        self.assertEqual(retained, full_output)
        self.assertEqual(result.output_chars, len(full_output))
        self.assertEqual(
            result.output_sha256,
            hashlib.sha256(full_output.encode()).hexdigest(),
        )


class ReviewPacketTests(unittest.TestCase):
    def test_review_packet_omits_verification_output(self) -> None:
        secret_output = "large-output-marker-" + "x" * 50_000
        command = CommandResult(
            command="pytest tests/test_example.py -q",
            exit_code=0,
            output=secret_output,
            duration_seconds=1.25,
            log_path="/tmp/run/verification/01-command.log",
            output_chars=len(secret_output),
            output_bytes=len(secret_output.encode()),
            output_sha256="a" * 64,
            output_truncated=True,
        )
        packet = compact_run(
            {
                "id": "run-1",
                "repository": "owner/repo",
                "issue_number": 7,
                "status": "ready",
                "stage": "review",
                "worktree": "/tmp/worktree",
                "updated_at": "2026-08-20T00:00:00Z",
                "details": {
                    "commit_sha": "b" * 40,
                    "changed_files": ["src/example.py"],
                    "agent_result": {
                        "summary": "Fix the example.",
                        "pr_title": "fix(example): handle the edge case",
                        "risks": [],
                    },
                    "verification": asdict(VerificationResult(True, (command,))),
                },
            }
        )
        encoded = json.dumps(packet, indent=2)

        self.assertNotIn("large-output-marker", encoded)
        self.assertLess(len(encoded), 12_000)
        self.assertEqual(packet["next_action"], "human_review")
        self.assertEqual(packet["verification"]["commands"][0]["output_chars"], 50_020)

    def test_pathological_review_packet_still_obeys_the_output_budget(self) -> None:
        command = {
            "command": "pytest " + "x" * 2_000,
            "exit_code": 0,
            "output": "not included",
            "duration_seconds": 1,
            "log_path": "/tmp/" + "x" * 2_000,
        }
        packet = compact_run(
            {
                "id": "run-2",
                "repository": "owner/repo",
                "issue_number": 8,
                "status": "ready",
                "stage": "review",
                "details": {
                    "worktree": "/tmp/" + "w" * 2_000,
                    "changed_files": ["f" * 2_000 for _ in range(100)],
                    "agent_result": {
                        "summary": "s" * 10_000,
                        "pr_title": "t" * 10_000,
                        "risks": ["r" * 10_000 for _ in range(100)],
                    },
                    "verification": {
                        "passed": True,
                        "reason": "",
                        "commands": [command for _ in range(100)],
                    },
                },
            }
        )

        self.assertLessEqual(
            len(json.dumps(packet, ensure_ascii=False, indent=2)), 12_000
        )
        self.assertTrue(packet["packet_truncated"])

    def test_log_command_reads_only_a_bounded_tail_from_the_run_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            config = replace(
                load_config(ROOT / "examples" / "tiammomo.toml"), state_dir=state_dir
            )
            store = Store(state_dir / "reposteward.sqlite3")
            run_id = store.start_run("owner/repo", 7, "verification")
            log_path = state_dir / "runs" / run_id / "verification" / "01.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("a" * 100 + "failure-tail", encoding="utf-8")
            command = CommandResult(
                command="pytest -q",
                exit_code=1,
                output="failure-tail",
                duration_seconds=0.5,
                log_path=str(log_path),
                output_chars=112,
            )
            store.update_run(
                run_id,
                status="failed",
                stage="failed",
                details={
                    "verification": asdict(
                        VerificationResult(False, (command,), "failed")
                    )
                },
            )
            pipeline = Pipeline.__new__(Pipeline)
            pipeline.config = config
            pipeline.store = store

            listing = pipeline.run_logs(run_id)
            tail = pipeline.run_logs(run_id, command_number=1, tail_chars=12)

        self.assertNotIn("tail", listing)
        self.assertEqual(tail["tail"], "failure-tail")
        self.assertTrue(tail["tail_truncated"])

    def test_follow_up_returns_only_activity_since_the_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            config = replace(
                load_config(ROOT / "examples" / "tiammomo.toml"), state_dir=state_dir
            )
            store = Store(state_dir / "reposteward.sqlite3")
            run_id = store.start_run("owner/repo", 7, "pull_request")
            _bind_test_context(store, run_id)
            store.update_run(
                run_id,
                status="submitted",
                stage="pull_request",
                details={
                    "commit_sha": "a" * 40,
                    "pr_url": "https://github.com/owner/repo/pull/12",
                },
            )
            activity = {
                "pull_request": {
                    "number": 12,
                    "url": "https://github.com/owner/repo/pull/12",
                    "state": "open",
                    "draft": True,
                    "updated_at": "2026-08-20T00:00:00Z",
                    "head_sha": "a" * 40,
                    "base_branch": "main",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                },
                "comments": [
                    {
                        "id": 21,
                        "author": "reviewer",
                        "association": "MEMBER",
                        "created_at": "2026-08-20T00:00:00Z",
                        "updated_at": "2026-08-20T00:00:00Z",
                        "url": "https://example.test/comment/21",
                        "body": "review " + "x" * 1_000,
                    }
                ],
                "reviews": [],
                "review_comments": [
                    {
                        "id": 31,
                        "author": "reviewer",
                        "association": "MEMBER",
                        "created_at": "2026-08-20T00:00:00Z",
                        "updated_at": "2026-08-20T00:00:00Z",
                        "url": "https://example.test/review-comment/31",
                        "path": "src/example.py",
                        "line": 42,
                        "body": "Handle this branch explicitly.",
                    }
                ],
                "checks": [
                    {
                        "id": 41,
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "success",
                        "url": "https://example.test/check/41",
                    }
                ],
            }
            pipeline = Pipeline.__new__(Pipeline)
            pipeline.config = config
            pipeline.store = store
            pipeline.github = Mock()
            pipeline.github.pull_request_activity.return_value = activity

            first = pipeline.follow_up(run_id)
            second = pipeline.follow_up(run_id)
            activity["comments"][0]["body"] = "edited review"
            activity["comments"][0]["updated_at"] = "2026-08-21T00:00:00Z"
            edited = pipeline.follow_up(run_id)
            activity["checks"][0]["conclusion"] = "failure"
            failed = pipeline.follow_up(run_id)
            activity["pull_request"]["head_sha"] = "b" * 40
            changed_head = pipeline.follow_up(run_id)
            events = store.github_pr_events("owner/repo", 12)
            watermark = store.github_pr_watermark(run_id)
            bundle = store.context_bundle(run_id)

        self.assertTrue(first["changed"])
        self.assertEqual(len(first["new_comments"]), 1)
        self.assertEqual(len(first["new_review_comments"]), 1)
        self.assertTrue(first["review_checkpoint_id"])
        self.assertIn("untrusted", first["trust_boundary"])
        self.assertLessEqual(len(first["new_comments"][0]["body"]), 640)
        self.assertTrue(first["head_matches_verified_commit"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["new_comments"], [])
        self.assertEqual(second["new_review_comments"], [])
        self.assertEqual(second["changed_checks"], [])
        self.assertEqual(len(edited["new_comments"]), 1)
        self.assertEqual(edited["new_comments"][0]["body"], "edited review")
        self.assertEqual(failed["next_action"], "diagnose_failed_checks")
        self.assertEqual(changed_head["next_action"], "reverify_changed_head")
        self.assertFalse(changed_head["head_matches_verified_commit"])
        self.assertEqual(len(events), 7)
        assert watermark is not None
        self.assertEqual(changed_head["event_watermark"], watermark["sequence"])
        assert bundle is not None
        self.assertEqual(bundle["checkpoint"]["next_action"], "reverify_changed_head")
        self.assertEqual(
            bundle["checkpoint"]["id"], changed_head["review_checkpoint_id"]
        )
        self.assertIn(
            "source=github_untrusted",
            bundle["checkpoint"]["evidence"][-1]["summary"],
        )


if __name__ == "__main__":
    unittest.main()
