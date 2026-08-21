from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.config import RepositoryPolicy
from reposteward.context import repository_policy_digest
from reposteward.github import GitHubError
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError


class StubStore:
    def __init__(self, policy: RepositoryPolicy) -> None:
        self.policy = policy
        self.audits: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.executions: list[dict] = []

    def run(self, run_id: str) -> dict:
        return {
            "id": run_id,
            "repository": "owner/repo",
            "status": "submitted",
            "details": {
                "pr_url": "https://github.com/owner/repo/pull/12",
                "commit_sha": "a" * 40,
                "base_commit": "b" * 40,
            },
        }

    def context_bundle(self, _run_id: str) -> dict:
        return {
            "context_pack": {
                "project": {"policy_digest": repository_policy_digest(self.policy)}
            }
        }

    def append_merge_decision(self, **kwargs) -> dict:
        self.audits.append(kwargs)
        decision_id = f"audit-{len(self.audits)}"
        decision = kwargs["decision"]
        self.decisions[decision_id] = {
            "id": decision_id,
            "repository": kwargs["repository"],
            "pull_number": kwargs["pull_number"],
            "head_sha": kwargs["head_sha"],
            "base_sha": kwargs["base_sha"],
            "policy_digest": kwargs["policy_digest"],
            "snapshot_digest": decision["snapshot_digest"],
            "eligible": decision["eligible"],
            "decision_digest": decision["decision_digest"],
            "payload": decision,
        }
        return {"id": decision_id}

    def merge_decision(self, decision_id: str) -> dict | None:
        return self.decisions.get(decision_id)

    def append_merge_execution(self, **kwargs) -> dict:
        self.executions.append(kwargs)
        return {
            "id": f"execution-{len(self.executions)}",
            "attempt_id": kwargs["attempt_id"],
            "stage": kwargs["stage"],
            "outcome": kwargs["outcome"],
        }


class StubGitHub:
    def __init__(self) -> None:
        self.activity_marker = "initial"
        self.state = "OPEN"
        self.merge_commit_sha = ""
        self.merge_calls: list[dict] = []
        self.fail_after_merge = False
        self.fail_without_merge = False
        self.activity_calls = 0
        self.mutate_on_activity_call = 0
        self.conversation_marker = "initial"

    def pull_request_activity(self, repository: str, number: int) -> dict:
        self.activity_calls += 1
        if self.activity_calls == self.mutate_on_activity_call:
            self.activity_marker = "changed-during-execution"
        return {
            "pull_request": {
                "number": number,
                "head_sha": "a" * 40,
                "marker": self.activity_marker,
            },
            "comments": [],
            "reviews": [],
            "review_comments": [],
            "checks": [],
        }

    def pull_request_merge_snapshot(self, repository: str, number: int) -> dict:
        return {
            "repository": repository,
            "pull_number": number,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "state": self.state,
            "draft": False,
            "mergeable": "MERGEABLE",
            "review_decision": "APPROVED",
            "unresolved_conversations": 0,
            "conversation_digest": self.conversation_marker,
            "files": ["src/example.py"],
            "additions": 10,
            "deletions": 2,
            "checks": [
                {
                    "name": "quality",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "required": True,
                }
            ],
            "files_complete": True,
            "conversations_complete": True,
            "checks_complete": True,
            "merge_commit_sha": self.merge_commit_sha,
        }

    def authenticated_login(self) -> str:
        return "alice"

    def repository(self, _repository: str) -> SimpleNamespace:
        return SimpleNamespace(can_push=True)

    def merge_pull_request(
        self, repository: str, number: int, *, head_sha: str, method: str
    ) -> dict:
        self.merge_calls.append(
            {
                "repository": repository,
                "number": number,
                "head_sha": head_sha,
                "method": method,
            }
        )
        if self.fail_without_merge:
            raise GitHubError("network unavailable")
        self.state = "MERGED"
        self.merge_commit_sha = "f" * 40
        if self.fail_after_merge:
            raise GitHubError("network timeout")
        return {"merged": True, "sha": self.merge_commit_sha, "message": "merged"}


