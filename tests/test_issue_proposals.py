from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reposteward.config import load_config
from reposteward.github import ProjectIssueProposal
from reposteward.pipeline import Pipeline
from reposteward.policy import PolicyError
from reposteward.store import Store


class FakeProposalClient:
    def __init__(
        self, proposal: ProjectIssueProposal, *, login: str = "reviewer"
    ) -> None:
        self.proposal = proposal
        self.login = login
        self.created = 0
        self.converted = 0
        self.converted_item_id = ""
        self.similar = [
            {
                "number": 7,
                "title": "Related watcher behavior",
                "state": "open",
                "url": "https://example.test/owner/repo/issues/7",
            }
        ]

    def authenticated_login(self) -> str:
        return self.login

    def project_v2(
        self, owner: str, number: int, *, owner_type: str
    ) -> dict[str, object]:
        return {
            "id": self.proposal.project_id,
            "number": number,
            "url": self.proposal.project_url,
        }

    def add_project_issue_proposal(self, **_kwargs: object) -> ProjectIssueProposal:
        self.created += 1
        return self.proposal

    def project_issue_proposal(self, _item_id: str) -> ProjectIssueProposal:
        return self.proposal

    def project_issue_proposal_by_database_id(
        self, *, project_id: str, database_id: int
    ) -> ProjectIssueProposal:
        if project_id != self.proposal.project_id:
            raise AssertionError("unexpected project")
        if database_id != self.proposal.database_id:
            raise AssertionError("unexpected database ID")
        return self.proposal

    def similar_issues(
        self, _repository: str, _title: str, *, limit: int
    ) -> list[dict[str, object]]:
        return self.similar[:limit]

    def convert_project_issue_proposal(
        self, *, item_id: str, repository: str
    ) -> ProjectIssueProposal:
        self.converted += 1
        self.converted_item_id = item_id
        return ProjectIssueProposal(
            item_id=item_id,
            database_id=self.proposal.database_id,
            project_id=self.proposal.project_id,
            project_number=self.proposal.project_number,
            project_url=self.proposal.project_url,
            updated_at="2026-08-21T02:00:00Z",
            creator=self.proposal.creator,
            content_type="Issue",
            title=self.proposal.title,
            body=self.proposal.body,
            issue_number=42,
            issue_url="https://example.test/owner/repo/issues/42",
            repository=repository,
        )


def proposal(
    *, creator: str = "author", body: str = "## Summary\n\nDetails\n"
) -> ProjectIssueProposal:
    return ProjectIssueProposal(
        item_id="PVTI_example",
        database_id=123456,
        project_id="PVT_example",
        project_number=1,
        project_url="https://github.com/users/example/projects/1",
        updated_at="2026-08-21T01:00:00Z",
        creator=creator,
        content_type="DraftIssue",
        title="Watcher repeats one read",
        body=body,
    )


