from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_external_tasks
from test_projects import git, repository

from reposteward.github.client import ConditionalRead, GitHubReadError
from reposteward.projects.clone import directory_identity, publish_directory
from reposteward.projects.imports import source_input
from reposteward.projects.registry import ProjectError
from reposteward.storage.snapshots import workspace_snapshot
from reposteward.storage.state_upgrade import (
    StateUpgradeError,
    inspect_backup,
    upgrade_plan,
    upgrade_state,
)
from reposteward.storage.store import SCHEMA_VERSION
from reposteward.tasks.local_operations import LocalOperations
from reposteward.web.workbench import Workbench


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.remote_id, self.name = 101, "owner/repo"
        self.client = Mock(config=self.config.github)
        self.client.conditional_get.side_effect = self.read
        self.operations = LocalOperations(
            Workbench(self.config), client_factory=lambda config: self.client
        )
        self.imports = self.operations.imports

    def read(self, path, **kwargs):
        return ConditionalRead(
            {"login": "owner"}
            if path == "/user"
            else {
                "id": self.remote_id,
                "full_name": self.name,
                "fork": False,
                "private": False,
                "default_branch": "main",
            },
            "",
            False,
        )

    def inspect(self, source=None, key="inspect"):
        op = self.imports.inspect(
            "local_path" if source is None else "github_url",
            str(self.repo) if source is None else source,
            key,
        )
        self.assertTrue(self.operations.process_once())
        self.assertEqual(self.operations.operation(op["id"])["state"], "completed")
        return self.imports.show(op["import_id"])

    def plan(self, current, method="link", target="", key="plan"):
        return self.imports.plan(
            current["id"], current["inspection_id"], method, "maintain", target, key
        )

    def apply(self, current, preview, key="apply"):
        op = self.imports.apply(current["id"], preview["id"], preview["digest"], key)
        self.assertTrue(self.operations.process_once())
        return self.operations.operation(op["id"])

    def test_read_does_not_initialize_and_credential_input_is_never_saved(self):
        with self.assertRaises((KeyError, ValueError)):
            self.imports.show("bad")
        for url in [
            "https://token@github.com/owner/repo",
            "https://git:secret@github.com/owner/repo",
            "git@other.test:owner/repo",
            "https://github.com/owner/repo?token=bad",
        ]:
            with self.assertRaises(ProjectError):
                self.imports.inspect("github_url", url, "one")
        self.assertFalse(self.operations.path.exists())
        self.assertFalse(self.client.conditional_get.called)

    def test_url_variants_normalize_and_identify_before_any_clone(self):
        expected = {"kind": "github_url", "value": "https://github.com/owner/repo"}
        for value in [
            "https://github.com/Owner/Repo.git",
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
        ]:
            self.assertEqual(source_input("github_url", value, "github.com"), expected)
        with patch(
            "reposteward.projects.imports.clone_ssh",
            side_effect=AssertionError("no clone"),
        ):
            self.inspect("https://github.com/owner/repo")

    def test_dirty_existing_project_reuses_ids_without_workspace_mutation(self):
        (self.repo / "source.txt").write_text("keep dirty contents")
        before = workspace_snapshot(self.repo)
        linked = self.service.registry.inspect(self.repo)
        current = self.inspect()
        result = self.apply(current, self.plan(current))
        self.assertEqual(result["state"], "completed")
        after = self.service.registry.inspect(self.repo)
        self.assertEqual(linked["project"]["id"], after["project"]["id"])
        self.assertEqual(linked["binding"], after["binding"])
        self.assertEqual(before, workspace_snapshot(self.repo))
        self.assertEqual(after["project"]["purpose"], "maintain")
        self.assertEqual(len(self.service.registry.list()["projects"]), 1)

    def test_watch_import_has_no_workspace_and_no_execution_policy(self):
        self.remote_id, self.name = 202, "owner/new"
        current = self.inspect("https://github.com/owner/new")
        result = self.apply(current, self.plan(current, "watch"))
        self.assertEqual(result["state"], "completed")
        project = next(
            p
            for p in self.operations.workbench.projects()["projects"]
            if p["repository"] == self.name
        )
        self.assertEqual(project["workspace_count"], 0)
        self.assertFalse(project["policy"]["enabled"])
        self.assertTrue(self.operations.github(project["id"])["never_synced"])

    def test_plan_and_apply_keys_are_durable_and_completed_apply_reuses_operation(self):
        current = self.inspect()
        preview = self.plan(current)
        self.assertEqual(preview, self.plan(current))
        with self.assertRaisesRegex(ProjectError, "idempotency_conflict"):
            self.plan(current, "watch")
        result = self.apply(current, preview)
        repeated = self.imports.apply(
            current["id"], preview["id"], preview["digest"], "different-key"
        )
        self.assertEqual(result["id"], repeated["id"])
        self.assertFalse(self.operations.process_once())

    def test_registry_change_or_dirty_content_edit_invalidates_reviewed_plan(self):
        (self.repo / "source.txt").write_text("first dirty")
        current = self.inspect()
        preview = self.plan(current)
        (self.repo / "source.txt").write_text("second dirty")
        self.assertEqual(self.apply(current, preview)["state"], "failed")
        self.assertFalse(self.service.registry.inspect(self.repo)["project"]["remote"])

    def test_rename_preserves_ids_aliases_and_cli_link_inspect(self):
        current = self.inspect()
        self.apply(current, self.plan(current))
        original = self.service.registry.inspect(self.repo)
        self.name = "owner/renamed"
        current = self.inspect("https://github.com/owner/renamed", "rename")
        self.assertEqual(
            self.apply(
                current, self.plan(current, "watch", key="rename-plan"), "rename-apply"
            )["state"],
            "completed",
        )
        after = self.service.registry.link(self.repo)
        self.assertEqual(after["project"]["id"], original["project"]["id"])
        self.assertEqual(after["binding"]["id"], original["binding"]["id"])
        self.assertEqual(after["project"]["repository"], self.name)
        self.assertIn("github.com/owner/repo", after["project"]["aliases"])

    def test_new_remote_reusing_a_name_is_rejected(self):
        current = self.inspect()
        self.apply(current, self.plan(current))
        self.remote_id = 999
        current = self.inspect(key="replaced")
        result = self.apply(
            current,
            self.plan(current, "watch", key="replacement-plan"),
            "replacement-apply",
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            self.service.registry.inspect(self.repo)["project"]["remote"]["remote_id"],
            "101",
        )

    def test_binding_failure_keeps_registration_and_retry_reconciles(self):
        current = self.inspect()
        preview = self.plan(current)
        original = self.imports.registry.link
        with patch.object(
            self.imports.registry, "link", side_effect=ProjectError("injected failure")
        ):
            result = self.apply(current, preview)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            self.service.registry.inspect(self.repo)["project"]["remote"]["remote_id"],
            "101",
        )
        with patch.object(self.imports.registry, "link", wraps=original):
            self.operations.control(result["id"], "retry", result["revision"], "retry")
            self.assertTrue(self.operations.process_once())
        self.assertEqual(self.operations.operation(result["id"])["state"], "completed")
        self.assertEqual(len(self.service.registry.list()["projects"]), 1)

    def fake_clone(self, host, name, destination, guard):
        guard()
        repository(destination, remote=f"git@{host}:{name}.git")

    def test_clone_new_directory_and_reconcile_lost_publish_ack(self):
        current = self.inspect("https://github.com/owner/repo")
        target = self.root / "new-checkout"
        preview = self.plan(current, "clone", str(target))
        publish = publish_directory

        def lost_ack(*args):
            publish(*args)
            raise OSError("lost acknowledgment")

        with (
            patch(
                "reposteward.projects.imports.clone_ssh", side_effect=self.fake_clone
            ) as clone,
            patch(
                "reposteward.projects.imports.publish_directory", side_effect=lost_ack
            ),
        ):
            result = self.apply(current, preview)
        self.assertEqual(result["state"], "failed")
        self.assertTrue((target / ".git").is_dir())
        self.operations.control(result["id"], "retry", result["revision"], "retry")
        with patch(
            "reposteward.projects.imports.clone_ssh",
            side_effect=AssertionError("do not reclone"),
        ):
            self.operations.process_once()
        self.assertEqual(self.operations.operation(result["id"])["state"], "completed")
        self.assertEqual(clone.call_count, 1)
        self.assertEqual(
            self.service.registry.inspect(target)["project"]["repository"], "owner/repo"
        )

    def test_publish_never_overwrites_even_empty_directory(self):
        source, target = self.root / "source-dir", self.root / "target-dir"
        source.mkdir()
        target.mkdir()
        (source / "keep").write_text("safe")
        before = directory_identity(target)
        with self.assertRaises(ProjectError):
            publish_directory(source, target, directory_identity(self.root))
        self.assertEqual(directory_identity(target), before)
        self.assertTrue((source / "keep").exists())

    def test_account_mismatch_stops_inspection_before_repository_read(self):
        self.client.conditional_get.return_value = ConditionalRead(
            {"login": "someone-else"}, "", False
        )
        self.client.conditional_get.side_effect = None
        op = self.imports.inspect("local_path", str(self.repo), "account")
        self.operations.process_once()
        self.assertEqual(
            self.operations.operation(op["id"])["last_error_code"], "account_mismatch"
        )
        self.assertEqual(self.client.conditional_get.call_count, 1)

    def test_failed_ssh_retry_uses_new_owned_attempt_without_deleting_partial_files(
        self,
    ):
        current = self.inspect("https://github.com/owner/repo")
        preview = self.plan(current, "clone", str(self.root / "target"))
        leftovers = []

        def fail(host, name, destination, guard):
            destination.mkdir()
            (destination / "keep").write_text("partial")
            leftovers.append(destination)
            raise GitHubReadError("network_unavailable")

        with patch("reposteward.projects.imports.clone_ssh", side_effect=fail):
            result = self.apply(current, preview)
        self.assertEqual(result["state"], "failed")
        self.operations.control(result["id"], "retry", result["revision"], "retry")
        with patch(
            "reposteward.projects.imports.clone_ssh", side_effect=self.fake_clone
        ):
            self.operations.process_once()
        self.assertEqual(self.operations.operation(result["id"])["state"], "completed")
        self.assertEqual((leftovers[0] / "keep").read_text(), "partial")

    def test_explicit_pair_upgrade_backups_preserve_ids_and_no_get_migration(self):
        self.operations.store(write=True)
        original = self.service.registry.inspect(self.repo)
        with closing(sqlite3.connect(self.operations.path)) as db:
            db.execute("PRAGMA user_version=23")
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            for table in (
                "project_aliases",
                "project_remotes",
                "project_profiles",
                "project_import_receipts",
            ):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=1")
        with self.assertRaises(ProjectError):
            self.service.registry.list()
        plan = upgrade_plan(self.config)
        self.assertTrue(plan["eligible"])
        result = upgrade_state(
            self.config,
            expected_state_dir=self.config.state_dir,
            plan_digest=plan["plan_digest"],
        )
        backup = Path(result["backup"]["backup_directory"])
        self.assertEqual(len(inspect_backup(backup)["databases"]), 2)
        self.assertEqual(result["schema"], SCHEMA_VERSION)
        after = self.service.registry.inspect(self.repo)
        self.assertEqual(original["binding"], after["binding"])
        self.assertEqual(original["project"]["id"], after["project"]["id"])
        with closing(sqlite3.connect(backup / "projects.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertFalse(upgrade_plan(self.config)["eligible"])

    def test_changed_registry_plan_rejected_before_backup(self):
        self.operations.store(write=True)
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            db.execute("PRAGMA user_version=1")
        plan = upgrade_plan(self.config)
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            db.execute("UPDATE projects SET name='changed'")
            db.commit()
        with self.assertRaises(StateUpgradeError):
            upgrade_state(
                self.config,
                expected_state_dir=self.config.state_dir,
                plan_digest=plan["plan_digest"],
            )
        self.assertFalse((self.config.state_dir / "backups").exists())

    def test_pair_migration_failure_rolls_back_both_before_commit(self):
        self.operations.store(write=True)
        with closing(sqlite3.connect(self.operations.path)) as db:
            db.execute("PRAGMA user_version=23")
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            db.execute("PRAGMA user_version=1")
        plan = upgrade_plan(self.config)
        with (
            patch(
                "reposteward.storage.state_upgrade_pair.migrate_registry",
                side_effect=sqlite3.OperationalError("injected"),
            ),
            self.assertRaisesRegex(StateUpgradeError, "did not commit"),
        ):
            upgrade_state(
                self.config,
                expected_state_dir=self.config.state_dir,
                plan_digest=plan["plan_digest"],
            )
        for path, version in (
            (self.operations.path, 23),
            (self.service.registry.path, 1),
        ):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone()[0], version
                )
        self.assertEqual(
            inspect_backup(next((self.config.state_dir / "backups").iterdir()))[
                "phase"
            ],
            "rolled_back",
        )

    def test_pair_partial_commit_can_be_replanned_without_losing_old_backup(self):
        self.operations.store(write=True)
        with closing(sqlite3.connect(self.operations.path)) as db:
            db.execute("PRAGMA user_version=23")
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            db.execute("PRAGMA user_version=1")

        class BeforeCommitFailure(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected registry failure")

        connect = sqlite3.connect

        def intercepted(database, **kwargs):
            if str(database).endswith("projects.sqlite3?mode=rw"):
                kwargs["factory"] = BeforeCommitFailure
            return connect(database, **kwargs)

        plan = upgrade_plan(self.config)
        with (
            patch(
                "reposteward.storage.state_upgrade_pair.sqlite3.connect",
                side_effect=intercepted,
            ),
            self.assertRaisesRegex(StateUpgradeError, "partially committed"),
        ):
            upgrade_state(
                self.config,
                expected_state_dir=self.config.state_dir,
                plan_digest=plan["plan_digest"],
            )
        backup = next((self.config.state_dir / "backups").iterdir())
        self.assertEqual(inspect_backup(backup)["phase"], "partially_upgraded")
        fresh = upgrade_plan(self.config)
        self.assertEqual(fresh["migrations"], [])
        self.assertTrue(fresh["eligible"])
        upgrade_state(
            self.config,
            expected_state_dir=self.config.state_dir,
            plan_digest=fresh["plan_digest"],
        )
        self.assertEqual(inspect_backup(backup)["phase"], "partially_upgraded")
        self.assertFalse(upgrade_plan(self.config)["eligible"])

    def test_legacy_registry_blocks_import_before_initializing_main_ledger(self):
        with closing(sqlite3.connect(self.service.registry.path)) as db:
            db.execute("PRAGMA user_version=1")
        with self.assertRaises(ProjectError):
            self.imports.inspect("local_path", str(self.repo), "old-registry")
        self.assertFalse(self.operations.path.exists())

    def test_fork_keeps_upstream_and_account_isolation(self):
        self.client.conditional_get.side_effect = lambda path, **kwargs: (
            ConditionalRead(
                {"login": "owner"}
                if path == "/user"
                else {
                    "id": 303,
                    "full_name": "owner/fork",
                    "fork": True,
                    "parent": {"id": 101, "full_name": "upstream/original"},
                },
                "",
                False,
            )
        )
        current = self.inspect("https://github.com/owner/fork")
        self.assertEqual(
            current["inspection"]["remote"]["upstream"]["repository"],
            "upstream/original",
        )
        other = LocalOperations(
            Workbench(
                replace(self.config, github=replace(self.config.github, login="other"))
            )
        )
        with self.assertRaises(KeyError):
            other.imports.show(current["id"])

    def test_concurrent_tabs_share_inspection_and_apply(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            inspected = list(
                executor.map(
                    lambda _: self.imports.inspect(
                        "local_path", str(self.repo), "same-inspect"
                    ),
                    range(2),
                )
            )
        self.assertEqual(inspected[0]["id"], inspected[1]["id"])
        self.operations.process_once()
        current = self.imports.show(inspected[0]["import_id"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            plans = list(executor.map(lambda _: self.plan(current), range(2)))
        self.assertEqual(plans[0], plans[1])
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(
                executor.map(
                    lambda index: self.imports.apply(
                        current["id"],
                        plans[0]["id"],
                        plans[0]["digest"],
                        "apply-" + str(index),
                    ),
                    range(2),
                )
            )
        self.assertEqual(tasks[0]["id"], tasks[1]["id"])

    def test_unknown_import_does_not_initialize_main_state(self):
        with self.assertRaises(KeyError):
            self.imports.show("a" * 32)
        self.assertFalse(self.operations.path.exists())

    def test_moved_repository_resolves_only_bounded_validated_hints(self):
        original = self.read

        def moved(path, **kwargs):
            if path == "/repos/owner/repo":
                raise GitHubReadError(
                    "repository_moved", redirect_path="/repositories/101"
                )
            return original(path, **kwargs)

        self.name = "owner/renamed"
        self.client.conditional_get.side_effect = moved
        result = self.inspect("https://github.com/owner/repo")
        self.assertEqual(
            result["inspection"]["remote"]["aliases"],
            ["github.com/owner/renamed", "github.com/owner/repo"],
        )
        self.assertEqual(self.client.conditional_get.call_count, 3)

    def test_clone_process_has_only_ssh_auth_and_no_git_configuration_injection(self):
        import os
        from unittest.mock import MagicMock

        from reposteward.projects.clone import clone_ssh

        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = 0
        process.returncode = 0
        with (
            patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "fixture-credential",
                    "GITHUB_TOKEN": "fixture-credential",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.sshCommand",
                    "GIT_CONFIG_VALUE_0": "unwanted-helper",
                },
            ),
            patch(
                "reposteward.projects.clone.subprocess.Popen", return_value=process
            ) as launch,
        ):
            clone_ssh("github.com", "owner/repo", self.root / "target", lambda: None)
        command, environment = launch.call_args.args[0], launch.call_args.kwargs["env"]
        self.assertIn("git@github.com:owner/repo.git", command)
        self.assertIn("--template=", command)
        self.assertNotIn("fixture-credential", str(environment))
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertIn("BatchMode=yes", environment["GIT_SSH_COMMAND"])

    def test_large_remote_id_is_exact_in_http_facts_and_registry(self):
        self.remote_id = 9007199254740993
        current = self.inspect()
        self.assertEqual(
            current["inspection"]["remote"]["remote_id"], "9007199254740993"
        )
        self.assertEqual(self.apply(current, self.plan(current))["state"], "completed")
        self.assertEqual(
            self.service.registry.inspect(self.repo)["project"]["remote"]["remote_id"],
            "9007199254740993",
        )

    def test_origin_change_during_apply_never_registers_an_unreviewed_project(self):
        current = self.inspect()
        preview = self.plan(current)
        original = self.imports.registry.link

        def changed(path, **kwargs):
            git(
                self.repo,
                "remote",
                "set-url",
                "origin",
                "git@github.com:other/unreviewed.git",
            )
            return original(path, **kwargs)

        with patch.object(self.imports.registry, "link", side_effect=changed):
            result = self.apply(current, preview)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(
            [p["repository"] for p in self.service.registry.list()["projects"]],
            ["owner/repo"],
        )

    def test_failed_publication_parent_open_closes_already_open_directory(self):
        import os

        source = self.root / "source-dir"
        source.mkdir()
        target = self.root / "new-target"
        original = os.open
        descriptors = []

        def fail_second(*args, **kwargs):
            if descriptors:
                raise OSError("injected second directory failure")
            descriptor = original(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        with (
            patch("reposteward.projects.clone.os.open", side_effect=fail_second),
            self.assertRaises(OSError),
        ):
            publish_directory(source, target, directory_identity(self.root))
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_retargeted_source_symlink_invalidates_remote_only_apply(self):
        source = self.root / "selected-source"
        source.symlink_to(self.repo, target_is_directory=True)
        operation = self.imports.inspect("local_path", str(source), "symlink")
        self.operations.process_once()
        current = self.imports.show(operation["import_id"])
        preview = self.plan(current, "watch")
        other = repository(self.root / "other", remote="git@github.com:owner/other.git")
        source.unlink()
        source.symlink_to(other, target_is_directory=True)
        result = self.apply(current, preview)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(self.service.registry.inspect(self.repo)["project"]["remote"])
