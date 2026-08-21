from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reposteward.agent import build_harness_prompt
from reposteward.config import RepositoryPolicy
from reposteward.context import repository_policy_digest
from reposteward.context_budget import estimate_tokens
from reposteward.models import (
    AgentExecution,
    AgentMetrics,
    AgentResult,
    VerificationResult,
)
from reposteward.pipeline import Pipeline, _canonical_digest, _repair_feedback
from reposteward.policy import DiffSummary, PolicyError


def _follow(**extra: object) -> dict:
    value = {
        "changed": True,
        "head_matches_verified_commit": True,
        "next_action": "review_new_activity",
        "event_watermark": 9,
        "event_batch_digest": "d" * 64,
        "pull_request": {"number": 12},
        "new_comments": [],
        "new_comments_omitted": 0,
        "new_reviews": [],
        "new_reviews_omitted": 0,
        "new_review_comments": [],
        "new_review_comments_omitted": 0,
        "changed_checks": [],
        "changed_checks_omitted": 0,
        "_previous_event_watermark": 3,
        "_checkpoint_payload": {"status": "submitted"},
        "_github_snapshot": {},
    }
    value.update(extra)
    events = []
    for kind, key in (
        ("issue_comment", "new_comments"),
        ("review", "new_reviews"),
        ("review_comment", "new_review_comments"),
    ):
        events.extend({"kind": kind, **item} for item in value[key])
    failed_checks = [
        item
        for item in value["changed_checks"]
        if str(item.get("conclusion") or "").casefold()
        in {"action_required", "cancelled", "failure", "stale", "timed_out"}
    ]
    value["context_plan"] = {
        "schema_version": 1,
        "budget_tokens": 12_000,
        "estimated_tokens": 100,
        "mandatory": {
            "failed_checks": failed_checks,
            "safety_blockers": [],
            "blocking_reviews": [],
        },
        "events": events,
        "diff_snippets": [],
        "actionable": bool(events or failed_checks),
        "stats": {"new_failed_checks": len(failed_checks)},
    }
    return value


