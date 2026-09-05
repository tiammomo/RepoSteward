from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_context import _candidate
from test_projects import git, repository

from reposteward.cli import main
from reposteward.config import load_config
from reposteward.context import portable_bundle
from reposteward.context_budget import ContextBudgetError
from reposteward.external_tasks import ExternalTasks, TaskConflict
from reposteward.policy import PolicyError
from reposteward.projects import ProjectError
from reposteward.setup import add_repository, initialize_user_config
from reposteward.snapshots import workspace_snapshot
from reposteward.store import Store, StoreError


class ExternalTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = repository(
            self.root / "checkout", remote="git@github.com:owner/repo.git"
        )
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "switch", "-c", "owner/feature")
        user = self.root / "user.toml"
        policy_file = self.root / "repo.toml"
        initialize_user_config(path=user, login="owner")
        add_repository("owner/repo", path=policy_file, mode="maintainer")
        config = load_config(policy_file, user_path=user)
        policy = replace(
            config.repositories["owner/repo"],
            require_assignment_before_submit=False,
            require_no_competing_work=False,
        )
        self.config = replace(
            config, state_dir=self.root / "state", repositories={"owner/repo": policy}
        )
        self.github = Mock()
        self.github.authenticated_login.return_value = "owner"
        self.github.issue.return_value = _candidate().issue
        self.github.repository.return_value = _candidate().repository
        self.github.branch_head_sha.return_value = git(self.repo, "rev-parse", "HEAD")
        self.service = ExternalTasks(self.config, github=self.github)
        self.service.registry.link(self.repo)

    def start(self) -> dict:
        return self.service.start(self.repo, issue_number=7, reviewed_by="owner")

    def checkpoint(
        self,
        run_id: str,
        *,
        revision: int = 0,
        key: str = "one",
        payload: dict | None = None,
    ) -> dict:
        live = self.service.inspect(run_id, live=True)
        return self.service.checkpoint(
            run_id,
            expected_revision=revision,
            idempotency_key=key,
            expected_snapshot=live["current_snapshot"]["digest"],
            payload=payload
            or {
                "completed": ["Agent claims progress"],
                "remaining": ["Handle empty input"],
                "next_action": "implement boundary case",
            },
        )

    def test_start_freezes_dirty_code_without_starting_pipeline_or_modifying_workspace(
        self,
    ) -> None:
        (self.repo / "source.txt").write_text("dirty before start")
        (self.repo / "new.py").write_text("x = 1\n")
        before = workspace_snapshot(self.repo)
        with patch(
            "reposteward.cli.Pipeline",
            side_effect=AssertionError("unexpected Pipeline"),
        ):
            result = self.start()
        self.assertEqual(result["snapshot"]["digest"], before["digest"])
        self.assertTrue(result["snapshot"]["dirty"])
        self.assertEqual(workspace_snapshot(self.repo), before)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["revision"], 0)
        self.assertEqual(
            result["context_pack"]["provenance"]["harness"], "external-agent"
        )
        self.assertEqual(result["claim_trust"], "agent_unverified")

    def test_current_task_is_selected_by_workspace_binding(self) -> None:
        first = self.start()
        self.assertEqual(self.service.current(self.repo)["run_id"], first["run_id"])
        other_repo = repository(
            self.root / "other", remote="git@github.com:owner/another.git"
        )
        self.service.registry.link(other_repo)
        with self.assertRaisesRegex(KeyError, "no active external task"):
            self.service.current(other_repo)
        second = self.start()
        self.assertEqual(first["work_item_id"], second["work_item_id"])
        self.assertEqual(self.service.current(self.repo)["run_id"], second["run_id"])

    def test_checkpoint_idempotency_conflicts_and_partial_fields_preserve_open_work(
        self,
    ) -> None:
        result = self.start()
        run_id = result["run_id"]
        first = self.checkpoint(run_id)
        repeated = self.checkpoint(run_id)
        self.assertEqual(first["checkpoint_id"], repeated["checkpoint_id"])
        self.assertTrue(repeated["idempotent"])
        with self.assertRaisesRegex(TaskConflict, "idempotency key"):
            self.checkpoint(run_id, payload={"next_action": "different"})
        with self.assertRaisesRegex(TaskConflict, "revision changed"):
            self.checkpoint(run_id, key="new-key")
        self.checkpoint(
            run_id, revision=1, key="two", payload={"next_action": "verify next"}
        )
        report = self.service.inspect(run_id)
        self.assertEqual(report["checkpoint"]["remaining"], ["Handle empty input"])
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["checkpoint"]["status"], "running")
        self.assertEqual(report["revision"], 2)

    def test_concurrent_agents_cannot_overwrite_each_other(self) -> None:
        run_id = self.start()["run_id"]
        snapshot = self.service.inspect(run_id)["snapshot"]["digest"]

        def save(key):
            try:
                return self.service.checkpoint(
                    run_id,
                    expected_revision=0,
                    idempotency_key=key,
                    expected_snapshot=snapshot,
                    payload={"next_action": key},
                )
            except TaskConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("agent-a", "agent-b")))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.service.inspect(run_id)["revision"], 1)

    def test_changed_snapshot_policy_base_and_binding_are_detected(self) -> None:
        result = self.start()
        run_id = result["run_id"]
        old = result["snapshot"]["digest"]
        (self.repo / "source.txt").write_text("changed without commit")
        with self.assertRaisesRegex(TaskConflict, "expected snapshot"):
            self.service.checkpoint(
                run_id,
                expected_revision=0,
                idempotency_key="one",
                expected_snapshot=old,
                payload={"next_action": "verify"},
            )
        current = self.service.inspect(run_id, live=True)
        self.assertIn("workspace_changed_since_checkpoint", current["validity"])
        self.assertEqual(
            current["snapshot"]["head"], current["current_snapshot"]["head"]
        )
        changed = replace(
            self.config,
            repositories={
                "owner/repo": replace(
                    self.config.repositories["owner/repo"], min_stars=1
                )
            },
        )
        other = ExternalTasks(changed)
        self.assertIn("policy_changed", other.inspect(run_id, live=True)["validity"])
        git(self.repo, "add", "source.txt")
        git(self.repo, "commit", "-m", "Advance test base")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIn(
            "base_changed", self.service.inspect(run_id, live=True)["validity"]
        )
        with self.assertRaisesRegex(TaskConflict, "baseline"):
            self.checkpoint(run_id)
        self.service.registry.unlink(result["binding_id"])
        with self.assertRaises(ProjectError):
            self.service.inspect(run_id, live=True)

    def test_read_only_context_works_without_authentication_or_pipeline(self) -> None:
        result = self.start()
        self.github.reset_mock()
        reader = ExternalTasks(self.config)
        with (
            patch(
                "reposteward.external_tasks.GitHubClient",
                side_effect=AssertionError("unexpected auth"),
            ),
            patch(
                "reposteward.cli.Pipeline",
                side_effect=AssertionError("unexpected Pipeline"),
            ),
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(["task", "context", result["run_id"], "--format", "markdown"]), 0
            )
        self.assertIn("RepoSteward task handoff", output.getvalue())
        self.assertEqual(reader.context(result["run_id"])["revision"], 0)
        self.github.assert_not_called()

    def test_read_only_database_never_creates_or_migrates(self) -> None:
        missing = self.root / "missing/db.sqlite3"
        with self.assertRaises(StoreError):
            Store(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        self.start()
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("PRAGMA user_version=18")
        before = self.service.path.read_bytes()
        with self.assertRaisesRegex(StoreError, "current schema"):
            Store(self.service.path, read_only=True)
        self.assertEqual(before, self.service.path.read_bytes())

    def test_failed_registration_and_checkpoint_roll_back_all_native_records(
        self,
    ) -> None:
        with (
            patch.object(
                Store, "save_checkpoint", side_effect=RuntimeError("interrupted")
            ),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
        ):
            self.start()
        store = Store(self.service.path)
        with store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM context_packs").fetchone()[0], 0
            )
        run_id = self.start()["run_id"]
        with (
            patch.object(Store, "update_run", side_effect=RuntimeError("interrupted")),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
        ):
            self.checkpoint(run_id)
        self.assertEqual(self.service.inspect(run_id)["revision"], 0)
        self.assertEqual(self.checkpoint(run_id)["revision"], 1)

    def test_late_import_cannot_replace_current_external_open_items(self) -> None:
        run_id = self.start()["run_id"]
        store = Store(self.service.path)
        old = portable_bundle(store.context_bundle(run_id))
        self.checkpoint(run_id)
        store.import_context_bundle(old)
        self.assertEqual(
            self.service.context(run_id)["open_work"], ["Handle empty input"]
        )
        next_run = self.start()
        self.assertEqual(next_run["work_item_id"], old["work_item"]["id"])
        self.assertIn("Handle empty input", next_run["checkpoint"]["remaining"])

    def test_minimum_context_and_authority_claims_fail_explicitly(self) -> None:
        run_id = self.start()["run_id"]
        self.checkpoint(
            run_id,
            payload={
                "remaining": ["中英 task condition " * 80] * 40,
                "next_action": "continue",
            },
        )
        with self.assertRaises(ContextBudgetError):
            self.service.context(run_id, budget=5000)
        with self.assertRaisesRegex(ValueError, "authority"):
            self.checkpoint(
                run_id,
                revision=1,
                key="claim",
                payload={"next_action": "done", "verified": True},
            )
        self.assertEqual(self.service.inspect(run_id)["status"], "running")

    def test_closed_issue_and_default_branch_cannot_start_development(self) -> None:
        self.github.issue.return_value = replace(
            self.github.issue.return_value, state="closed"
        )
        with self.assertRaises(PolicyError):
            self.start()
        self.github.issue.return_value = _candidate().issue
        git(self.repo, "switch", "main")
        with self.assertRaisesRegex(PolicyError, "feature branch"):
            self.start()

    def test_frozen_issue_source_is_retained_by_gc(self) -> None:
        result = self.start()
        store = Store(self.service.path)
        digest = result["context_pack"]["task_contract"]["source_digest"]
        inventory = store.event_payload_gc_inventory(
            {"owner/repo": "2099-01-01T00:00:00Z"}
        )
        retained = next(
            value for value in inventory["retained"] if value["digest"] == digest
        )
        self.assertIn("external_task_source_reference", retained["reasons"])
        self.assertEqual(
            store.delete_event_payloads(
                (digest,), retention_cutoffs={"owner/repo": "2099-01-01T00:00:00Z"}
            )["deleted"],
            [],
        )

    def test_snapshot_excludes_sensitive_untracked_and_detects_special_files(
        self,
    ) -> None:
        before = workspace_snapshot(self.repo)
        (self.repo / ".env").write_text("TOKEN=example")
        after = workspace_snapshot(self.repo)
        self.assertEqual(before["files"], after["files"])
        self.assertEqual(after["excluded_untracked"], 1)
        (self.repo / "escape").symlink_to(self.root, target_is_directory=True)
        linked = workspace_snapshot(self.repo)
        self.assertEqual(
            next(v for v in linked["files"] if v["path"] == "escape")["kind"], "symlink"
        )


if __name__ == "__main__":
    unittest.main()
