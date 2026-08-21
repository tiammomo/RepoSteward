from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.config import RepositoryPolicy
from reposteward.context import repository_policy_digest
from reposteward.models import (
    AgentExecution,
    AgentMetrics,
    AgentResult,
    Candidate,
    Issue,
    RepositoryInfo,
    VerificationResult,
)
from reposteward.pipeline import (
    Pipeline,
    _canonical_digest,
    _repair_feedback,
)
from reposteward.policy import DiffSummary, PolicyError


def _candidate() -> Candidate:
    return Candidate(
        issue=Issue(
            repository="owner/repo",
            number=7,
            node_id=8,
            title="Fix the edge case",
            body="Original scope",
            url="https://github.com/owner/repo/issues/7",
            labels=("bug",),
            comments=0,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            author_login="reporter",
            author_association="NONE",
        ),
        repository=RepositoryInfo(
            full_name="owner/repo",
            default_branch="main",
            stars=100,
            forks=2,
            open_issues=3,
            pushed_at="2026-01-02T00:00:00Z",
            archived=False,
            is_fork=False,
        ),
        score=50,
    )


def _follow(**overrides: object) -> dict:
    value = {
        "changed": True,
        "head_matches_verified_commit": True,
        "next_action": "review_new_activity",
        "event_watermark": 9,
        "event_batch_digest": "d" * 64,
        "pull_request": {"number": 12},
        "_previous_event_watermark": 3,
        "_checkpoint_payload": {"status": "submitted"},
        "_github_snapshot": {"pull_request": {"number": 12}},
        "new_comments": [],
        "new_comments_omitted": 0,
        "new_reviews": [],
        "new_reviews_omitted": 0,
        "new_review_comments": [],
        "new_review_comments_omitted": 0,
        "changed_checks": [],
        "changed_checks_omitted": 0,
    }
    value.update(overrides)
    return value


def _snapshot() -> dict:
    return {
        "repository": "owner/repo",
        "pull_number": 12,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "state": "OPEN",
        "draft": True,
        "mergeable": "MERGEABLE",
        "review_decision": "CHANGES_REQUESTED",
        "unresolved_conversations": 1,
        "files": ["src/example.py"],
        "additions": 2,
        "deletions": 1,
        "checks": [],
        "files_complete": True,
        "conversations_complete": True,
        "checks_complete": True,
    }


