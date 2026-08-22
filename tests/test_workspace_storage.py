from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reposteward import workspace_storage
from reposteward.config import RepositoryPolicy, StorageConfig
from reposteward.pipeline import Pipeline
from reposteward.workspace_storage import (
    delete_workspace,
    scan_workspaces,
    workspace_gc_inventory,
    workspace_statistics,
)

OLD_TIMESTAMP = 1_577_836_800
OLD_ISO = "2020-01-01T00:00:00+00:00"
CUTOFF = "2026-01-01T00:00:00+00:00"


def git(workspace: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def age_tree(path: Path) -> None:
    for current, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            os.utime(
                Path(current) / name,
                (OLD_TIMESTAMP, OLD_TIMESTAMP),
                follow_symlinks=False,
            )
    os.utime(path, (OLD_TIMESTAMP, OLD_TIMESTAMP))


def create_workspace(
    root: Path, name: str = "issue-44-20200101T000000Z"
) -> tuple[Path, str]:
    workspace = root / "owner__repo" / name
    workspace.mkdir(parents=True)
    git(workspace, "init", "--quiet")
    git(workspace, "config", "user.name", "RepoSteward Test")
    git(workspace, "config", "user.email", "test@example.com")
    (workspace / "tracked.txt").write_text("retained source\n", encoding="utf-8")
    git(workspace, "add", "tracked.txt")
    git(workspace, "commit", "--quiet", "-m", "test: seed workspace")
    head = git(workspace, "rev-parse", "HEAD")
    age_tree(workspace)
    return workspace, head


def run_state(workspace: Path, head: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": "owner/repo",
        "status": "submitted",
        "worktree": str(workspace),
        "updated_at": OLD_ISO,
        "head_commit": head,
        "terminal_checkpoint": True,
    }
    result.update(overrides)
    return result


class WorkspaceStorageTests(unittest.TestCase):
    def test_selected_rescan_does_not_inventory_unrelated_workspace_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            selected, selected_head = create_workspace(root)
            unrelated, unrelated_head = create_workspace(
                root, "issue-45-20200101T000000Z"
            )
            runs = {
                "run-1": run_state(selected, selected_head),
                "run-2": run_state(unrelated, unrelated_head),
            }

            inventory = scan_workspaces(
                root,
                repositories=("owner/repo",),
                runs=runs,
                paths=frozenset({"owner__repo/issue-44-20200101T000000Z"}),
            )

        self.assertEqual(
            [item["path"] for item in inventory["items"]],
            ["owner__repo/issue-44-20200101T000000Z"],
        )

    def test_statistics_count_workspace_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspaces"
            workspace, head = create_workspace(root)
            outside = base / "outside.bin"
            outside.write_bytes(b"x" * 1_000_000)
            outside_size = outside.stat().st_size
            os.symlink(outside, workspace / "outside-link")
            age_tree(workspace)
            runs = {"run-1": run_state(workspace, head)}

            inventory = scan_workspaces(root, repositories=("owner/repo",), runs=runs)
            categories, errors = workspace_statistics(
                inventory, repository="owner/repo", cutoff=""
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(categories), 1)
        self.assertEqual(categories[0]["records"], 1)
        self.assertLess(categories[0]["bytes"], outside_size)

    def test_submitted_clean_terminal_workspace_becomes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            workspace, head = create_workspace(root)

            with patch(
                "reposteward.workspace_storage._git", wraps=workspace_storage._git
            ) as git_command:
                result = workspace_gc_inventory(
                    root,
                    repositories=("owner/repo",),
                    runs={"run-1": run_state(workspace, head)},
                    repository="owner/repo",
                    cutoff=CUTOFF,
                )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["retained"], [])
        self.assertEqual(result["candidates"][0]["head_commit"], head)
        self.assertEqual(git_command.call_count, 1)

    def test_dirty_and_unpushed_workspaces_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            workspace, head = create_workspace(root)
            (workspace / "untracked.txt").write_text("local work\n", encoding="utf-8")
            age_tree(workspace)

            dirty = workspace_gc_inventory(
                root,
                repositories=("owner/repo",),
                runs={"run-1": run_state(workspace, head)},
                repository="owner/repo",
                cutoff=CUTOFF,
            )

            (workspace / "untracked.txt").unlink()
            age_tree(workspace)
            unpushed = workspace_gc_inventory(
                root,
                repositories=("owner/repo",),
                runs={
                    "run-1": run_state(
                        workspace,
                        head,
                        status="failed",
                        head_commit="",
                    )
                },
                repository="owner/repo",
                cutoff=CUTOFF,
            )

        self.assertIn("dirty_workspace", dirty["retained"][0]["reasons"])
        self.assertIn("unpushed_commits", unpushed["retained"][0]["reasons"])

    def test_any_active_run_retains_a_shared_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            workspace, head = create_workspace(root)
            runs = {
                "run-1": run_state(workspace, head),
                "run-2": run_state(
                    workspace,
                    head,
                    status="running",
                    terminal_checkpoint=False,
                ),
            }

            result = workspace_gc_inventory(
                root,
                repositories=("owner/repo",),
                runs=runs,
                repository="owner/repo",
                cutoff=CUTOFF,
            )

        reasons = result["retained"][0]["reasons"]
        self.assertIn("active_run", reasons)
        self.assertIn("no_terminal_checkpoint", reasons)

    def test_workspace_symlink_is_never_followed_or_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspaces"
            container = root / "owner__repo"
            container.mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
            link = container / "issue-44-20200101T000000Z"
            os.symlink(outside, link, target_is_directory=True)

            result = workspace_gc_inventory(
                root,
                repositories=("owner/repo",),
                runs={},
                repository="owner/repo",
                cutoff=CUTOFF,
            )

            outside_preserved = (outside / "keep.txt").exists()

        self.assertEqual(result["candidates"], [])
        self.assertIn("workspace_scan_unsafe", result["retained"][0]["reasons"])
        self.assertTrue(outside_preserved)

    def test_delete_rejects_workspace_replaced_after_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspaces"
            workspace, head = create_workspace(root)
            result = workspace_gc_inventory(
                root,
                repositories=("owner/repo",),
                runs={"run-1": run_state(workspace, head)},
                repository="owner/repo",
                cutoff=CUTOFF,
            )
            candidate = result["candidates"][0]
            original = workspace.with_name(f"{workspace.name}-original")
            workspace.rename(original)
            workspace.mkdir()
            (workspace / "keep.txt").write_text("replacement\n", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "identity changed"):
                delete_workspace(root, candidate)

            self.assertTrue((workspace / "keep.txt").exists())


