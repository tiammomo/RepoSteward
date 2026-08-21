from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicationWorkflowTests(unittest.TestCase):
    def test_issue_workflows_are_manual_and_do_not_use_ambient_write_token(
        self,
    ) -> None:
        for name in ("issue-proposal-review.yml", "issue-proposal-promote.yml"):
            content = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )

            self.assertIn("workflow_dispatch:", content)
            self.assertNotIn("pull_request_target", content)
            self.assertNotIn("issue_comment:", content)
            self.assertNotIn("github.token", content)
            self.assertIn("permissions:\n  contents: read", content)
            self.assertIn("github.event.repository.default_branch", content)
            self.assertIn("persist-credentials: false", content)
            self.assertIn("$RUNNER_TEMP/reposteward/state", content)
            self.assertNotIn("${{ runner.temp }}", content)
            self.assertNotIn(
                "    env:\n      GH_TOKEN: ${{ secrets.REPOSTEWARD_GITHUB_", content
            )

    def test_issue_promotion_requires_environment_digest_and_duplicate_review(
        self,
    ) -> None:
        content = (
            ROOT / ".github" / "workflows" / "issue-proposal-promote.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("environment: reposteward-issue-publishing", content)
        self.assertIn("group: issue-promote-${{ inputs.repository }}", content)
        self.assertIn("review_digest:", content)
        self.assertIn("duplicates_reviewed:", content)
        self.assertIn('test "$GITHUB_ACTOR" = "$PUBLISHER_LOGIN"', content)
        self.assertIn('REPOSTEWARD_ENABLE_ISSUE_PROMOTION: "1"', content)


if __name__ == "__main__":
    unittest.main()
