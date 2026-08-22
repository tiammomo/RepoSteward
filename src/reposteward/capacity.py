from __future__ import annotations

from typing import Any

from .github import PullRequest

CAPACITY_DETAILS_LIMIT = 20


def effective_capacity_limit(global_limit: int, repository_limit: int | None) -> int:
    """A repository may tighten, but never raise, a user-owned capacity limit."""
    return (
        min(global_limit, repository_limit)
        if repository_limit is not None
        else global_limit
    )


def pull_request_capacity(
    pulls: tuple[PullRequest, ...], *, login: str, limit: int
) -> dict[str, Any]:
    """Return a bounded, deterministic view of one account's open PR capacity."""
    if limit < 1:
        raise ValueError("pull request capacity limit must be positive")
    active = sorted(
        (
            pull
            for pull in pulls
            if pull.state.casefold() == "open"
            and pull.author.casefold() == login.casefold()
        ),
        key=lambda pull: pull.number,
    )
    details = active[:CAPACITY_DETAILS_LIMIT]
    return {
        "limit": limit,
        "active_count": len(active),
        "available": max(limit - len(active), 0),
        "allows_new_pull_request": len(active) < limit,
        "active_pull_requests": [
            {
                "number": pull.number,
                "draft": pull.draft,
                "url": pull.url,
            }
            for pull in details
        ],
        "active_pull_requests_omitted": len(active) - len(details),
    }
