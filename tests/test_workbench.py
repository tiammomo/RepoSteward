from __future__ import annotations

import io
import json
import shutil
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import test_external_tasks
from test_projects import repository

from reposteward.cli import main
from reposteward.external_tasks import TaskConflict
from reposteward.lifecycle import build_lifecycle_trace
from reposteward.mcp_bridge import ScopedBridge
from reposteward.overview import ProjectOverview
from reposteward.projects import ProjectError
from reposteward.store import Store, StoreError
from reposteward.workbench import Workbench


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        (self.repo / "app.py").write_text(
            '"""Entry point for the workbench fixture."""\ndef main():\n    return "hello"\n'
        )
        self.task = self.service.start(self.repo, issue_number=7, reviewed_by="owner")
        test_external_tasks.ExternalTaskTests.checkpoint(self, self.task["run_id"])
        self.app = Workbench(self.config)
        self.project_id = self.task["project_id"]
        self.binding_id = self.task["binding_id"]
        self.app.understanding.scan(self.repo)

    def second(self):
        root = repository(
            self.root / "second", remote="https://github.com/owner/second"
        )
        linked = self.app.registry.link(root)
        policy = replace(self.config.repositories["owner/repo"], name="owner/second")
        self.config = replace(
            self.config,
            repositories={**self.config.repositories, "owner/second": policy},
        )
        self.app = Workbench(self.config)
        return root, linked

    def test_external_context_and_guide_match_cli_and_mcp(self):
        run_id = self.task["run_id"]
        bridge = ScopedBridge(self.config, self.repo)
        response = self.app.task(self.project_id, run_id)
        self.assertEqual(
            response["context"], bridge.call("context", {"run_id": run_id})
        )
        self.assertEqual(response["context"]["open_work"], ["Handle empty input"])
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["task", "context", run_id, "--live"]), 0)
        self.assertEqual(response["context"], json.loads(output.getvalue()))
        workspace = self.app.workspace(self.project_id, self.binding_id)
        self.assertEqual(
            workspace["guide"], bridge.call("understanding", {"action": "guide"})
        )
        source = workspace["guide"]["reading_path"][0]["source"]
        self.assertEqual(
            self.app.code(self.project_id, self.binding_id, source["evidence_id"]),
            bridge.call(
                "understanding",
                {"action": "evidence", "evidence_id": source["evidence_id"]},
            ),
        )

    def test_reads_do_not_write_authenticate_or_start_pipeline(self):
        paths = [
            self.config.state_dir / name
            for name in ("reposteward.sqlite3", "projects.sqlite3")
        ]
        before = [p.read_bytes() for p in paths]
        initialize = Store.__init__

        def guarded(instance, path, *, read_only=False):
            self.assertTrue(read_only)
            initialize(instance, path, read_only=read_only)

        with (
            patch.object(Store, "__init__", guarded),
            patch(
                "reposteward.github.resolve_token", side_effect=AssertionError("auth")
            ),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
            patch(
                "reposteward.overview.GitHubClient",
                side_effect=AssertionError("network"),
            ),
        ):
            self.app.projects()
            self.app.overview()
            self.app.workspace(self.project_id, self.binding_id)
            self.app.tasks(self.project_id)
            self.app.task(self.project_id, self.task["run_id"])
            self.app.review(self.project_id, self.task["run_id"])
            self.app.settings()
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_cross_project_run_binding_and_evidence_are_rejected(self):
        root, linked = self.second()
        pid, bid = linked["project"]["id"], linked["binding"]["id"]
        self.app.understanding.scan(root)
        evidence = self.app.workspace(self.project_id, self.binding_id)["guide"][
            "reading_path"
        ][0]["source"]["evidence_id"]
        with self.assertRaises(KeyError):
            self.app.task(pid, self.task["run_id"])
        with self.assertRaises(KeyError):
            self.app.review(pid, self.task["run_id"])
        with self.assertRaises(KeyError):
            self.app.workspace(pid, self.binding_id)
        with self.assertRaises(ProjectError):
            self.app.code(pid, bid, evidence)

    def test_unlink_and_replaced_workspace_revoke_detail_access(self):
        self.app.registry.unlink(self.binding_id)
        with self.assertRaises(KeyError):
            self.app.task(self.project_id, self.task["run_id"])
        self.app.registry.link(self.repo)
        moved = self.repo.with_name("moved")
        self.repo.rename(moved)
        repository(self.repo)
        with self.assertRaises(ProjectError):
            self.app.workspace(self.project_id, self.binding_id)

    def test_changed_source_is_stale_without_an_implicit_scan(self):
        (self.repo / "source.txt").write_text("changed\n")
        with patch.object(
            self.app.understanding, "scan", side_effect=AssertionError("scan")
        ):
            self.assertEqual(
                self.app.workspace(self.project_id, self.binding_id)["guide"]["status"],
                "stale",
            )
        context = self.app.task(self.project_id, self.task["run_id"])["context"]
        self.assertIn("workspace_changed_since_checkpoint", context["validity"])

    def test_one_unavailable_workspace_keeps_the_other_project_visible(self):
        root, linked = self.second()
        shutil.rmtree(self.repo)
        projects = self.app.projects()["projects"]
        self.assertEqual(len(projects), 2)
        self.assertEqual(sum(p["missing_workspaces"] for p in projects), 1)
        self.assertEqual(
            self.app.workspace(linked["project"]["id"], linked["binding"]["id"])[
                "guide"
            ]["status"],
            "not_scanned",
        )
        self.assertFalse(
            (self.config.state_dir / "understanding").joinpath(root.name).exists()
        )
        self.assertEqual(len(self.app.overview()["projects"]), 2)

    def test_missing_and_newer_state_are_not_initialized_or_migrated(self):
        app = Workbench(replace(self.config, state_dir=self.root / "missing"))
        self.assertEqual(app.projects()["projects"], [])
        self.assertEqual(app.overview()["projects"], [])
        self.assertEqual(app.settings()["databases"]["tasks"]["status"], "missing")
        self.assertFalse((self.root / "missing").exists())
        path = self.config.state_dir / "reposteward.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=99")
        before = path.read_bytes()
        with self.assertRaises(StoreError):
            self.app.tasks(self.project_id)
        self.assertEqual(
            self.app.settings()["databases"]["tasks"]["status"], "newer_than_supported"
        )
        self.assertEqual(before, path.read_bytes())

    def test_policy_and_configuration_changes_do_not_silently_grant_access(self):
        policy = replace(self.config.repositories["owner/repo"], enabled=False)
        app = Workbench(replace(self.config, repositories={"owner/repo": policy}))
        self.assertFalse(app.projects()["projects"][0]["policy"]["task_access"])
        with self.assertRaises(ProjectError):
            app.task(self.project_id, self.task["run_id"])
        app.config_files[0].write_text("changed configuration")
        with self.assertRaises(TaskConflict):
            app.check_configuration()

    def test_managed_context_and_review_remain_historical_evidence(self):
        store = self.service._store(write=True)
        run_id = self.task["run_id"]
        store.update_run(run_id, status="submitted", stage="pull_request", details={})
        self.assertEqual(
            self.app.task(self.project_id, run_id)["context"],
            store.context_bundle(run_id),
        )
        self.assertEqual(
            self.app.task(self.project_id, run_id)["current_applicability"],
            "not_checked",
        )
        review = self.app.review(self.project_id, run_id)
        self.assertEqual(
            review["trace"],
            build_lifecycle_trace(
                self.config.state_dir, "owner/repo", 7, event_limit=60
            ),
        )
        self.assertFalse(review["publication_eligible"])
        self.assertEqual(review["decision_freshness"], "historical_only")

    def test_overview_preserves_the_shared_fact_digest(self):
        self.assertEqual(
            self.app.overview()["overview_digest"],
            ProjectOverview(self.config).show(project_limit=20, item_limit=15)[
                "overview_digest"
            ],
        )

    def test_cli_web_uses_explicit_scope_without_pipeline(self):
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch("reposteward.cli.Pipeline", side_effect=AssertionError("Pipeline")),
            patch("reposteward.web_server.serve") as serve,
        ):
            self.assertEqual(
                main(
                    [
                        "web",
                        "--port",
                        "8765",
                        "--expect-state-dir",
                        str(self.config.state_dir),
                    ]
                ),
                0,
            )
            serve.assert_called_once_with(self.config, port=8765)

    def test_unsafe_api_origin_is_not_exposed_via_derived_paths(self):
        config = replace(
            self.config,
            github=replace(self.config.github, api_url="https://secret@api.github.com"),
        )
        with self.assertRaisesRegex(ValueError, "correct the API URL"):
            Workbench(config)
