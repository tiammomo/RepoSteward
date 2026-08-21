from __future__ import annotations

import unittest

from reposteward.ci import (
    classify_failure,
    fingerprint_failure,
    redact_ci_log,
)


def _job(
    *,
    job_id: int,
    run_id: int,
    conclusion: str,
    fingerprint: str = "",
) -> dict:
    return {
        "id": job_id,
        "run_id": run_id,
        "workflow_name": "CI",
        "name": "quality",
        "labels": ["ubuntu-latest"],
        "conclusion": conclusion,
        "fingerprint": fingerprint,
        "steps": [],
    }


class CiFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_output_is_redacted_and_bounded(self) -> None:
        job = {
            **_job(job_id=1, run_id=10, conclusion="failure"),
            "steps": [
                {"name": "Run tests", "conclusion": "failure"},
            ],
        }
        first = fingerprint_failure(
            job,
            log=(
                "2026-08-21T10:11:12Z GITHUB_TOKEN=ghp_abcdefghijklmnop\n"
                "2026-08-21T10:11:13Z FAILED tests/test_api.py::test_case:42 "
                "AssertionError after 1.23s\n"
                "https://example.test/log?sig=private-signature\n"
            ),
            log_bytes=200,
            log_truncated=False,
        )
        second = fingerprint_failure(
            job,
            log=(
                "2026-08-22T01:02:03Z GITHUB_TOKEN=github_pat_abcdefghijklmnop\n"
                "2026-08-22T01:02:04Z FAILED tests/test_api.py::test_case:99 "
                "AssertionError after 9.87s\n"
            ),
            log_bytes=190,
            log_truncated=True,
        )

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(first["fingerprint"])
        rendered = str(first)
        self.assertNotIn("ghp_", rendered)
        self.assertNotIn("private-signature", rendered)
        self.assertLessEqual(len(first["log_excerpt"]), 12)

    def test_redaction_covers_assignments_bearer_tokens_and_signed_queries(
        self,
    ) -> None:
        value = redact_ci_log(
            "password=hunter2 Authorization: Bearer abcdefghijklmnop "
            "https://user:pass@example.test/x?token=secret-value&ok=1\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY-----"
        )

        self.assertNotIn("hunter2", value)
        self.assertNotIn("abcdefghijklmnop", value)
        self.assertNotIn("secret-value", value)
        self.assertNotIn("private-material", value)
        self.assertNotIn("user:pass", value)

    def test_infrastructure_signature_is_reported_without_model_inference(self) -> None:
        result = fingerprint_failure(
            _job(job_id=1, run_id=10, conclusion="failure"),
            log="Error: The runner has lost communication with the server",
            log_bytes=56,
            log_truncated=False,
        )

        classified = classify_failure(
            {"job": _job(job_id=1, run_id=10, conclusion="failure"), **result},
            same_head=[],
            pull_history=[],
            baseline=[],
            evidence_complete=True,
        )

        self.assertEqual(classified["classification"], "infrastructure")
        self.assertEqual(classified["confidence"], "high")


class CiClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current_job = _job(job_id=1, run_id=10, conclusion="failure")
        self.current = {
            "job": self.current_job,
            "fingerprint": "fingerprint-a",
            "infrastructure_signals": [],
        }

    def test_same_head_success_is_flaky(self) -> None:
        result = classify_failure(
            self.current,
            same_head=[_job(job_id=2, run_id=10, conclusion="success")],
            pull_history=[],
            baseline=[],
            evidence_complete=True,
        )

        self.assertEqual(result["classification"], "flaky")
        self.assertEqual(result["compared_run_ids"], [10])

    def test_matching_base_failure_is_inherited(self) -> None:
        result = classify_failure(
            self.current,
            same_head=[],
            pull_history=[],
            baseline=[
                _job(
                    job_id=3,
                    run_id=20,
                    conclusion="failure",
                    fingerprint="fingerprint-a",
                )
            ],
            evidence_complete=True,
        )

        self.assertEqual(result["classification"], "inherited")
        self.assertEqual(result["compared_run_ids"], [20])

    def test_successful_base_is_introduced_only_with_complete_evidence(self) -> None:
        baseline = [_job(job_id=4, run_id=30, conclusion="success")]

        complete = classify_failure(
            self.current,
            same_head=[],
            pull_history=[],
            baseline=baseline,
            evidence_complete=True,
        )
        incomplete = classify_failure(
            self.current,
            same_head=[],
            pull_history=[],
            baseline=baseline,
            evidence_complete=False,
        )

        self.assertEqual(complete["classification"], "introduced")
        self.assertEqual(incomplete["classification"], "unknown")

    def test_mixed_base_success_and_different_failure_is_unknown(self) -> None:
        result = classify_failure(
            self.current,
            same_head=[],
            pull_history=[],
            baseline=[
                _job(job_id=4, run_id=30, conclusion="success"),
                _job(
                    job_id=5,
                    run_id=31,
                    conclusion="failure",
                    fingerprint="fingerprint-b",
                ),
            ],
            evidence_complete=True,
        )

        self.assertEqual(result["classification"], "unknown")

    def test_history_is_evidence_but_does_not_replace_a_baseline(self) -> None:
        result = classify_failure(
            self.current,
            same_head=[],
            pull_history=[
                _job(
                    job_id=5,
                    run_id=40,
                    conclusion="failure",
                    fingerprint="fingerprint-a",
                )
            ],
            baseline=[],
            evidence_complete=True,
        )

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["same_pr_history_run_ids"], [40])


if __name__ == "__main__":
    unittest.main()