class RepairTests(unittest.TestCase):
    def test_out_of_scope_feedback_is_persisted_as_a_suggestion(self) -> None:
        actionable, suggestions = _repair_feedback(
            _follow(
                new_review_comments=[
                    {"id": 1, "path": "docs/other.md", "body": "rewrite"}
                ]
            ),
            ("src/example.py",),
        )
        pipeline = object.__new__(Pipeline)
        pipeline.store = Mock()
        pipeline.store.commit_github_follow_up.return_value = {"sequence": 9}
        source = {
            "id": "source",
            "repository": "owner/repo",
            "status": "submitted",
            "stage": "pull_request",
            "worktree": "/tmp/worktree",
            "details": {},
        }

        pipeline._commit_repair_event_preview(
            source, _follow(), suggestions=suggestions
        )

        self.assertEqual(actionable, ())
        saved = pipeline.store.update_run.call_args.kwargs["details"]
        self.assertEqual(saved["repair_suggestions"][0]["id"], 1)

    def test_no_new_activity_does_not_start_a_run_or_harness(self) -> None:
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="owner/repo")}
        )
        pipeline.store = Mock()
        pipeline.store.run.return_value = {
            "id": "source",
            "repository": "owner/repo",
            "issue_number": 7,
            "status": "submitted",
            "details": {"pr_url": "https://example.test/pull/12"},
        }
        pipeline._follow_up = Mock(
            return_value=_follow(changed=False, next_action="wait_for_activity")
        )
        pipeline.harness = Mock()

        pipeline.prepare_repair("source")
        pipeline.harness.run.assert_not_called()

    def test_successful_check_only_advances_watermark_without_harness(self) -> None:
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="owner/repo")}
        )
        pipeline.store = Mock()
        pipeline.store.run.return_value = {
            "id": "source",
            "repository": "owner/repo",
            "issue_number": 7,
            "status": "submitted",
            "stage": "pull_request",
            "worktree": "/tmp/worktree",
            "details": {"pr_url": "https://example.test/pull/12"},
        }
        pipeline.store.commit_github_follow_up.return_value = {
            "sequence": 9,
            "checkpoint": None,
        }
        pipeline._follow_up = Mock(
            return_value=_follow(
                changed_checks=[
                    {
                        "id": "check-1",
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
                next_action="wait_for_activity",
            )
        )
        pipeline.harness = Mock()

        result = pipeline.prepare_repair("source")

        self.assertEqual(result["reason"], "no_new_actionable_activity")
        pipeline.store.commit_github_follow_up.assert_called_once()
        pipeline.harness.run.assert_not_called()

    def test_diff_reader_only_uses_verified_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / ".git").mkdir()
            details = {
                "worktree": str(worktree),
                "base_commit": "a" * 40,
                "commit_sha": "b" * 40,
                "changed_files": ["src/allowed.py"],
            }
            events = [
                {
                    "event_type": "review_comment",
                    "payload": {"path": "src/allowed.py"},
                },
                {
                    "event_type": "review_comment",
                    "payload": {"path": "../outside.py"},
                },
            ]
            completed = SimpleNamespace(returncode=0, stdout="bounded diff")

            with patch(
                "reposteward.pipeline.subprocess.run", return_value=completed
            ) as run:
                snippets = Pipeline._follow_up_diff_snippets(details, events)

        self.assertEqual(snippets, {"src/allowed.py": "bounded diff"})
        self.assertEqual(run.call_args.args[0][-1], "src/allowed.py")

    def test_in_scope_feedback_is_verified_and_left_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / ".git").mkdir()
            parent = "a" * 40
            policy = RepositoryPolicy(name="owner/repo")
            source = {
                "id": "source",
                "repository": "owner/repo",
                "issue_number": 7,
                "status": "submitted",
                "stage": "pull_request",
                "worktree": str(worktree),
                "details": {
                    "worktree": str(worktree),
                    "base_branch": "main",
                    "base_commit": "b" * 40,
                    "branch": "alice/fix",
                    "commit_sha": parent,
                    "changed_files": ["src/example.py"],
                    "pr_url": "https://example.test/pull/12",
                },
            }
            ready = {"id": "repair", "repository": "owner/repo", "issue_number": 7}
            store = Mock()
            store.run.side_effect = lambda run_id: (
                source if run_id == "source" else ready
            )
            store.start_run.return_value = "repair"
            store.candidate.return_value = SimpleNamespace(
                issue=SimpleNamespace(
                    repository="owner/repo",
                    number=7,
                    title="Fix edge",
                    body="Original scope",
                    url="https://example.test/issues/7",
                    updated_at="2026-01-02T00:00:00Z",
                ),
                repository=SimpleNamespace(default_branch="main"),
            )
            store.context_bundle.return_value = {
                "work_item": {"id": "work"},
                "context_pack": {
                    "project": {"policy_digest": repository_policy_digest(policy)}
                },
                "checkpoint": {"id": "checkpoint", "status": "submitted"},
            }
            store.latest_harness_session.return_value = "session"
            store.save_checkpoint.return_value = {}
            store.update_run.side_effect = lambda run_id, **values: ready.update(values)
            harness = Mock()
            harness.name = "fake"
            harness.run.return_value = AgentExecution(
                AgentResult("Fixed.", "fix(repo): address review", "notes", ()),
                AgentMetrics(),
                "fake",
                "test",
            )
            pipeline = object.__new__(Pipeline)
            pipeline.config = SimpleNamespace(
                repositories={"owner/repo": policy},
                agent=SimpleNamespace(model="test"),
                state_dir=worktree / "state",
            )
            pipeline.store, pipeline.harness = store, harness
            pipeline.github, pipeline.verifier, pipeline.workspaces = (
                Mock(),
                Mock(),
                Mock(),
            )
            pipeline.github.pull_request_merge_snapshot.return_value = {
                "head_sha": parent,
                "base_sha": "b" * 40,
                "state": "OPEN",
            }
            pipeline.verifier.verify.return_value = VerificationResult(True, ())
            pipeline.workspaces.commit.return_value = "c" * 40
            pipeline._follow_up = Mock(
                return_value=_follow(
                    new_review_comments=[
                        {"id": 2, "path": "src/example.py", "body": "handle None"}
                    ]
                )
            )

            with (
                patch.object(Pipeline, "_revision", return_value=parent),
                patch(
                    "reposteward.pipeline.subprocess.run",
                    return_value=SimpleNamespace(stdout=""),
                ),
                patch(
                    "reposteward.pipeline.enforce_change_policy",
                    return_value=DiffSummary(("src/example.py",), 1, 1),
                ),
            ):
                pipeline.prepare_repair("source")

        harness.run.assert_called_once()
        pipeline.verifier.verify.assert_called_once()
        store.commit_github_follow_up.assert_called_once()
        request = harness.run.call_args.args[0]
        prompt_tokens = estimate_tokens(build_harness_prompt(request.context))
        self.assertLessEqual(prompt_tokens, 12_000)
        self.assertEqual(
            ready["details"]["context_budget"]["estimated_tokens"], prompt_tokens
        )


class RepairSubmissionTests(unittest.TestCase):
    def test_submission_guard_fails_when_events_change(self) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        snapshot = {"head_sha": "a" * 40, "base_sha": "b" * 40}
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
        pipeline = object.__new__(Pipeline)
        pipeline.store = Mock()
        pipeline.store.run.return_value = {"status": "submitted"}
        pipeline.store.ingest_github_pr_activity.return_value = {
            "previous_sequence": 9,
            "through_sequence": 9,
        }
        client = Mock()
        client.pull_request_activity.return_value = {
            "pull_request": {"head_sha": "a" * 40, "base_branch": "main"}
        }
        client.pull_request_merge_snapshot.return_value = snapshot

        pipeline._validate_repair_submission(
            client=client, policy=policy, details={"repair_guard": guard}
        )
        pipeline.store.ingest_github_pr_activity.return_value["through_sequence"] = 10

        with self.assertRaisesRegex(PolicyError, "event_watermark"):
            pipeline._validate_repair_submission(
                client=client, policy=policy, details={"repair_guard": guard}
            )

    def test_submission_guard_fails_when_snapshot_changes(self) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        snapshot = {"head_sha": "a" * 40, "base_sha": "b" * 40}
        pipeline = object.__new__(Pipeline)
        pipeline.store = Mock()
        pipeline.store.run.return_value = {"status": "submitted"}
        pipeline.store.ingest_github_pr_activity.return_value = {
            "previous_sequence": 9,
            "through_sequence": 9,
        }
        client = Mock()
        client.pull_request_activity.return_value = {
            "pull_request": {"head_sha": "a" * 40, "base_branch": "main"}
        }
        client.pull_request_merge_snapshot.return_value = {
            **snapshot,
            "review_decision": "CHANGES_REQUESTED",
        }
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

        with self.assertRaisesRegex(PolicyError, "github_snapshot"):
            pipeline._validate_repair_submission(
                client=client, policy=policy, details={"repair_guard": guard}
            )
