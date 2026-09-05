"""Shared read-only portfolio collection for Pipeline and local project views."""

from __future__ import annotations

from typing import Any

from .config import RepositoryPolicy
from .github import GitHubClient, GitHubError, PullRequest
from .portfolio import build_portfolio_snapshot


def portfolio_from_pulls(
    github: GitHubClient,
    policy: RepositoryPolicy,
    pulls: tuple[PullRequest, ...],
    *,
    expected_digest: str = "",
    max_pulls: int | None = None,
) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if max_pulls is not None and len(pulls) > max_pulls:
        errors.append(
            {
                "pull_number": 0,
                "message": f"portfolio omitted {len(pulls) - max_pulls} open PRs",
            }
        )
        pulls = pulls[:max_pulls]
    for pull in pulls:
        try:
            snapshot = github.pull_request_merge_snapshot(policy.name, pull.number)
            if str(snapshot.get("state") or "").casefold() != "open":
                errors.append(
                    {
                        "pull_number": pull.number,
                        "message": "pull request changed state during snapshot",
                    }
                )
            elif pull.head_sha and snapshot.get("head_sha") != pull.head_sha:
                errors.append(
                    {
                        "pull_number": pull.number,
                        "message": "pull request head changed during snapshot",
                    }
                )
            elif pull.base_sha and snapshot.get("base_sha") != pull.base_sha:
                errors.append(
                    {
                        "pull_number": pull.number,
                        "message": "pull request base changed during snapshot",
                    }
                )
            snapshots.append(snapshot)
        except GitHubError as exc:
            errors.append({"pull_number": pull.number, "message": str(exc)[:500]})
            snapshots.append(
                {
                    "repository": policy.name.casefold(),
                    "pull_number": pull.number,
                    "title": pull.title,
                    "url": pull.url,
                    "state": pull.state,
                    "draft": pull.draft,
                    "updated_at": pull.updated_at,
                    "head_branch": pull.head_branch,
                    "head_sha": pull.head_sha,
                    "base_branch": pull.base_branch,
                    "base_sha": pull.base_sha,
                    "files": [],
                    "checks": [],
                    "files_complete": False,
                    "conversations_complete": False,
                    "checks_complete": False,
                }
            )
    snapshot = build_portfolio_snapshot(policy.name, snapshots, errors=errors)
    digest = str(snapshot.pop("snapshot_digest"))
    return {
        "snapshot_digest": digest,
        "expected_digest": expected_digest,
        "matches_expected_digest": (
            digest == expected_digest if expected_digest else None
        ),
        "snapshot": snapshot,
        "harness_invoked": False,
        "workspace_modified": False,
        "public_write": False,
    }
