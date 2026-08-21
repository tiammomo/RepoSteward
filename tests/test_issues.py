from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from reposteward.issues import (
    attach_proposal_marker,
    issue_security_signals,
    proposal_body,
    read_details,
    render_issue_body,
    validate_issue_title,
)


class IssueDraftTests(unittest.TestCase):
    def test_english_draft_contains_structured_sections_and_checklist(self) -> None:
        body = render_issue_body(
            summary="The watcher reads the same file repeatedly.",
            actual="A polling pass performs N identical reads.",
            expected="Each trajectory is read once per pass.",
            reproduction="1. Start the watcher.\n2. Update one trajectory.",
            acceptance=("One read per trajectory", "Existing tests pass"),
        )

        self.assertIn("## Summary", body)
        self.assertIn("## Steps to reproduce", body)
        self.assertIn("- [ ] One read per trajectory", body)

    def test_chinese_draft_uses_chinese_headings(self) -> None:
        body = render_issue_body(
            summary="轮询中存在重复读取。",
            actual="单轮会读取多次。",
            expected="单轮只读取一次。",
            language="zh",
        )

        self.assertIn("## 问题概述", body)
        self.assertIn("## 实际结果", body)
        self.assertNotIn("Steps to reproduce", body)

    def test_title_is_normalized_and_bounded(self) -> None:
        self.assertEqual(validate_issue_title("  fix   watcher  "), "fix watcher")
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate_issue_title("x" * 201)

    def test_details_file_with_a_credential_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "failure.log"
            path.write_text(
                "GITHUB_TOKEN=example-secret-value",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "credential"):
                read_details(path)

    def test_security_reports_are_blocked_before_online_staging(self) -> None:
        signals = issue_security_signals(
            "Authentication bypass",
            "The report demonstrates a command injection vulnerability.",
        )

        self.assertIn("potential security vulnerability", signals)

    def test_online_marker_cannot_silently_redirect_the_repository(self) -> None:
        body = attach_proposal_marker(
            "## Summary\n\nDetails\n",
            repository="owner/repo",
            draft_id="a" * 32,
        )

        self.assertEqual(
            proposal_body(body, repository="owner/repo"),
            "## Summary\n\nDetails\n",
        )
        with self.assertRaisesRegex(ValueError, "different repository"):
            proposal_body(body, repository="other/repo")


if __name__ == "__main__":
    unittest.main()
