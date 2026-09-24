from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from reposteward.core.config import ConfigError, RepositoryPolicy, load_config
from reposteward.core.models import VerificationResult
from reposteward.workflows.pipeline import Pipeline
from reposteward.workflows.policy import PolicyError, enforce_change_policy
from reposteward.workflows.review import compact_run


class WorkflowGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "alice@example.com")
        self.git("config", "user.name", "Alice")
        (self.worktree / "README.md").write_text("example\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.base = self.git("rev-parse", "HEAD")
        self.path = ".github/workflows/ci.yml"
        self.workflow = self.worktree / self.path
        self.workflow.parent.mkdir(parents=True)
        self.workflow.write_text("name: CI\non: push\n")
        self.user = self.root / "user.toml"
        # Keep the project configuration outside the measured diff.
        self.project = self.root / "project.toml"
        self.project.write_text('config_version = 1\n[repositories."owner/repo"]\n')
        self.write_user()
        self.config = load_config(self.project, user_path=self.user)
        self.policy = RepositoryPolicy(name="owner/repo")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def grant(self) -> dict:
        return {
            "repository": "owner/repo",
            "issue": 42,
            "base_commit": self.base,
            "reviewed_by": "alice",
            "files": {
                self.path: hashlib.sha256(self.workflow.read_bytes()).hexdigest()
            },
        }

    def write_user(self, grant: dict | None = None) -> None:
        text = 'config_version = 1\n[github]\nlogin = "alice"\n'
        if grant is not None:
            text += "[[safety.workflow_grants]]\n"
            for key, value in grant.items():
                if key != "files":
                    text += f"{key} = {json.dumps(value)}\n"
            text += "[safety.workflow_grants.files]\n"
            for key, value in grant["files"].items():
                text += f"{json.dumps(key)} = {json.dumps(value)}\n"
        self.user.write_text(text)

    def check(self, **kwargs):
        return enforce_change_policy(
            self.worktree,
            VerificationResult(passed=True, commands=()),
            self.policy,
            self.config,
            base_ref=self.base,
            issue_number=kwargs.get("issue", 42),
        )

    def test_default_deny_and_exact_user_review(self) -> None:
        with self.assertRaisesRegex(PolicyError, "exact trusted"):
            self.check()
        self.write_user(self.grant())
        summary = self.check()
        self.assertEqual(summary.files, (self.path,))
        self.assertEqual(summary.workflow_review["files"], self.grant()["files"])
        self.assertEqual(len(summary.workflow_review["digest"]), 64)
        packet = compact_run({"details": {"workflow_review": summary.workflow_review}})
        self.assertEqual(
            packet["change"]["workflow_review_digest"],
            summary.workflow_review["digest"],
        )

    def test_project_cannot_grant_or_override_user_review(self) -> None:
        self.write_user(self.grant())
        self.project.write_text(self.user.read_text())
        self.write_user()
        self.config = load_config(self.project, user_path=self.user)
        self.assertEqual(self.config.safety.workflow_grants, ())
        with self.assertRaises(PolicyError):
            self.check()
        self.config = load_config(self.project)
        with self.assertRaises(PolicyError):
            self.check()
        self.write_user(self.grant())
        # Even malformed project grants cannot replace or expand user authority.
        self.project.write_text(
            'config_version = 1\n[safety]\nworkflow_grants = "unsafe"\n'
        )
        self.config = load_config(self.project, user_path=self.user)
        self.assertIsNotNone(self.check().workflow_review)

    def test_identity_base_issue_host_and_exact_file_set_mismatches(self) -> None:
        for field, value in (
            ("repository", "other/repo"),
            ("issue", 43),
            ("base_commit", "0" * 40),
            ("reviewed_by", "bob"),
            ("api_url", "https://enterprise.example/api/v3"),
        ):
            with self.subTest(field=field):
                grant = self.grant()
                grant[field] = value
                self.write_user(grant)
                with self.assertRaises(PolicyError):
                    self.check()
        grant = self.grant()
        grant["files"][".github/workflows/extra.yml"] = "0" * 64
        self.write_user(grant)
        with self.assertRaises(PolicyError):
            self.check()
        self.write_user(self.grant())
        with self.assertRaises(PolicyError):
            self.check(issue=43)
        self.workflow.write_text("name: changed\n")
        with self.assertRaises(PolicyError):
            self.check()

    def test_invalid_grant_paths_hashes_and_identity_fail_config_loading(self) -> None:
        for path in (
            ".github/workflows/*.yml",
            ".github/workflows/../ci.yml",
            "README.md",
            ".github/workflows/ci.txt",
            "/.github/workflows/ci.yml",
        ):
            with self.subTest(path=path):
                grant = self.grant()
                grant["files"] = {path: "0" * 64}
                self.write_user(grant)
                with self.assertRaises(ConfigError):
                    load_config(self.project, user_path=self.user)
        for field, value in (
            ("issue", True),
            ("base_commit", "main"),
            ("reviewed_by", "*"),
        ):
            grant = self.grant()
            grant[field] = value
            self.write_user(grant)
            with self.assertRaises(ConfigError):
                load_config(self.project, user_path=self.user)

    def test_symlinks_deletion_and_unrelated_restrictions_remain_blocked(self) -> None:
        self.git("add", ".")
        self.git("commit", "-qm", "ci(test): initial workflow")
        self.base = self.git("rev-parse", "HEAD")
        self.workflow.write_text("name: reviewed CI\non: push\n")
        self.write_user(self.grant())
        saved = self.workflow.read_bytes()
        self.workflow.unlink()
        with self.assertRaisesRegex(PolicyError, "regular YAML"):
            self.check()
        target = self.root / "external.yml"
        target.write_bytes(saved)
        self.workflow.symlink_to(target)
        with self.assertRaisesRegex(PolicyError, "symlink"):
            self.check()
        self.workflow.unlink()
        self.workflow.write_bytes(saved)
        (self.worktree / ".env").write_text("EXAMPLE=1\n")
        with self.assertRaisesRegex(PolicyError, "forbidden paths"):
            self.check()
        (self.worktree / ".env").unlink()
        self.config = replace(
            self.config,
            safety=replace(
                self.config.safety, forbidden_paths=(".github/workflows/", "ci.yml")
            ),
        )
        with self.assertRaisesRegex(PolicyError, "forbidden paths"):
            self.check()

    def publication(self):
        self.write_user(self.grant())
        evidence = self.check().workflow_review
        self.git("add", ".")
        self.git("commit", "-qm", "ci(test): reviewed workflow")
        details = {
            "worktree": str(self.worktree),
            "commit_sha": self.git("rev-parse", "HEAD"),
            "base_branch": "main",
            "base_commit": self.base,
            "changed_files": [self.path],
            "workflow_review": evidence,
            "verification": {"passed": True},
        }
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = replace(self.config, repositories={"owner/repo": self.policy})
        client = Mock()
        client.branch_head_sha.return_value = self.base
        return pipeline, client, details

    def test_publication_refreshes_revocation_content_and_base(self) -> None:
        pipeline, client, details = self.publication()
        pipeline._validate_workflow_publication(client, "owner/repo", 42, details)
        self.write_user()
        with self.assertRaisesRegex(PolicyError, "exact trusted"):
            pipeline._validate_workflow_publication(client, "owner/repo", 42, details)
        self.write_user(self.grant())
        client.branch_head_sha.return_value = "0" * 40
        with self.assertRaisesRegex(PolicyError, "base changed"):
            pipeline._validate_workflow_publication(client, "owner/repo", 42, details)
        client.branch_head_sha.return_value = self.base
        details["workflow_review"] = None
        with self.assertRaisesRegex(PolicyError, "verified evidence"):
            pipeline._validate_workflow_publication(client, "owner/repo", 42, details)

    def submit_fixture(self):
        from test_submission_capacity import _SubmissionGitHub, _SubmissionStore

        pipeline, _, details = self.publication()
        self.git("checkout", "-qb", "alice/feat/workflow")
        details.update(
            branch="alice/feat/workflow",
            agent_result={
                "summary": "Archive the benchmark report.",
                "pr_title": "ci(benchmark): archive reports",
                "implementation_notes": "Exact reviewed workflow.",
                "verification_commands": ["python -m unittest"],
                "risks": [],
            },
        )
        policy = replace(
            self.policy, mode="maintainer", submission_strategy="same-repository"
        )
        pipeline.config = replace(pipeline.config, repositories={"owner/repo": policy})
        pipeline.store = _SubmissionStore(details)
        pipeline.workspaces = Mock()
        pipeline.gate_status = Mock(return_value={"submission_ready": True})
        client = _SubmissionGitHub()
        client.new_head = details["commit_sha"]
        client.branch_head_sha = lambda repo, branch: (
            self.base if branch == "main" else client.remote_head
        )
        pipeline.workspaces.push.side_effect = lambda *a, **kw: client.mark_pushed(
            client.new_head
        )
        return pipeline, client

    def submit(self, pipeline, client):
        with (
            patch.dict(os.environ, {"REPOSTEWARD_ENABLE_SUBMIT": "1"}),
            patch(
                "reposteward.workflows.pipeline.resolve_token",
                return_value="fixture-token",
            ),
            patch("reposteward.workflows.pipeline.GitHubClient", return_value=client),
        ):
            return pipeline.submit("owner/repo", 42, reviewed_by="alice")

    def test_exact_review_reaches_normal_audited_submission(self) -> None:
        pipeline, client = self.submit_fixture()
        result = self.submit(pipeline, client)
        self.assertEqual(result["pr_number"], 99)
        self.assertEqual(client.create_calls, 1)
        pipeline.workspaces.push.assert_called_once()
        self.assertTrue(pipeline.store.details["workflow_review"]["digest"])
        completed = [r for r in pipeline.store.publication if r["stage"] == "completed"]
        self.assertEqual({r["action"] for r in completed}, {"push", "create"})

    def test_revoked_review_blocks_submit_without_public_writes(self) -> None:
        pipeline, client = self.submit_fixture()
        self.write_user()
        with self.assertRaisesRegex(PolicyError, "exact trusted"):
            self.submit(pipeline, client)
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.repository_calls, 0)
        pipeline.workspaces.push.assert_not_called()
        self.assertEqual(pipeline.store.publication, [])

    def test_revocation_after_push_preserves_audit_and_blocks_pr_creation(self) -> None:
        pipeline, client = self.submit_fixture()

        def push(*args, **kwargs):
            client.mark_pushed(client.new_head)
            self.write_user()

        pipeline.workspaces.push.side_effect = push
        with self.assertRaisesRegex(PolicyError, "exact trusted"):
            self.submit(pipeline, client)
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.remote_head, client.new_head)
        completed = [r for r in pipeline.store.publication if r["stage"] == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["action"], "push")
        self.assertTrue(completed[0]["payload"]["public_write"])
        self.write_user(self.grant())
        self.assertEqual(self.submit(pipeline, client)["pr_number"], 99)
        pipeline.workspaces.push.assert_called_once()

    def test_rename_requires_review_of_removed_path_and_is_refused(self) -> None:
        self.git("add", ".")
        self.git("commit", "-qm", "ci(test): initial workflow")
        self.base = self.git("rev-parse", "HEAD")
        new = self.workflow.with_name("renamed.yml")
        self.workflow.rename(new)
        self.git("add", "-A")
        self.path = ".github/workflows/renamed.yml"
        self.workflow = new
        self.write_user(self.grant())
        with self.assertRaisesRegex(PolicyError, "regular YAML"):
            self.check()


if __name__ == "__main__":
    unittest.main()
