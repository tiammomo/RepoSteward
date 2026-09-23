from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import test_external_verification

from reposteward.cli import main
from reposteward.tasks.external import TaskConflict
from reposteward.telemetry.skills import SkillUsage


class SkillUsageTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run
    request = test_external_verification.ExternalVerificationTests.request

    def setUp(self):
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.usage = SkillUsage(self.config)
        self.run_id = self.task["run_id"]

    def event(self, **overrides):
        return {
            "event_id": "event-1",
            "attempt_id": "attempt-1",
            "revision": 0,
            "skill_name": "verify-change",
            "skill_digest": "a" * 64,
            "phase": "loaded",
            "client": "codex",
            "model": "test-model",
            "trigger": "change_verification",
            **overrides,
        }

    def test_stages_are_distinct_and_retries_do_not_inflate_attempts(self):
        payload = self.event()
        result = self.usage.record(self.run_id, payload)
        self.assertFalse(result["idempotent"])
        self.assertTrue(self.usage.record(self.run_id, payload)["idempotent"])
        self.assertEqual(result["claim_trust"], "agent_reported")
        self.assertEqual(result["skill_digest_trust"], "caller_reported")
        self.usage.record(self.run_id, self.event(event_id="event-2", phase="executed"))
        report = self.usage.report(self.run_id)
        self.assertEqual(report["page_counts"]["events"], 2)
        self.assertEqual(report["page_counts"]["attempts"], 1)
        self.assertEqual(report["page_counts"]["phases"]["executed"], 1)
        self.assertEqual(report["causal_effect"], "not_established")
        with self.assertRaises(TaskConflict):
            self.usage.record(self.run_id, self.event(model="different-model"))
        with self.assertRaisesRegex(TaskConflict, "phase already recorded"):
            self.usage.record(self.run_id, self.event(event_id="event-3"))

    def test_bounded_inputs_reject_content_and_stale_revision(self):
        for extra in (
            {"transcript": "private"},
            {"revision": True},
            {"revision": -1},
            {"phase": "successful"},
            {"phase": {}},
            {"trigger": []},
            {"event_id": "x" * 129},
            {"model": "a model with prose"},
            {"skill_digest": "short"},
            {"verification_id": "invalid"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.usage.record(self.run_id, self.event(**extra))
        with self.assertRaises(TaskConflict):
            self.usage.record(self.run_id, self.event(revision=1))
        self.assertEqual(self.usage.report(self.run_id)["events"], [])

    def test_scope_and_pagination_are_bound_to_task(self):
        for index in range(3):
            self.usage.record(
                self.run_id,
                self.event(event_id=f"event-{index}", attempt_id=f"attempt-{index}"),
            )
        first = self.usage.report(self.run_id, limit=2)
        second = self.usage.report(self.run_id, limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["events"]), 1)
        self.assertEqual(second["next_cursor"], "")
        self.assertNotIn(
            second["events"][0]["sequence"],
            [event["sequence"] for event in first["events"]],
        )
        other = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        self.assertEqual(self.usage.report(other["run_id"])["events"], [])
        with self.assertRaises(ValueError):
            self.usage.report(other["run_id"], cursor=first["next_cursor"])
        for cursor in ("broken!", "W10=", "x" * 1025):
            with self.assertRaises(ValueError):
                self.usage.report(self.run_id, cursor=cursor)

    def test_verification_is_a_reference_and_changes_are_disclosed(self):
        proof = self.request()
        self.usage.record(
            self.run_id,
            self.event(phase="executed", verification_id=proof["evidence_id"]),
        )
        event = self.usage.report(self.run_id)["events"][0]
        self.assertEqual(event["verification"]["outcome"], "passed")
        self.assertEqual(event["verification"]["current_applicability"], "not_checked")
        self.assertNotIn("result", event)
        other = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        with self.assertRaises(KeyError):
            self.usage.record(
                other["run_id"],
                self.event(phase="executed", verification_id=proof["evidence_id"]),
            )
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE external_verifications SET outcome='failed' WHERE run_id=?",
                (self.run_id,),
            )
        report = self.usage.report(self.run_id)
        self.assertEqual(
            report["events"][0]["verification"]["availability"],
            "changed_or_unavailable",
        )
        self.assertTrue(
            self.usage.record(
                self.run_id,
                self.event(phase="executed", verification_id=proof["evidence_id"]),
            )["idempotent"]
        )

    def test_changed_task_authority_blocks_new_events(self):
        self.usage.record(self.run_id, self.event())
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE external_task_runs SET policy_digest='changed' WHERE run_id=?",
                (self.run_id,),
            )
        with self.assertRaises(TaskConflict):
            self.usage.record(self.run_id, self.event(event_id="new", phase="executed"))
        self.assertTrue(self.usage.record(self.run_id, self.event())["idempotent"])
        self.assertEqual(len(self.usage.report(self.run_id)["events"]), 1)

    def test_modified_payload_is_not_silently_reported(self):
        self.usage.record(self.run_id, self.event())
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE skill_usage_events SET payload=replace(payload,'test-model','modified')"
            )
        with self.assertRaises(TaskConflict):
            self.usage.report(self.run_id)

    def test_cli_record_and_report_do_not_launch_pipeline(self):
        payload = self.root / "event.json"
        payload.write_text(json.dumps(self.event()))
        for args in (
            ["record", self.run_id, "--input", str(payload)],
            ["report", self.run_id, "--limit", "1"],
        ):
            output = io.StringIO()
            with (
                patch("reposteward.cli.load_config", return_value=self.config),
                patch(
                    "reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(main(["skill-usage", *args]), 0)
            self.assertFalse(json.loads(output.getvalue())["public_write"])
        payload.write_text("x" * 8193)
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["skill-usage", "record", self.run_id, "--input", str(payload)]), 2
            )


if __name__ == "__main__":
    unittest.main()
