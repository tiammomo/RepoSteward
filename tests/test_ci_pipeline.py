from __future__ import annotations

import unittest
from types import SimpleNamespace

from reposteward.config import RepositoryPolicy
from reposteward.github import PullRequest
from reposteward.pipeline import Pipeline


def _job(
    job_id: int,
    run_id: int,
    conclusion: str,
    *,
    check_run_id: int = 0,
    head_sha: str = "",
) -> dict:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": 1,
        "head_sha": head_sha,
        "workflow_name": "CI",
        "name": "quality",
        "status": "completed",
        "conclusion": conclusion,
        "url": f"https://example.test/jobs/{job_id}",
        "labels": ["ubuntu-latest"],
        "runner_group_name": "",
        "check_run_id": check_run_id,
        "steps": [
            {
                "number": 1,
                "name": "Run tests",
                "status": "completed",
                "conclusion": conclusion,
            }
        ],
    }


class _CiGitHub:
    def __init__(self, *, failed: bool = True) -> None:
        self.failed = failed
        self.log_calls: list[int] = []
        self.workflow_run_calls: list[dict] = []

    def pull_request(self, _repository: str, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://example.test/pulls/{number}",
            state="open",
            draft=False,
            head_branch="feature",
            head_sha="a" * 40,
            base_branch="main",
            base_sha="b" * 40,
        )

    def check_runs(self, _repository: str, _ref: str) -> tuple[dict, ...]:
        if not self.failed:
            return (
                {
                    "id": 501,
                    "name": "quality",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://github.test/actions/runs/101/job/1001",
                    "app_slug": "github-actions",
                },
            )
        return (
            {
                "id": 501,
                "name": "quality",
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.test/actions/runs/101/job/1001",
                "app_slug": "github-actions",
            },
        )

    def workflow_runs(self, _repository: str, **kwargs) -> tuple[dict, ...]:
        self.workflow_run_calls.append(kwargs)
        if kwargs.get("head_sha") == "b" * 40:
            return (
                {
                    "id": 201,
                    "name": "CI",
                    "head_sha": "b" * 40,
                    "created_at": "2026-08-21T10:00:00Z",
                },
            )
        if kwargs.get("branch") == "feature":
            return (
                {
                    "id": 301,
                    "name": "CI",
                    "head_sha": "c" * 40,
                    "created_at": "2026-08-20T10:00:00Z",
                    "pull_numbers": [12],
                },
                {
                    "id": 101,
                    "name": "CI",
                    "head_sha": "a" * 40,
                    "created_at": "2026-08-21T10:00:00Z",
                    "pull_numbers": [12],
                },
            )
        raise AssertionError(f"unexpected workflow run query: {kwargs}")

    def workflow_jobs(self, _repository: str, run_id: int) -> tuple[dict, ...]:
        if run_id == 101:
            return (
                _job(
                    1001,
                    101,
                    "failure",
                    check_run_id=501,
                    head_sha="a" * 40,
                ),
            )
        if run_id == 201:
            return (_job(2001, 201, "success", head_sha="b" * 40),)
        if run_id == 301:
            return (_job(3001, 301, "failure", head_sha="c" * 40),)
        raise AssertionError(f"unexpected run: {run_id}")

    def workflow_job_log(
        self, _repository: str, job_id: int, *, max_bytes: int
    ) -> dict:
        self.log_calls.append(job_id)
        self.assert_log_bound(max_bytes)
        return {
            "text": "FAILED tests/test_api.py::test_case AssertionError",
            "bytes_read": 52,
            "truncated": False,
        }

    @staticmethod
    def assert_log_bound(max_bytes: int) -> None:
        if max_bytes != 256 * 1024:
            raise AssertionError(f"unexpected log bound: {max_bytes}")


class _ManyFailuresGitHub(_CiGitHub):
    def check_runs(self, _repository: str, _ref: str) -> tuple[dict, ...]:
        return tuple(
            {
                "id": 500 + value,
                "name": f"quality-{value}",
                "status": "completed",
                "conclusion": "failure",
                "url": f"https://github.test/actions/runs/{100 + value}/job/{1000 + value}",
                "app_slug": "github-actions",
            }
            for value in range(30)
        )

    def workflow_runs(self, _repository: str, **kwargs) -> tuple[dict, ...]:
        self.workflow_run_calls.append(kwargs)
        return ()

    def workflow_jobs(self, _repository: str, run_id: int) -> tuple[dict, ...]:
        value = run_id - 100
        return (
            {
                **_job(
                    1000 + value,
                    run_id,
                    "failure",
                    check_run_id=500 + value,
                    head_sha="a" * 40,
                ),
                "name": f"quality-{value}",
            },
        )


class _ThirdPartyFailureGitHub(_CiGitHub):
    def check_runs(self, _repository: str, _ref: str) -> tuple[dict, ...]:
        return (
            {
                "id": 900,
                "name": "external-quality TOKEN=secret-value",
                "status": "completed",
                "conclusion": "failure",
                "url": "https://ci.example.test/build/900?token=secret-value",
                "app_slug": "external-ci",
            },
        )


class CiFailurePipelineTests(unittest.TestCase):
    def pipeline(self, github: _CiGitHub) -> Pipeline:
        pipeline = object.__new__(Pipeline)
        pipeline.config = SimpleNamespace(
            repositories={"owner/repo": RepositoryPolicy(name="owner/repo")}
        )
        pipeline.github = github
        return pipeline

    def test_current_failure_is_compared_with_base_and_same_pr_history(self) -> None:
        github = _CiGitHub()
        result = self.pipeline(github).ci_failure_analysis("owner/repo", 12)

        self.assertTrue(result["complete"])
        self.assertEqual(result["failures"][0]["classification"], "introduced")
        self.assertEqual(result["failures"][0]["same_pr_history_run_ids"], [301])
        self.assertEqual(github.log_calls, [1001, 3001])
        self.assertEqual(
            result["comparison"],
            {
                "current_head_run_ids": [101],
                "same_pr_history_run_ids": [301],
                "base_run_ids": [201],
            },
        )
        self.assertFalse(result["harness_invoked"])
        self.assertFalse(result["workflow_rerun"])
        self.assertFalse(result["raw_logs_persisted"])
        self.assertFalse(result["local_write"])
        self.assertFalse(result["public_write"])

    def test_successful_checks_skip_workflow_and_log_queries(self) -> None:
        github = _CiGitHub(failed=False)
        result = self.pipeline(github).ci_failure_analysis("owner/repo", 12)

        self.assertEqual(result["failures"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(github.workflow_run_calls, [])
        self.assertEqual(github.log_calls, [])

    def test_current_log_downloads_have_a_global_bound(self) -> None:
        github = _ManyFailuresGitHub()
        result = self.pipeline(github).ci_failure_analysis("owner/repo", 12)

        self.assertEqual(len(result["failures"]), 30)
        self.assertEqual(len(github.log_calls), 24)
        self.assertFalse(result["complete"])
        self.assertEqual(result["failures"][-1]["classification"], "unknown")
        self.assertEqual(result["failures"][-1]["log_error"], "current_log_limit")

    def test_third_party_failure_is_unknown_without_actions_queries(self) -> None:
        github = _ThirdPartyFailureGitHub()
        result = self.pipeline(github).ci_failure_analysis("owner/repo", 12)

        self.assertEqual(result["failures"][0]["classification"], "unknown")
        self.assertNotIn("secret-value", str(result))
        self.assertFalse(result["complete"])
        self.assertEqual(github.workflow_run_calls, [])
        self.assertEqual(github.log_calls, [])


if __name__ == "__main__":
    unittest.main()
