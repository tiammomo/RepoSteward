from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reposteward.config import RepositoryPolicy
from reposteward.context import repository_policy_digest
from reposteward.github import GitHubError, PullRequest
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError


class StubStore:
    def __init__(self, policy: RepositoryPolicy) -> None:
        self.policy = policy
        self.audits: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.executions: list[dict] = []
        self.dependency_events: list[dict] = []
        self.owner_attestation: dict | None = None

    def run(self, run_id: str) -> dict:
        return {
            "id": run_id,
            "repository": "owner/repo",
            "status": "submitted",
            "details": {
                "pr_url": "https://github.com/owner/repo/pull/12",
                "commit_sha": "a" * 40,
                "base_commit": "b" * 40,
                "branch": "alice/feat/example",
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

    def latest_portfolio_dependency_events(
        self, _repository: str, *, pull_number: int = 0
    ) -> list[dict]:
        return [
            value
            for value in self.dependency_events
            if not pull_number or int(value["pull_number"]) == pull_number
        ]

    def append_owner_review_attestation(self, *, facts: dict) -> dict:
        facts_digest, attestation_digest = Pipeline._owner_review_attestation_material(
            facts
        )
        if (
            self.owner_attestation is not None
            and self.owner_attestation["review_facts_digest"] == facts_digest
        ):
            return {**self.owner_attestation, "idempotent": True}
        self.owner_attestation = {
            "id": "owner-attestation-1",
            "actor": facts["actor"],
            "facts": facts,
            "review_facts_digest": facts_digest,
            "attestation_digest": attestation_digest,
            "idempotent": False,
        }
        return self.owner_attestation

    def latest_owner_review_attestation(
        self, _repository: str, _pull_number: int
    ) -> dict | None:
        return self.owner_attestation

    def harness_usage_rows(self, _repository: str, **_filters) -> list[dict]:
        return []

    def latest_merge_outcomes(self, _repository: str) -> dict[int, str]:
        completed = [
            value for value in self.executions if value["stage"] == "completed"
        ]
        return {12: completed[-1]["outcome"]} if completed else {}


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
        self.body = ""
        self.dependency_target_merged = False
        self.review_decision = "APPROVED"
        self.pull_author = "alice"
        self.head_owner = "owner"
        self.head_repository = "owner/repo"
        self.head_branch = "alice/feat/example"
        self.activity_head_sha = "a" * 40
        self.rules_require_review = False
        self.rules_calls = 0
        self.can_admin = True
        self.owner_login = "owner"
        self.files = ["src/example.py"]
        self.optional_check_pending = False

    def pull_request_activity(
        self, repository: str, number: int, *, include_body: bool = False
    ) -> dict:
        self.activity_calls += 1
        if self.activity_calls == self.mutate_on_activity_call:
            self.activity_marker = "changed-during-execution"
        pull = {
            "number": number,
            "head_sha": self.activity_head_sha,
            "base_sha": "b" * 40,
            "marker": self.activity_marker,
        }
        if include_body:
            pull["body"] = self.body
            pull["author"] = self.pull_author
            pull["head_owner"] = self.head_owner
            pull["head_repository"] = self.head_repository
            pull["head_branch"] = self.head_branch
        return {
            "pull_request": pull,
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
            "head_branch": self.head_branch,
            "base_branch": "main",
            "state": self.state,
            "draft": False,
            "mergeable": "MERGEABLE",
            "review_decision": self.review_decision,
            "unresolved_conversations": 0,
            "conversation_digest": self.conversation_marker,
            "files": self.files,
            "additions": 10,
            "deletions": 2,
            "checks": [
                {
                    "name": "quality",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "required": True,
                },
                *(
                    [
                        {
                            "name": "optional",
                            "status": "IN_PROGRESS",
                            "conclusion": "",
                            "required": False,
                        }
                    ]
                    if self.optional_check_pending
                    else []
                ),
            ],
            "files_complete": True,
            "conversations_complete": True,
            "checks_complete": True,
            "merge_commit_sha": self.merge_commit_sha,
        }

    def pull_request(self, _repository: str, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://example.test/pulls/{number}",
            state="closed" if self.dependency_target_merged else "open",
            draft=False,
            merged=self.dependency_target_merged,
            head_sha="e" * 40,
            base_branch="main",
            base_sha="b" * 40,
        )

    def authenticated_login(self) -> str:
        return "alice"

    def repository(self, _repository: str) -> SimpleNamespace:
        return SimpleNamespace(
            can_push=True,
            can_admin=self.can_admin,
            owner_login=self.owner_login,
        )

    def branch_review_policy(self, repository: str, branch: str) -> dict:
        self.rules_calls += 1
        return {
            "repository": repository,
            "branch": branch,
            "complete": True,
            "requirements": (
                ["ruleset:approvals=1"] if self.rules_require_review else []
            ),
            "requires_independent_review": self.rules_require_review,
            "rules_digest": "9" * 64,
        }

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
    def pipeline(
        *, auto_merge: bool = False, owner_attestation: bool = False
    ) -> Pipeline:
        policy = RepositoryPolicy(
            name="owner/repo",
            auto_merge=auto_merge,
            owner_attestation=owner_attestation,
            mode="maintainer",
            submission_strategy="same-repository",
        )
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": policy},
            safety=SimpleNamespace(max_files_changed=18, max_diff_lines=700),
            github=SimpleNamespace(login="alice"),
            observability=SimpleNamespace(prices=()),
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

    def test_open_declared_dependency_blocks_merge_until_it_is_merged(self) -> None:
        pipeline = self.pipeline()
        pipeline.github.body = "Depends on #11"

        blocked = pipeline.merge_decision("run-1")
        pipeline.github.dependency_target_merged = True
        satisfied = pipeline.merge_decision("run-1")

        self.assertFalse(blocked["eligible"])
        self.assertIn(
            "dependency_blocked", [value["code"] for value in blocked["reasons"]]
        )
        self.assertTrue(satisfied["eligible"])
        self.assertNotEqual(blocked["decision_digest"], satisfied["decision_digest"])

    def test_owner_attestation_is_opt_in_and_adds_no_default_api_reads(self) -> None:
        pipeline = self.pipeline()
        pipeline.github.review_decision = ""

        decision = pipeline.merge_decision("run-1")

        self.assertFalse(decision["eligible"])
        self.assertEqual(pipeline.github.rules_calls, 0)
        self.assertEqual(decision["reasons"][0]["code"], "review_not_approved")

    def test_owner_attestation_makes_exact_unapproved_facts_eligible(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}):
            attestation = pipeline.attest_owner_review("run-1", reviewed_by="alice")
        decision = pipeline.merge_decision("run-1")

        self.assertFalse(attestation["public_write"])
        self.assertTrue(decision["eligible"])
        snapshot = pipeline.store.audits[-1]["decision"]["snapshot"]
        self.assertTrue(snapshot["owner_attestation_valid"])
        self.assertEqual(snapshot["owner_attestation_status"], "valid")

    def test_owner_attestation_requires_one_time_environment_gate(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""

        with self.assertRaisesRegex(PolicyError, "is disabled"):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_changed_activity_invalidates_owner_attestation(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")
        pipeline.github.activity_marker = "new-review"

        decision = pipeline.merge_decision("run-1")

        self.assertFalse(decision["eligible"])
        snapshot = pipeline.store.audits[-1]["decision"]["snapshot"]
        self.assertFalse(snapshot["owner_attestation_valid"])
        self.assertEqual(snapshot["owner_attestation_status"], "stale")

    def test_owner_attestation_rejects_external_author_and_review_rules(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        pipeline.github.pull_author = "external"
        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "authored externally"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        pipeline.github.pull_author = "alice"
        pipeline.github.rules_require_review = True
        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "independent review"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_owner_attestation_rejects_a_different_repository_head(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        pipeline.github.head_repository = "owner/other-repo"

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "cross-repository"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_owner_attestation_rejects_a_torn_github_read(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        pipeline.github.activity_head_sha = "f" * 40

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "different revisions"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_owner_attestation_does_not_override_changes_requested(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = "CHANGES_REQUESTED"

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "cannot override"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_owner_attestation_cannot_override_high_risk_paths(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        pipeline.github.files = [".github/workflows/quality.yml"]

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "high_risk_change"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_owner_attestation_waits_for_optional_ci_to_finish(self) -> None:
        pipeline = self.pipeline(owner_attestation=True)
        pipeline.github.review_decision = ""
        pipeline.github.optional_check_pending = True

        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}),
            self.assertRaisesRegex(PolicyError, "CI to finish: optional"),
        ):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")

        self.assertIsNone(pipeline.store.owner_attestation)

    def test_merge_executor_accepts_fresh_owner_attestation(self) -> None:
        pipeline = self.pipeline(auto_merge=True, owner_attestation=True)
        pipeline.github.review_decision = ""
        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_OWNER_ATTESTATION": "1"}):
            pipeline.attest_owner_review("run-1", reviewed_by="alice")
        decision = pipeline.merge_decision("run-1")

        with patch.dict(os.environ, {"REPOSTEWARD_ENABLE_MERGE": "1"}):
            result = pipeline.execute_merge(
                "run-1", decision_id=decision["audit"]["id"], reviewed_by="alice"
            )

        self.assertTrue(result["merged"])
        self.assertEqual(len(pipeline.github.merge_calls), 1)

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
        self.assertEqual(result["usage_summary"]["status"], "available")
        self.assertEqual(result["usage_summary"]["runs"], 0)
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
