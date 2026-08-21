from __future__ import annotations

import unittest
from types import SimpleNamespace

from reposteward.config import RepositoryPolicy
from reposteward.context import repository_policy_digest
from reposteward.pipeline import Pipeline


class StubStore:
    def __init__(self, policy: RepositoryPolicy) -> None:
        self.policy = policy
        self.audits: list[dict] = []

    def run(self, run_id: str) -> dict:
        return {
            "id": run_id,
            "repository": "owner/repo",
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
        return {"id": f"audit-{len(self.audits)}"}


class StubGitHub:
    def pull_request_merge_snapshot(self, repository: str, number: int) -> dict:
        return {
            "repository": repository,
            "pull_number": number,
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "state": "OPEN",
            "draft": False,
            "mergeable": "MERGEABLE",
            "review_decision": "APPROVED",
            "unresolved_conversations": 0,
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
        }


class MergePipelineTests(unittest.TestCase):
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