class RepairPlanningTests(unittest.TestCase):
    def test_path_outside_existing_diff_is_recorded_as_suggestion(self) -> None:
        actionable, suggestions = _repair_feedback(
            _follow(
                new_review_comments=[
                    {"id": 1, "path": "docs/unrelated.md", "body": "also rewrite this"},
                    {"id": 2, "path": "src/example.py", "body": "handle None"},
                ]
            ),
            ("src/example.py",),
        )

        self.assertEqual([value["id"] for value in actionable], [2])
        self.assertEqual([value["id"] for value in suggestions], [1])
        self.assertEqual(
            suggestions[0]["reason"], "path_outside_existing_pull_request_scope"
        )

    def test_no_new_activity_never_invokes_harness(self) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(repositories={"owner/repo": policy})
        pipeline.store = Mock()
        pipeline.store.run.return_value = {
            "id": "source",
            "repository": "owner/repo",
            "issue_number": 7,
            "status": "submitted",
            "details": {"pr_url": "https://github.com/owner/repo/pull/12"},
        }
        pipeline._follow_up = Mock(
            return_value=_follow(changed=False, next_action="wait_for_activity")
        )
        pipeline.harness = Mock()

        result = pipeline.prepare_repair("source")

        self.assertFalse(result["repair_prepared"])
        self.assertFalse(result["harness_invoked"])
        pipeline.harness.run.assert_not_called()
        pipeline.store.start_run.assert_not_called()

    def test_actionable_feedback_is_committed_verified_and_left_for_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                cwd=root,
                check=True,
            )
            parent = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            snapshot = _snapshot()
            snapshot["head_sha"] = parent
            policy = RepositoryPolicy(
                name="owner/repo", verification_prefixes=("pytest ",)
            )
            source = {
                "id": "source",
                "repository": "owner/repo",
                "issue_number": 7,
                "status": "submitted",
                "stage": "pull_request",
                "worktree": str(root),
                "details": {
                    "worktree": str(root),
                    "base_branch": "main",
                    "base_commit": "b" * 40,
                    "branch": "alice/fix-7",
                    "commit_sha": parent,
                    "changed_files": ["src/example.py"],
                    "pr_url": "https://github.com/owner/repo/pull/12",
                },
            }
            repair_run: dict = {"id": "repair-1", "details": {}}
            store = Mock()

            def get_run(run_id: str) -> dict:
                return source if run_id == "source" else repair_run

            def update_run(run_id: str, **values: object) -> None:
                if run_id != "repair-1":
                    return
                repair_run.update(
                    {key: value for key, value in values.items() if key != "details"}
                )
                if "details" in values:
                    repair_run["details"] = values["details"]
                repair_run.setdefault("repository", "owner/repo")
                repair_run.setdefault("issue_number", 7)
                repair_run.setdefault("updated_at", "2026-01-03T00:00:00Z")
                repair_run.setdefault("worktree", str(root))

            store.run.side_effect = get_run
            store.update_run.side_effect = update_run
            store.context_bundle.return_value = {
                "work_item": {"id": "work-1"},
                "context_pack": {
                    "project": {"policy_digest": repository_policy_digest(policy)}
                },
                "checkpoint": {"id": "checkpoint-1", "status": "submitted"},
            }
            store.candidate.return_value = _candidate()
            store.start_run.return_value = "repair-1"
            store.latest_harness_session.return_value = "session-1"
            store.save_checkpoint.return_value = {}

            class Harness:
                name = "fake"

                def run(self, request: object) -> AgentExecution:
                    (root / "src" / "example.py").write_text(
                        "value = 2\n", encoding="utf-8"
                    )
                    return AgentExecution(
                        result=AgentResult(
                            summary="Address review.",
                            pr_title="fix(repo): address review feedback",
                            implementation_notes="Changed the branch.",
                            verification_commands=("pytest -q",),
                        ),
                        metrics=AgentMetrics(),
                        harness="fake",
                        model="test",
                    )

            pipeline = object.__new__(Pipeline)
            pipeline.config = SimpleNamespace(
                repositories={"owner/repo": policy},
                agent=SimpleNamespace(model="test"),
                state_dir=root / "state",
            )
            pipeline.store = store
            pipeline.github = Mock()
            pipeline.github.pull_request_merge_snapshot.return_value = snapshot
            pipeline._follow_up = Mock(
                return_value=_follow(
                    new_review_comments=[
                        {"id": 2, "path": "src/example.py", "body": "handle None"}
                    ]
                )
            )
            pipeline.harness = Harness()
            pipeline.verifier = Mock()
            pipeline.verifier.verify.return_value = VerificationResult(True, ())
            pipeline.workspaces = Mock()
            pipeline.workspaces.commit.return_value = "c" * 40

            with patch(
                "reposteward.pipeline.enforce_change_policy",
                return_value=DiffSummary(("src/example.py",), 1, 1),
            ):
                result = pipeline.prepare_repair("source")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["commit_sha"], "c" * 40)
        self.assertEqual(repair_run["details"]["repair_guard"]["parent_commit"], parent)
        store.seed_github_pr_watermark.assert_called_once()
        pipeline.verifier.verify.assert_called_once()
        pipeline.workspaces.commit.assert_called_once()


class RepairSubmissionGuardTests(unittest.TestCase):
    def test_guard_accepts_identical_facts_and_rejects_new_events(self) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        snapshot = _snapshot()
        guard = {
            "source_run_id": "source",
            "pull_number": 12,
            "parent_commit": "a" * 40,
            "base_sha": "b" * 40,
            "base_branch": "main",
            "policy_digest": repository_policy_digest(policy),
            "event_watermark": 9,
            "snapshot_digest": _canonical_digest(snapshot),
        }
        activity = {"pull_request": {"head_sha": "a" * 40, "base_branch": "main"}}
        pipeline = object.__new__(Pipeline)
        pipeline.store = Mock()
        pipeline.store.run.return_value = {"status": "submitted"}
        pipeline.store.ingest_github_pr_activity.return_value = {
            "previous_sequence": 9,
            "through_sequence": 9,
        }
        client = Mock()
        client.pull_request_activity.return_value = activity
        client.pull_request_merge_snapshot.return_value = snapshot

        pipeline._validate_repair_submission(
            client=client, policy=policy, details={"repair_guard": guard}
        )
        pipeline.store.ingest_github_pr_activity.return_value["through_sequence"] = 10

        with self.assertRaisesRegex(PolicyError, "event_watermark"):
            pipeline._validate_repair_submission(
                client=client, policy=policy, details={"repair_guard": guard}
            )


if __name__ == "__main__":
    unittest.main()
