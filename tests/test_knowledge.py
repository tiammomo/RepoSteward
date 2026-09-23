from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import test_external_verification

from reposteward.cli import main
from reposteward.core.config import load_config
from reposteward.integrations.mcp import ScopedBridge
from reposteward.projects.knowledge import ProjectKnowledge
from reposteward.tasks.external import TaskConflict
from reposteward.workflows.policy import PolicyError


class KnowledgeTests(unittest.TestCase):
    container_run = test_external_verification.ExternalVerificationTests.container_run
    request = test_external_verification.ExternalVerificationTests.request

    def setUp(self) -> None:
        test_external_verification.ExternalVerificationTests.setUp(self)
        self.knowledge = ProjectKnowledge(self.config)
        self.run_id = self.task["run_id"]

    def propose(
        self, *, statement: str = "Keep the source format stable", supersedes: str = ""
    ) -> dict:
        return self.knowledge.propose(
            self.run_id,
            {
                "statement": statement,
                "scope_paths": ["source.txt"],
                "evidence_ids": [f"checkpoint:{self.run_id}:0"],
                "supersedes": supersedes,
            },
        )

    def promote(self, identifier: str, *, proof: str = "") -> dict:
        return self.knowledge.promote(
            self.run_id,
            identifier,
            reviewed_by="owner",
            basis="verification_evidence" if proof else "human_confirmation",
            rationale="Reviewed the statement against its source and scope",
            verification_id=proof,
        )

    def dispose(self, identifier, *, action="withdraw", reason="Incorrect advice"):
        return self.knowledge.dispose(
            self.run_id, identifier, action=action, reviewed_by="owner", reason=reason
        )

    def test_withdraw_excludes_context_preserves_review_and_is_idempotent(self):
        entry = self.propose()
        reviewed = self.promote(entry["id"])
        result = self.dispose(entry["id"])
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["review"], reviewed["review"])
        self.assertEqual(result["disposition"]["reason"], "Incorrect advice")
        self.assertFalse(result["idempotent"])
        self.assertTrue(self.dispose(entry["id"])["idempotent"])
        self.assertEqual(self.knowledge.list(self.run_id)["entries"], [])
        self.assertEqual(
            self.service.context(self.run_id, scope_paths=("source.txt",))["knowledge"][
                "entries"
            ],
            [],
        )
        inactive = self.knowledge.list(self.run_id, include_inactive=True)["entries"][0]
        self.assertEqual(inactive["disposition"], result["disposition"])
        with self.assertRaises(TaskConflict):
            self.dispose(entry["id"], reason="Different decision")
        with self.assertRaisesRegex(TaskConflict, "withdrawn"):
            self.promote(entry["id"])
        replacement = self.propose(statement="Corrected advice", supersedes=entry["id"])
        self.promote(replacement["id"])
        old = self.knowledge.inspect(self.run_id, entry["id"])
        self.assertEqual(old["status"], "withdrawn")
        self.assertEqual(old["successor"], replacement["id"])
        self.assertEqual(old["disposition"], result["disposition"])

    def test_reject_is_terminal_and_requires_candidate(self):
        entry = self.propose()
        with self.assertRaises(TaskConflict):
            self.dispose(entry["id"])
        rejected = self.dispose(entry["id"], action="reject")
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(self.dispose(entry["id"], action="reject")["idempotent"])
        with self.assertRaisesRegex(TaskConflict, "rejected"):
            self.promote(entry["id"])
        self.assertEqual(self.propose()["status"], "rejected")
        reviewed = self.propose(statement="Another candidate")
        self.promote(reviewed["id"])
        with self.assertRaises(TaskConflict):
            self.dispose(reviewed["id"], action="reject")

    def test_disposition_checks_identity_scope_and_bounded_reason(self):
        entry = self.propose()
        for reason in ("", " ", None, "x" * 2001):
            with self.assertRaises(ValueError):
                self.dispose(entry["id"], action="reject", reason=reason)
        with self.assertRaises(PolicyError):
            self.knowledge.dispose(
                self.run_id,
                entry["id"],
                action="reject",
                reviewed_by="other",
                reason="No",
            )
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE project_knowledge SET project_id='other' WHERE id=?",
                (entry["id"],),
            )
        with self.assertRaises(KeyError):
            self.dispose(entry["id"], action="reject")

    def test_disposition_audit_failure_rolls_back_state(self):
        entry = self.propose()
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "CREATE TRIGGER fail_disposition BEFORE INSERT ON knowledge_dispositions BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit unavailable"):
            self.dispose(entry["id"], action="reject")
        after = self.knowledge.inspect(self.run_id, entry["id"])
        self.assertEqual(after["status"], "candidate")
        self.assertIsNone(after["disposition"])

    def test_stale_sources_can_be_withdrawn_and_cli_records_reason(self):
        entry = self.propose()
        self.promote(entry["id"])
        (self.repo / "source.txt").write_text("Changed source")
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "knowledge",
                        "withdraw",
                        self.run_id,
                        entry["id"],
                        "--reviewed-by",
                        "owner",
                        "--reason",
                        "Incorrect advice",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["status"], "withdrawn")
        current = self.knowledge.inspect(self.run_id, entry["id"], live=True)
        self.assertIn("scoped_code_changed", current["invalidations"])
        self.assertEqual(current["effective_status"], "withdrawn")

    def test_candidates_do_not_enter_context_and_human_confirmation_is_not_test_evidence(
        self,
    ) -> None:
        entry = self.propose()
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["id"], self.propose()["id"])
        self.assertEqual(self.knowledge.list(self.run_id)["entries"], [])
        before = self.service.context(self.run_id, scope_paths=("source.txt",))
        self.assertEqual(before["knowledge"]["entries"], [])
        reviewed = self.promote(entry["id"])
        self.assertEqual(reviewed["review"]["trust"], "human_attested")
        context = self.service.context(self.run_id, scope_paths=("source.txt",))
        self.assertEqual(
            context["knowledge"]["entries"][0]["basis"], "human_confirmation"
        )
        self.assertEqual(context["claim_trust"], "agent_unverified")
        self.assertEqual(
            self.service.context(self.run_id)["knowledge"]["selection"],
            "scope_not_requested",
        )
        self.assertTrue(self.promote(entry["id"])["idempotent"])

    def test_verification_promotion_requires_current_passed_evidence(self) -> None:
        entry = self.propose()
        with self.assertRaises(KeyError):
            self.promote(entry["id"], proof="verification:" + "f" * 32)
        passed = self.request()
        reviewed = self.promote(entry["id"], proof=passed["evidence_id"])
        self.assertEqual(reviewed["review"]["trust"], "reviewed_with_test_evidence")
        self.assertIn("verification_digest", reviewed["review"])
        second = self.propose(statement="Another scoped observation")
        (self.repo / "source.txt").write_text("changed after test")
        with self.assertRaises(TaskConflict):
            self.promote(second["id"], proof=passed["evidence_id"])
        report = self.knowledge.list(self.run_id, include_inactive=True)
        previous = next(item for item in report["entries"] if item["id"] == entry["id"])
        self.assertEqual(previous["effective_status"], "stale")
        self.assertIn("scoped_code_changed", previous["invalidations"])
        self.assertEqual(self.knowledge.list(self.run_id)["entries"], [])

    def test_scope_selection_and_changes_outside_dependency_paths(self) -> None:
        entry = self.propose()
        self.promote(entry["id"])
        (self.repo / "unrelated.py").write_text("x = 1\n")
        self.assertEqual(
            len(
                self.knowledge.list(self.run_id, scope_paths=("source.txt",))["entries"]
            ),
            1,
        )
        self.assertEqual(
            self.knowledge.list(self.run_id, scope_paths=("unrelated.py",))["entries"],
            [],
        )
        (self.repo / "source.txt").write_text("changed dependency")
        self.assertEqual(
            self.knowledge.inspect(self.run_id, entry["id"], live=True)[
                "effective_status"
            ],
            "stale",
        )
        self.assertEqual(
            self.knowledge.inspect(self.run_id, entry["id"])["freshness"], "not_checked"
        )

    def test_replacement_is_atomic_and_keeps_history(self) -> None:
        first = self.propose()
        self.promote(first["id"])
        replacement = self.propose(
            statement="Use the updated format rule", supersedes=first["id"]
        )
        self.assertEqual(
            self.knowledge.inspect(self.run_id, first["id"])["status"], "reviewed"
        )
        self.promote(replacement["id"])
        previous = self.knowledge.inspect(self.run_id, first["id"])
        self.assertEqual(previous["status"], "replaced")
        self.assertEqual(previous["successor"], replacement["id"])
        self.assertEqual(
            [item["id"] for item in self.knowledge.list(self.run_id)["entries"]],
            [replacement["id"]],
        )
        with self.assertRaisesRegex(TaskConflict, "replaced"):
            self.promote(first["id"])
        competing = self.propose(
            statement="Competing replacement", supersedes=first["id"]
        )
        with self.assertRaisesRegex(TaskConflict, "another replacement"):
            self.promote(competing["id"])
        self.assertEqual(
            self.knowledge.inspect(self.run_id, competing["id"])["status"], "candidate"
        )

    def test_missing_sources_and_changed_profiles_invalidate_reviewed_guidance(
        self,
    ) -> None:
        entry = self.propose()
        passed = self.request()
        self.promote(entry["id"], proof=passed["evidence_id"])
        changed = replace(self.config, verification_profiles=())
        report = ProjectKnowledge(changed).list(self.run_id, include_inactive=True)
        self.assertIn(
            "verification_profile_changed", report["entries"][0]["invalidations"]
        )
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE checkpoints SET payload=replace(payload,'investigate','changed') WHERE run_id=?",
                (self.run_id,),
            )
            db.execute(
                "UPDATE external_verifications SET outcome='unknown' WHERE run_id=?",
                (self.run_id,),
            )
        report = self.knowledge.list(self.run_id, include_inactive=True)
        self.assertIn(
            "review_evidence_changed_or_missing", report["entries"][0]["invalidations"]
        )
        self.assertEqual(self.knowledge.list(self.run_id)["entries"], [])

    def test_review_is_scoped_and_cannot_be_inferred_from_agent_claims(self) -> None:
        entry = self.propose()
        with self.assertRaises(PolicyError):
            self.knowledge.promote(
                self.run_id,
                entry["id"],
                reviewed_by="another",
                basis="human_confirmation",
                rationale="review",
            )
        with self.assertRaises(ValueError):
            self.knowledge.promote(
                self.run_id,
                entry["id"],
                reviewed_by="owner",
                basis="agent_says_done",
                rationale="review",
            )
        with self.service._store(write=True)._connection() as db:
            db.execute(
                "UPDATE project_knowledge SET project_id='another-project' WHERE id=?",
                (entry["id"],),
            )
        with self.assertRaises(KeyError):
            self.knowledge.inspect(self.run_id, entry["id"])
        self.assertEqual(self.knowledge.list(self.run_id)["entries"], [])

    def test_context_budget_omits_optional_guidance_with_retrievable_evidence(
        self,
    ) -> None:
        for index in range(5):
            entry = self.propose(statement=f"Guidance {index}: " + "x" * 1950)
            self.promote(entry["id"])
        full = self.service.context(self.run_id, scope_paths=("source.txt",))
        self.assertEqual(len(full["knowledge"]["entries"]), 5)
        budget = self.service.context(self.run_id)["estimated_tokens"] + 1500
        bounded = self.service.context(
            self.run_id, scope_paths=("source.txt",), budget=budget
        )
        self.assertEqual(bounded["knowledge"]["entries"], [])
        self.assertEqual(bounded["knowledge"]["omitted"], 5)
        self.assertLessEqual(bounded["estimated_tokens"], budget)
        self.assertEqual(bounded["open_work"], full["open_work"])
        self.assertIn(
            "knowledge.entries", [item["field"] for item in bounded["coverage"]]
        )
        evidence = self.check.evidence(
            self.run_id, full["knowledge"]["entries"][0]["evidence_id"], limit=25
        )
        self.assertEqual(evidence["returned"], 25)
        bridge = ScopedBridge(self.config, self.repo)
        self.assertEqual(
            len(
                bridge.call(
                    "context", {"run_id": self.run_id, "scope_paths": ["source.txt"]}
                )["knowledge"]["entries"]
            ),
            5,
        )

    def test_cli_does_not_launch_pipeline_and_scope_limits_are_explicit(self) -> None:
        entry = self.propose()
        self.promote(entry["id"])
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(["knowledge", "list", self.run_id, "--scope-path", "source.txt"]),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["entries"][0]["id"], entry["id"])
        with self.assertRaises(ValueError):
            self.knowledge.list(self.run_id, scope_paths=("../elsewhere",))
        with self.assertRaises(ValueError):
            self.knowledge.list(self.run_id, limit=51)

    def test_general_preferences_remain_user_owned_and_are_budgeted_separately(
        self,
    ) -> None:
        user = self.root / "user.toml"
        project = self.root / "repo.toml"
        user.write_text(
            'guidance_preferences = ["Prefer concise explanations"]\n'
            + user.read_text()
        )
        project.write_text(
            'guidance_preferences = ["Copy business secrets across projects"]\n'
            + project.read_text()
        )
        config = load_config(project, user_path=user)
        self.assertEqual(config.guidance_preferences, ("Prefer concise explanations",))
        self.service.config = replace(
            self.config, guidance_preferences=("x" * 1000,) * 10
        )
        full = self.service.context(self.run_id)
        self.assertEqual(full["preferences"]["kind"], "user_preference")
        self.service.config = self.config
        baseline = self.service.context(self.run_id)
        self.service.config = replace(
            self.config, guidance_preferences=("x" * 1000,) * 10
        )
        bounded = self.service.context(
            self.run_id, budget=baseline["estimated_tokens"] + 1000
        )
        self.assertEqual(bounded["preferences"]["omitted"], 10)
        self.assertEqual(bounded["contract"], full["contract"])

    def seed_history(
        self, entry, count, *, offset=0, status="candidate", paths=None, conditions=None
    ):
        with self.service._store(write=True)._connection() as db:
            for index in range(offset + 1, offset + count + 1):
                db.execute(
                    """INSERT INTO project_knowledge
                    SELECT ?,project_id,origin_run_id,?,statement,?,?,evidence,
                    dependencies_digest,'','',review,created_at,'2099-01-01T00:00:00+00:00'
                    FROM project_knowledge WHERE id=?""",
                    (
                        f"{index:032x}",
                        status,
                        json.dumps(paths or ["source.txt"]),
                        json.dumps(conditions or {}),
                        entry["id"],
                    ),
                )

    def test_new_candidates_and_unrelated_scopes_do_not_hide_reviewed_memory(self):
        entry = self.propose()
        self.promote(entry["id"])
        self.seed_history(entry, 201)
        self.seed_history(
            entry, 201, offset=201, status="reviewed", paths=["source.txt-other"]
        )
        report = self.knowledge.list(self.run_id, scope_paths=("source.txt",))
        self.assertEqual([item["id"] for item in report["entries"]], [entry["id"]])
        self.assertEqual(report["scanned"], 1)
        self.assertFalse(report["scan_incomplete"])
        self.assertEqual(report["next_cursor"], "")
        context = self.service.context(self.run_id, scope_paths=("source.txt",))
        self.assertEqual(
            context["knowledge"]["entries"][0]["evidence_id"],
            "knowledge:" + entry["id"],
        )

    def test_empty_stale_window_has_a_bounded_continuation(self):
        entry = self.propose()
        self.promote(entry["id"])
        self.seed_history(
            entry, 201, status="reviewed", conditions={"branch": "not-this-branch"}
        )
        with patch.object(
            self.knowledge, "_validity", wraps=self.knowledge._validity
        ) as check:
            first = self.knowledge.list(self.run_id)
        self.assertEqual(check.call_count, 200)
        self.assertEqual(first["entries"], [])
        self.assertTrue(first["scan_incomplete"])
        self.assertTrue(first["next_cursor"])
        second = self.knowledge.list(self.run_id, cursor=first["next_cursor"])
        self.assertEqual([item["id"] for item in second["entries"]], [entry["id"]])
        self.assertEqual(second["next_cursor"], "")
        context = self.service.context(self.run_id, scope_paths=("source.txt",))
        self.assertTrue(context["knowledge"]["next_cursor"])
        self.assertEqual(context["knowledge"]["entries"], [])

    def test_keyset_pages_do_not_skip_eligible_rows_with_tied_timestamps(self):
        entry = self.propose()
        self.promote(entry["id"])
        self.seed_history(entry, 6, status="reviewed")
        first = self.knowledge.list(self.run_id, limit=2)
        second = self.knowledge.list(self.run_id, limit=3, cursor=first["next_cursor"])
        third = self.knowledge.list(self.run_id, limit=3, cursor=second["next_cursor"])
        ids = [
            item["id"] for page in (first, second, third) for item in page["entries"]
        ]
        self.assertEqual(ids, [f"{i:032x}" for i in range(6, 0, -1)] + [entry["id"]])
        self.assertEqual(third["next_cursor"], "")
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "knowledge",
                        "list",
                        self.run_id,
                        "--limit",
                        "3",
                        "--cursor",
                        first["next_cursor"],
                    ]
                ),
                0,
            )
        self.assertEqual(
            [item["id"] for item in json.loads(output.getvalue())["entries"]], ids[2:5]
        )

    def test_cursor_is_bound_to_workspace_and_query_and_validates_shape(self):
        entry = self.propose()
        self.promote(entry["id"])
        self.seed_history(entry, 2, status="reviewed")
        cursor = self.knowledge.list(self.run_id, limit=1)["next_cursor"]
        for options in ({"scope_paths": ("source.txt",)}, {"include_inactive": True}):
            with self.assertRaisesRegex(ValueError, "cursor"):
                self.knowledge.list(self.run_id, cursor=cursor, **options)
        store, record = self.knowledge._task(self.run_id)
        for field in ("binding_id", "project_id"):
            with (
                patch.object(
                    self.knowledge,
                    "_task",
                    return_value=(store, {**record, field: "other"}),
                ),
                self.assertRaisesRegex(ValueError, "cursor"),
            ):
                self.knowledge.list(self.run_id, cursor=cursor)
        for invalid in ("!", "W10=", "bnVsbA==", "e30=", "x" * 2049, 42):
            with self.assertRaisesRegex(ValueError, "cursor"):
                self.knowledge.list(self.run_id, cursor=invalid)

    def test_scope_components_are_literal_and_overlap_in_both_directions(self):
        entry = self.propose()
        scopes = ["src/a_b%/child.py", "src/aXbZ/child.py", "src/a_b%-other", "."]
        for index, path in enumerate(scopes):
            self.seed_history(entry, 1, offset=index, paths=[path])
        report = self.knowledge.list(
            self.run_id, scope_paths=("src/a_b%",), include_inactive=True
        )
        self.assertEqual(
            {item["scope_paths"][0] for item in report["entries"]}, {scopes[0], "."}
        )
        report = self.knowledge.list(
            self.run_id,
            scope_paths=("src/a_b%/child.py/nested",),
            include_inactive=True,
        )
        self.assertEqual(
            {item["scope_paths"][0] for item in report["entries"]}, {scopes[0], "."}
        )