class MergePipelineTests(unittest.TestCase):
    @staticmethod
    def pipeline(*, auto_merge: bool = False) -> Pipeline:
        policy = RepositoryPolicy(
            name="owner/repo",
            auto_merge=auto_merge,
            mode="maintainer",
            submission_strategy="same-repository",
        )
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": policy},
            safety=SimpleNamespace(max_files_changed=18, max_diff_lines=700),
            github=SimpleNamespace(login="alice"),
        )
        pipeline.store = StubStore(policy)
        pipeline.github = StubGitHub()
        return pipeline

    def test_decision_reads_current_snapshot_and_appends_every_audit(self) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": policy},
            safety=SimpleNamespace(max_files_changed=18, max_diff_lines=700),
        )
        pipeline.store = StubStore(policy)
        pipeline.github = StubGitHub()

        first = pipeline.merge_decision("run-1")
        second = pipeline.merge_decision("run-1")

        self.assertTrue(first["eligible"])
        self.assertFalse(first["public_write"])
        self.assertEqual(first["decision_digest"], second["decision_digest"])
        self.assertEqual(len(pipeline.store.audits), 2)
        self.assertNotEqual(first["audit"]["id"], second["audit"]["id"])
        self.assertEqual(
            pipeline.store.audits[0]["decision"]["snapshot"]["head_sha"],
            "a" * 40,
        )

    def test_opted_in_fresh_decision_merges_once_and_audits_intent_and_result(
        self,
    ) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}):
            result = pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertTrue(result["merged"])
        self.assertFalse(result["idempotent"])
        self.assertTrue(result["public_write"])
        self.assertEqual(len(pipeline.github.merge_calls), 1)
        self.assertEqual(
            [value["stage"] for value in pipeline.store.executions],
            ["applying", "completed"],
        )
        self.assertEqual(pipeline.store.executions[-1]["outcome"], "merged")

    def test_executor_is_disabled_by_default_and_records_the_block(self) -> None:
        pipeline = self.pipeline(auto_merge=False)
        decision = pipeline.merge_decision("run-1")

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}),
            self.assertRaisesRegex(PolicyError, "auto_merge is not explicitly enabled"),
        ):
            pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertEqual(pipeline.store.executions[-1]["outcome"], "blocked")
        self.assertEqual(pipeline.github.merge_calls, [])

    def test_activity_change_makes_the_decision_stale_without_public_write(
        self,
    ) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.activity_marker = "new-review"

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}),
            self.assertRaisesRegex(PolicyError, "decision is stale"),
        ):
            pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertEqual(pipeline.github.merge_calls, [])
        self.assertEqual(pipeline.store.executions[-1]["outcome"], "blocked")

    def test_conversation_change_makes_the_decision_stale_without_public_write(
        self,
    ) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.conversation_marker = "thread-resolution-changed"

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}),
            self.assertRaisesRegex(PolicyError, "decision is stale"),
        ):
            pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertEqual(pipeline.github.merge_calls, [])
        self.assertEqual(pipeline.store.executions[-1]["outcome"], "blocked")

    def test_already_merged_verified_head_is_idempotent(self) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.state = "MERGED"
        pipeline.github.merge_commit_sha = "f" * 40

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}):
            result = pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertTrue(result["idempotent"])
        self.assertFalse(result["public_write"])
        self.assertEqual(result["merge_commit_sha"], "f" * 40)
        self.assertEqual(pipeline.github.merge_calls, [])
        self.assertEqual(pipeline.store.executions[-1]["outcome"], "already_merged")

    def test_uncertain_merge_response_is_reconciled_before_retry(self) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.fail_after_merge = True

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}):
            result = pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertTrue(result["merged"])
        self.assertEqual(len(pipeline.github.merge_calls), 1)
        self.assertIn("uncertain", pipeline.store.executions[-1]["reason"])

    def test_just_in_time_activity_change_blocks_after_intent_audit(self) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.mutate_on_activity_call = 3

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}),
            self.assertRaisesRegex(PolicyError, "changed during execution"),
        ):
            pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertEqual(pipeline.github.merge_calls, [])
        self.assertEqual(
            [value["outcome"] for value in pipeline.store.executions],
            ["pending", "blocked"],
        )

    def test_failed_merge_is_not_blindly_retried_while_pr_remains_open(self) -> None:
        pipeline = self.pipeline(auto_merge=True)
        decision = pipeline.merge_decision("run-1")
        pipeline.github.fail_without_merge = True

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}),
            self.assertRaisesRegex(PolicyError, "did not merge"),
        ):
            pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertEqual(len(pipeline.github.merge_calls), 1)
        self.assertEqual(pipeline.store.executions[-1]["outcome"], "failed")