class WorkspaceGcStore:
    def __init__(self, safety: dict[str, dict[str, object]]) -> None:
        self.safety = safety
        self.audit: list[str] = []

    def run_gc_safety(self) -> dict[str, dict[str, object]]:
        return self.safety

    def event_payload_gc_inventory(self, _cutoffs: dict[str, str]) -> dict:
        return {"candidates": [], "retained": []}

    def delete_event_payloads(
        self, _digests: tuple[str, ...], *, retention_cutoffs: dict[str, str]
    ) -> dict:
        return {"deleted": [], "skipped": []}

    def record_storage_gc(self, *, stage: str, **_kwargs: object) -> dict[str, str]:
        self.audit.append(stage)
        return {"id": f"audit-{len(self.audit)}", "stage": stage}


class WorkspaceGcPipelineTests(unittest.TestCase):
    def test_apply_deletes_reviewed_workspace_and_audits_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspaces"
            workspace, head = create_workspace(root)
            store = WorkspaceGcStore({"run-1": run_state(workspace, head)})
            policy = RepositoryPolicy(name="owner/repo")
            pipeline = object.__new__(Pipeline)
            pipeline.config = SimpleNamespace(
                state_dir=base / "state",
                workspace_dir=root,
                storage=StorageConfig(
                    cache_retention_days=30,
                    workspace_retention_days=30,
                    max_gc_items=10,
                ),
                repositories={"owner/repo": policy},
                github=SimpleNamespace(login="operator"),
            )
            pipeline.store = store

            plan = pipeline.storage_gc(repository="owner/repo")
            with patch.dict("os.environ", {"REPOSTEWARD_ENABLE_GC": "1"}):
                applied = pipeline.storage_gc(repository="owner/repo", apply=True)

            workspace_exists = workspace.exists()

        self.assertEqual(plan["candidate_count"], 1)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(workspace_exists)
        self.assertEqual(store.audit, ["applying", "completed"])
        self.assertEqual(len(applied["applied"]["deleted_workspaces"]), 1)
        self.assertEqual(applied["applied"]["skipped_workspaces"], [])


if __name__ == "__main__":
    unittest.main()