class IssueProposalTests(unittest.TestCase):
    def _pipeline(self, root: Path, *, login: str = "reviewer") -> Pipeline:
        path = root / "config.toml"
        path.write_text(
            f"""config_version = 1
[project]
state_dir = {str(root / "state")!r}
namespace_state = false
[github]
login = {login!r}
git_name = {login!r}
git_email = {f"{login}@example.com"!r}
[issue_review]
project_owner = "example"
project_number = 1
project_owner_type = "user"
require_distinct_reviewer = true
[repositories."owner/repo"]
""",
            encoding="utf-8",
        )
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = load_config(path)
        pipeline.store = Store(root / "state.sqlite3")
        return pipeline

    def test_stage_is_explicit_online_write_and_idempotent_locally(self) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory))
            draft = pipeline.store.create_issue_draft(
                "owner/repo", "Watcher repeats one read", "## Summary\n\nDetails\n"
            )
            client = FakeProposalClient(proposal(), login="reviewer")
            with (
                patch.dict("os.environ", {"REPOSTEWARD_ENABLE_ISSUE_STAGE": "1"}),
                patch.object(
                    pipeline,
                    "_authenticated_publication_client",
                    return_value=client,
                ),
            ):
                first = pipeline.stage_issue_proposal(
                    draft["id"], submitted_by="reviewer"
                )
                second = pipeline.stage_issue_proposal(
                    draft["id"], submitted_by="reviewer"
                )

        self.assertTrue(first["public_write"])
        self.assertFalse(second["public_write"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(client.created, 1)

    def test_security_like_content_never_reaches_online_staging(self) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory))
            draft = pipeline.store.create_issue_draft(
                "owner/repo",
                "Authentication bypass",
                "## Summary\n\nA security vulnerability.\n",
            )
            with (
                patch.dict("os.environ", {"REPOSTEWARD_ENABLE_ISSUE_STAGE": "1"}),
                self.assertRaisesRegex(PolicyError, "private reporting channel"),
            ):
                pipeline.stage_issue_proposal(draft["id"], submitted_by="reviewer")

    def test_review_digest_covers_latest_online_content_and_duplicates(self) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory))
            client = FakeProposalClient(proposal())
            pipeline.github = client

            first = pipeline.issue_proposal_review(
                "PVTI_example", repository="owner/repo"
            )
            client.proposal = replace(
                client.proposal,
                updated_at="2026-08-21T03:00:00Z",
                body="## Summary\n\nEdited online.\n",
            )
            second = pipeline.issue_proposal_review(
                "PVTI_example", repository="owner/repo"
            )

        self.assertNotEqual(first["review_digest"], second["review_digest"])
        self.assertTrue(first["duplicates_require_human_judgment"])
        self.assertTrue(first["eligible_for_promotion"])

    def test_review_accepts_the_numeric_item_id_from_a_project_browser_url(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory))
            client = FakeProposalClient(proposal())
            pipeline.github = client

            report = pipeline.issue_proposal_review(
                "https://github.com/users/example/projects/1?pane=issue&itemId=123456",
                repository="owner/repo",
            )

        self.assertEqual(report["item_id"], "PVTI_example")
        self.assertEqual(report["database_id"], 123456)

    def test_promotion_requires_a_second_reviewer_and_current_digest(self) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory))
            client = FakeProposalClient(proposal(creator="author"), login="reviewer")
            pipeline.github = client
            report = pipeline.issue_proposal_review(
                "PVTI_example", repository="owner/repo"
            )
            with (
                patch.dict("os.environ", {"REPOSTEWARD_ENABLE_ISSUE_PROMOTION": "1"}),
                patch.object(
                    pipeline,
                    "_authenticated_publication_client",
                    return_value=client,
                ),
            ):
                with self.assertRaisesRegex(PolicyError, "digest is stale"):
                    pipeline.promote_issue_proposal(
                        "PVTI_example",
                        repository="owner/repo",
                        reviewed_by="reviewer",
                        review_digest="0" * 64,
                        duplicates_reviewed=True,
                    )
                published = pipeline.promote_issue_proposal(
                    "https://github.com/users/example/projects/1?pane=issue&itemId=123456",
                    repository="owner/repo",
                    reviewed_by="reviewer",
                    review_digest=report["review_digest"],
                    duplicates_reviewed=True,
                )

        self.assertTrue(published["public_write"])
        self.assertEqual(published["issue_number"], 42)
        self.assertEqual(client.converted, 1)
        self.assertEqual(client.converted_item_id, "PVTI_example")

    def test_proposal_creator_cannot_self_approve_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            pipeline = self._pipeline(Path(directory), login="author")
            client = FakeProposalClient(proposal(creator="author"), login="author")
            pipeline.github = client
            report = pipeline.issue_proposal_review(
                "PVTI_example", repository="owner/repo"
            )
            with (
                patch.dict("os.environ", {"REPOSTEWARD_ENABLE_ISSUE_PROMOTION": "1"}),
                patch.object(
                    pipeline,
                    "_authenticated_publication_client",
                    return_value=client,
                ),
                self.assertRaisesRegex(PolicyError, "must be different"),
            ):
                pipeline.promote_issue_proposal(
                    "PVTI_example",
                    repository="owner/repo",
                    reviewed_by="author",
                    review_digest=report["review_digest"],
                    duplicates_reviewed=True,
                )


if __name__ == "__main__":
    unittest.main()
