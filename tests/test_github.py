from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from reposteward.config import GitHubConfig
from reposteward.github import GitHubClient, GitHubError, resolve_authentication


class StubGitHubClient(GitHubClient):
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        super().__init__(GitHubConfig(), token="test-token")
        self.comments = comments

    def _request(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
        return self.comments, None


class GitHubApprovalTests(unittest.TestCase):
    def test_maintainer_command_at_end_is_accepted(self) -> None:
        client = StubGitHubClient(
            [{"author_association": "MEMBER", "body": "Looks reasonable.\n\nlgtm"}]
        )

        self.assertTrue(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER", "MEMBER"))
        )

    def test_non_maintainer_command_is_rejected(self) -> None:
        client = StubGitHubClient([{"author_association": "NONE", "body": "lgtm"}])

        self.assertFalse(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER", "MEMBER"))
        )

    def test_command_as_substring_is_rejected(self) -> None:
        client = StubGitHubClient(
            [{"author_association": "OWNER", "body": "not-lgtm-yet"}]
        )

        self.assertFalse(
            client.has_maintainer_approval("owner/repo", 1, "lgtm", ("OWNER",))
        )


class GitHubAuthenticationTests(unittest.TestCase):
    @patch("reposteward.github.shutil.which", return_value="/usr/bin/gh")
    @patch("reposteward.github.subprocess.run")
    def test_gh_oauth_is_used_when_environment_token_is_absent(
        self, run: Any, _which: Any
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "oauth-token\n"

        with patch.dict("os.environ", {}, clear=True):
            token, source = resolve_authentication(GitHubConfig())

        self.assertEqual(token, "oauth-token")
        self.assertEqual(source, "gh OAuth")
        run.assert_called_once()


class GitHubCompetingWorkTests(unittest.TestCase):
    def test_claim_comment_and_linked_pull_request_are_blockers(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")

        def request(method: str, path: str, **kwargs: Any) -> tuple[Any, Any]:
            if path.endswith("/comments"):
                return [
                    {
                        "user": {"login": "other"},
                        "body": "I can take this and open a PR.",
                        "html_url": "https://example.test/comment",
                    }
                ], None
            return [
                {
                    "number": 9,
                    "title": "fix: existing work",
                    "body": "Fixes #7",
                    "html_url": "https://example.test/pr",
                    "user": {"login": "another"},
                    "head": {"repo": {"owner": {"login": "another"}}},
                }
            ], None

        with patch.object(client, "_request", side_effect=request):
            conflicts = client.competing_work("owner/repo", 7, own_login="tiammomo")

        self.assertEqual(
            {value.kind for value in conflicts},
            {"claim_comment", "open_pull_request"},
        )


class GitHubIssueSearchTests(unittest.TestCase):
    def test_similar_issue_search_is_read_only_and_compact(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {
            "items": [
                {
                    "number": 7,
                    "title": "Watcher repeatedly reads the same trajectory",
                    "state": "open",
                    "html_url": "https://example.test/issues/7",
                    "body": "large body that should not be returned",
                }
            ]
        }

        with patch.object(client, "_request", return_value=(payload, None)) as request:
            issues = client.similar_issues(
                "owner/repo", "Watcher repeatedly reads the same trajectory"
            )

        self.assertEqual(issues[0]["number"], 7)
        self.assertNotIn("body", issues[0])
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("GET", "/search/issues"))
        self.assertIn("repo:owner/repo is:issue", kwargs["query"]["q"])


class GitHubProjectIssueTests(unittest.TestCase):
    def test_project_draft_item_is_parsed_from_graphql(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {
            "data": {
                "node": {
                    "id": "PVTI_example",
                    "fullDatabaseId": "123456",
                    "updatedAt": "2026-08-21T00:00:00Z",
                    "creator": {"login": "author"},
                    "project": {
                        "id": "PVT_example",
                        "number": 1,
                        "url": "https://example.test/project/1",
                    },
                    "content": {
                        "__typename": "DraftIssue",
                        "title": "Proposal title",
                        "body": "Proposal body",
                    },
                }
            }
        }

        with patch.object(client, "_request", return_value=(payload, None)):
            result = client.project_issue_proposal("PVTI_example")

        self.assertEqual(result.creator, "author")
        self.assertEqual(result.database_id, 123456)
        self.assertEqual(result.content_type, "DraftIssue")
        self.assertEqual(result.project_id, "PVT_example")

    def test_numeric_project_item_id_is_resolved_from_active_project_items(
        self,
    ) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {
            "data": {
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_example",
                                "fullDatabaseId": "123456",
                                "updatedAt": "2026-08-21T00:00:00Z",
                                "creator": {"login": "author"},
                                "project": {
                                    "id": "PVT_example",
                                    "number": 1,
                                    "url": "https://example.test/project/1",
                                },
                                "content": {
                                    "__typename": "DraftIssue",
                                    "title": "Proposal title",
                                    "body": "Proposal body",
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

        with patch.object(client, "_request", return_value=(payload, None)):
            result = client.project_issue_proposal_by_database_id(
                project_id="PVT_example", database_id=123456
            )

        self.assertEqual(result.item_id, "PVTI_example")

    def test_graphql_errors_fail_closed(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {"errors": [{"message": "Projects permission is required"}]}

        with (
            patch.object(client, "_request", return_value=(payload, None)),
            self.assertRaisesRegex(GitHubError, "Projects permission is required"),
        ):
            client.project_v2("owner", 1, owner_type="user")


class GitHubPullRequestTests(unittest.TestCase):
    def test_merge_snapshot_paginates_and_normalizes_required_checks(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")

        def page(
            *,
            files: list[dict[str, Any]],
            files_next: bool,
            file_cursor: str,
            threads: list[dict[str, Any]],
            checks: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
                "repository": {
                    "pullRequest": {
                        "number": 12,
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "reviewDecision": "APPROVED",
                        "headRefOid": "a" * 40,
                        "baseRefOid": "b" * 40,
                        "additions": 10,
                        "deletions": 2,
                        "changedFiles": 2,
                        "mergeCommit": {"oid": "f" * 40},
                        "files": {
                            "totalCount": 2,
                            "nodes": files,
                            "pageInfo": {
                                "hasNextPage": files_next,
                                "endCursor": file_cursor,
                            },
                        },
                        "reviewThreads": {
                            "totalCount": 1,
                            "nodes": threads,
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "thread-end",
                            },
                        },
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "totalCount": 2,
                                                "nodes": checks,
                                                "pageInfo": {
                                                    "hasNextPage": False,
                                                    "endCursor": "check-end",
                                                },
                                            }
                                        }
                                    }
                                }
                            ]
                        },
                    }
                }
            }

        first = page(
            files=[{"path": "src/b.py"}],
            files_next=True,
            file_cursor="file-1",
            threads=[{"id": "thread-1", "isResolved": False}],
            checks=[
                {
                    "__typename": "CheckRun",
                    "name": "quality",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "isRequired": True,
                },
                {
                    "__typename": "StatusContext",
                    "context": "legacy-ci",
                    "state": "PENDING",
                    "isRequired": False,
                },
            ],
        )
        second = page(
            files=[{"path": "src/a.py"}],
            files_next=False,
            file_cursor="file-end",
            threads=[],
            checks=[],
        )

        with patch.object(client, "_graphql", side_effect=[first, second]) as graphql:
            snapshot = client.pull_request_merge_snapshot("Owner/Repo", 12)

        self.assertEqual(snapshot["files"], ["src/a.py", "src/b.py"])
        self.assertEqual(snapshot["unresolved_conversations"], 1)
        self.assertEqual(len(snapshot["conversation_digest"]), 64)
        self.assertEqual(snapshot["checks"][0]["name"], "legacy-ci")
        self.assertEqual(snapshot["checks"][0]["status"], "pending")
        self.assertTrue(snapshot["checks"][1]["required"])
        self.assertEqual(snapshot["merge_commit_sha"], "f" * 40)
        self.assertEqual(graphql.call_args_list[1].args[1]["files"], "file-1")
        self.assertEqual(graphql.call_args_list[1].args[1]["threads"], "thread-end")

    def test_merge_request_is_pinned_to_one_head_and_method(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        payload = {"merged": True, "sha": "c" * 40, "message": "merged"}

        with patch.object(client, "_request", return_value=(payload, None)) as request:
            result = client.merge_pull_request(
                "owner/repo", 12, head_sha="a" * 40, method="squash"
            )

        self.assertTrue(result["merged"])
        request.assert_called_once_with(
            "PUT",
            "/repos/owner/repo/pulls/12/merge",
            data={"sha": "a" * 40, "merge_method": "squash"},
        )

    def test_merge_snapshot_rejects_incomplete_connection_data(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        malformed = {
            "repository": {
                "pullRequest": {
                    "number": 12,
                    "files": {
                        "totalCount": 1,
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "reviewThreads": {
                        "totalCount": 0,
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "commits": {"nodes": []},
                }
            }
        }

        with (
            patch.object(client, "_graphql", return_value=malformed),
            self.assertRaisesRegex(GitHubError, "incomplete: files"),
        ):
            client.pull_request_merge_snapshot("owner/repo", 12)

    def test_pull_request_activity_follows_every_rest_page(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        pull = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "open",
            "draft": True,
            "updated_at": "2026-08-20T00:00:00Z",
            "head": {"sha": "a" * 40},
            "base": {"ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
            "merged_at": None,
        }
        first_page = [
            {
                "id": value,
                "user": {"login": "reviewer"},
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": f"https://example.test/comment/{value}",
                "body": "review",
            }
            for value in range(1, 101)
        ]
        second_page = [
            {
                "id": 101,
                "user": {"login": "reviewer"},
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/comment/101",
                "body": "last review",
            }
        ]
        next_page = SimpleNamespace(
            headers={
                "Link": '<https://api.test/comments?page=2>; rel="next", '
                '<https://api.test/comments?page=2>; rel="last"'
            }
        )
        last_page = SimpleNamespace(headers={})

        with patch.object(
            client,
            "_request",
            side_effect=[
                (pull, None),
                (first_page, next_page),
                (second_page, last_page),
                ([], last_page),
                ([], last_page),
                ({"check_runs": []}, last_page),
            ],
        ) as request:
            activity = client.pull_request_activity("owner/repo", 12)

        self.assertEqual(len(activity["comments"]), 101)
        self.assertEqual(request.call_args_list[1].kwargs["query"]["page"], 1)
        self.assertEqual(request.call_args_list[2].kwargs["query"]["page"], 2)

    def test_pull_request_activity_returns_compact_structured_state(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        pull = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "open",
            "draft": True,
            "updated_at": "2026-08-20T00:00:00Z",
            "head": {"sha": "a" * 40},
            "base": {"ref": "main"},
            "mergeable": True,
            "mergeable_state": "clean",
            "merged_at": None,
        }
        comments = [
            {
                "id": 21,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/comment/21",
                "body": "Please add a regression test.",
            }
        ]
        reviews = [
            {
                "id": 31,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/review/31",
                "body": "One requested change.",
            }
        ]
        review_comments = [
            {
                "id": 35,
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
                "created_at": "2026-08-20T00:00:00Z",
                "updated_at": "2026-08-20T00:00:00Z",
                "html_url": "https://example.test/review-comment/35",
                "path": "src/example.py",
                "line": 42,
                "body": "Handle this branch explicitly.",
            }
        ]
        checks = {
            "check_runs": [
                {
                    "id": 41,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://example.test/check/41",
                }
            ]
        }

        with patch.object(
            client,
            "_request",
            side_effect=[
                (pull, None),
                (comments, None),
                (reviews, None),
                (review_comments, None),
                (checks, None),
            ],
        ):
            activity = client.pull_request_activity("owner/repo", 12)

        self.assertEqual(activity["pull_request"]["head_sha"], "a" * 40)
        self.assertEqual(activity["comments"][0]["id"], 21)
        self.assertEqual(activity["reviews"][0]["state"], "CHANGES_REQUESTED")
        self.assertEqual(activity["reviews"][0]["association"], "MEMBER")
        self.assertEqual(activity["review_comments"][0]["line"], 42)
        self.assertEqual(activity["checks"][0]["conclusion"], "success")

    def test_reopen_updates_only_the_matching_pull_request(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        closed = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "closed",
            "draft": False,
            "head": {
                "ref": "tiammomo/docs/example",
                "sha": "a" * 40,
                "repo": {"owner": {"login": "tiammomo"}},
            },
            "base": {"ref": "main"},
        }
        reopened = {**closed, "state": "open", "title": "docs: example"}

        with patch.object(
            client, "_request", side_effect=[(closed, None), (reopened, None)]
        ) as request:
            result = client.reopen_pull_request(
                "owner/repo",
                12,
                owner="tiammomo",
                branch="tiammomo/docs/example",
                base="main",
                title="docs: example",
                body="Closes #1",
            )

        self.assertEqual(result.state, "open")
        request.assert_called_with(
            "PATCH",
            "/repos/owner/repo/pulls/12",
            data={
                "title": "docs: example",
                "body": "Closes #1",
                "state": "open",
            },
        )

    def test_reopen_rejects_a_different_head_branch(self) -> None:
        client = GitHubClient(GitHubConfig(), token="test-token")
        closed = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "state": "closed",
            "draft": False,
            "head": {
                "ref": "other/branch",
                "sha": "a" * 40,
                "repo": {"owner": {"login": "tiammomo"}},
            },
            "base": {"ref": "main"},
        }

        with (
            patch.object(client, "_request", return_value=(closed, None)),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            client.reopen_pull_request(
                "owner/repo",
                12,
                owner="tiammomo",
                branch="tiammomo/docs/example",
                base="main",
                title="docs: example",
                body="Closes #1",
            )


if __name__ == "__main__":
    unittest.main()
