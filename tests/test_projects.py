from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reposteward.cli import main
from reposteward.config import load_config
from reposteward.projects import ProjectError, ProjectRegistry, normalize_remote
from reposteward.setup import add_repository, initialize_user_config


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(root: Path, remote: str = "https://github.com/Owner/Repo.git") -> Path:
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", remote)
    (root / "source.txt").write_text("initial\n")
    git(root, "add", "source.txt")
    git(root, "commit", "-m", "Initial")
    return root


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = repository(self.root / "checkout")
        self.db = self.root / "state/projects.sqlite3"
        self.registry = ProjectRegistry(self.db)

    def test_read_does_not_create_or_migrate_database(self) -> None:
        self.assertEqual(self.registry.list()["projects"], [])
        self.assertFalse(self.db.exists())
        with self.assertRaises(ProjectError):
            self.registry.inspect(self.repo)
        self.assertFalse(self.db.exists())
        self.registry.link(self.repo)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version=99")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ProjectError, "schema"):
            self.registry.list()
        self.assertEqual(before, self.db.read_bytes())

    def test_link_subdirectory_and_dirty_inspection_are_idempotent(self) -> None:
        (self.repo / "src").mkdir()
        first = self.registry.link(self.repo / "src")
        again = self.registry.link(self.repo)
        self.assertEqual(first["project"]["id"], again["project"]["id"])
        self.assertEqual(first["binding"]["id"], again["binding"]["id"])
        self.assertFalse(first["workspace"]["dirty"])
        (self.repo / "source.txt").write_text("changed\n")
        report = self.registry.inspect(self.repo / "src")
        self.assertTrue(report["workspace"]["dirty"])
        self.assertEqual(report["project"]["repository"], "owner/repo")
        self.assertEqual(len(self.registry.list()["projects"]), 1)

    def test_worktree_and_second_clone_share_project_but_not_binding(self) -> None:
        first = self.registry.link(self.repo)
        worktree = self.root / "worktree"
        git(self.repo, "worktree", "add", "-b", "feature", str(worktree))
        second = self.registry.link(worktree)
        third = self.registry.link(repository(self.root / "clone"))
        self.assertEqual(
            {x["project"]["id"] for x in (first, second, third)},
            {first["project"]["id"]},
        )
        self.assertEqual(len({x["binding"]["id"] for x in (first, second, third)}), 3)
        self.assertEqual(self.registry.list()["projects"][0]["workspace_count"], 3)

    def test_identity_changes_require_explicit_rebinding(self) -> None:
        self.registry.link(self.repo)
        git(
            self.repo,
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/different.git",
        )
        with self.assertRaisesRegex(ProjectError, "identity"):
            self.registry.inspect(self.repo)
        with self.assertRaisesRegex(ProjectError, "identity"):
            self.registry.link(self.repo)

    def test_unlink_preserves_files_and_can_be_repeated(self) -> None:
        bound = self.registry.link(self.repo)
        head = git(self.repo, "rev-parse", "HEAD")
        self.registry.unlink(bound["binding"]["id"])
        self.registry.unlink(bound["binding"]["id"])
        self.assertTrue((self.repo / "source.txt").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.registry.list()["projects"][0]["workspace_count"], 0)
        with self.assertRaises(ProjectError):
            self.registry.inspect(self.repo)
        self.assertEqual(
            self.registry.link(self.repo)["project"]["id"], bound["project"]["id"]
        )

    def test_missing_moved_directory_is_visible_and_can_be_linked_again(self) -> None:
        first = self.registry.link(self.repo)
        moved = self.root / "moved"
        self.repo.rename(moved)
        self.assertEqual(self.registry.list()["projects"][0]["missing_workspaces"], 1)
        self.assertEqual(
            self.registry.link(moved)["project"]["id"], first["project"]["id"]
        )

    def test_concurrent_link_has_one_identity(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(
                pool.map(
                    lambda _: self.registry.link(self.repo)["binding"]["id"], range(4)
                )
            )
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.registry.list()["projects"][0]["workspace_count"], 1)

    def test_normalized_remote_discards_auth_and_rejects_ambiguous_urls(self) -> None:
        expected = "github.com/owner/repo"
        for raw in (
            "https://example:fake-password@github.com/Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo.git",
            "git@github.com:Owner/Repo.git",
        ):
            self.assertEqual(normalize_remote(raw)["identity"], expected)
        for raw in (
            "/tmp/repo",
            "https://github.com/a/b?token=example",
            "https://github.com/a/../b",
            "https://github.com/a/b#fragment",
        ):
            with self.assertRaises(ProjectError):
                normalize_remote(raw)

    def test_managed_metadata_does_not_run_repository_fsmonitor(self) -> None:
        marker = self.root / "fsmonitor-ran"
        script = self.root / "monitor.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", str(script))
        self.registry.link(self.repo)
        self.assertFalse(marker.exists())

    def test_maintainer_scaffold_has_personal_repository_star_default(self) -> None:
        user = self.root / "user.toml"
        initialize_user_config(path=user, login="owner")
        for mode, minimum in (("maintainer", 0), ("contributor", 1000)):
            config = self.root / f"{mode}.toml"
            add_repository("owner/repo", path=config, mode=mode)
            self.assertEqual(
                load_config(config, user_path=user)
                .repositories["owner/repo"]
                .min_stars,
                minimum,
            )

    def test_project_cli_never_constructs_pipeline(self) -> None:
        user = self.root / "user.toml"
        initialize_user_config(path=user, login="owner")
        config = replace(load_config(user), state_dir=self.root / "cli-state")
        with (
            patch("reposteward.cli.load_config", return_value=config),
            patch(
                "reposteward.cli.Pipeline",
                side_effect=AssertionError("unexpected Pipeline"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["project", "link", str(self.repo)]), 0)
        self.assertEqual(
            json.loads(output.getvalue())["project"]["repository"], "owner/repo"
        )


if __name__ == "__main__":
    unittest.main()
