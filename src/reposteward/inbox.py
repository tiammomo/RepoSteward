from __future__ import annotations

import hashlib
import json
import re
from typing import Any

INBOX_SCHEMA_VERSION = 1
MAX_TEXT_ITEMS = 100
PULL_NUMBER = re.compile(r"/pull/([1-9][0-9]*)/?$")
FAILED_CHECKS = {"failure", "timed_out", "cancelled", "action_required"}


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _pull_number(run: dict[str, Any]) -> int:
    details = run.get("details")
    if not isinstance(details, dict):
        details = {}
    url = str(details.get("pr_url") or run.get("submission_pr_url") or "")
    match = PULL_NUMBER.search(url)
    return int(match.group(1)) if match else 0


def _item(
    *,
    item_id: str,
    repository: str,
    priority: int,
    reason_code: str,
    summary: str,
    source_updated_at: str,
    next_command: str,
    run_id: str = "",
    issue_number: int = 0,
    pull_number: int = 0,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "repository": repository.casefold(),
        "priority": priority,
        "reason_code": reason_code,
        "summary": summary,
        "source_updated_at": source_updated_at,
        "next_command": next_command,
        "run_id": run_id,
        "issue_number": issue_number,
        "pull_number": pull_number,
    }


def build_maintainer_inbox(
    repository: str,
    *,
    proposals: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    portfolio: dict[str, Any] | None,
    observed_at: str,
    error: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Join bounded local and GitHub facts into deterministic attention items."""
    normalized_repository = repository.casefold()
    items: list[dict[str, Any]] = []
    for proposal in proposals:
        item_id = str(proposal["project_item_id"])
        items.append(
            _item(
                item_id=f"issue-proposal:{item_id}",
                repository=normalized_repository,
                priority=60,
                reason_code="issue_proposal_review_required",
                summary="在线 Issue 提案等待内容与重复项审核",
                source_updated_at=str(proposal.get("updated_at") or ""),
                next_command=(
                    f"reposteward issue review {item_id} "
                    f"--repository {normalized_repository}"
                ),
            )
        )

    pulls = {}
    portfolio_complete = False
    if portfolio is not None:
        snapshot = portfolio.get("snapshot")
        if isinstance(snapshot, dict):
            portfolio_complete = bool(snapshot.get("complete"))
            pulls = {
                int(value["number"]): value
                for value in snapshot.get("pull_requests", ())
                if isinstance(value, dict) and value.get("number")
            }
    tracked_pulls: set[int] = set()
    for run in runs:
        run_id = str(run["id"])
        issue_number = int(run["issue_number"])
        status = str(run.get("status") or "")
        updated_at = str(run.get("updated_at") or "")
        pull_number = _pull_number(run)
        if pull_number:
            tracked_pulls.add(pull_number)
        common = {
            "item_id": f"run:{run_id}",
            "repository": normalized_repository,
            "source_updated_at": updated_at,
            "run_id": run_id,
            "issue_number": issue_number,
            "pull_number": pull_number,
        }
        if status == "failed":
            items.append(
                _item(
                    **common,
                    priority=75,
                    reason_code="run_failed",
                    summary=f"Issue #{issue_number} 的本地运行失败",
                    next_command=f"reposteward inspect {run_id}",
                )
            )
            continue
        if status == "ready":
            items.append(
                _item(
                    **common,
                    priority=50,
                    reason_code="local_review_required",
                    summary=f"Issue #{issue_number} 已验证，等待人工审阅",
                    next_command=f"reposteward inspect {run_id}",
                )
            )
            continue
        if status != "submitted":
            continue
        pull = pulls.get(pull_number)
        if pull is None or not pull.get("facts_complete"):
            items.append(
                _item(
                    **common,
                    priority=80,
                    reason_code="refresh_required",
                    summary=f"PR #{pull_number or '?'} 的在线事实缺失或不完整",
                    next_command=f"reposteward follow-up {run_id}",
                )
            )
            continue
        checks = pull.get("checks") or ()
        failed = any(
            str(value.get("conclusion") or "").casefold() in FAILED_CHECKS
            for value in checks
            if isinstance(value, dict) and value.get("required")
        )
        if failed:
            items.append(
                _item(
                    **common,
                    priority=100,
                    reason_code="required_ci_failed",
                    summary=f"PR #{pull_number} 的必需 CI 失败",
                    next_command=(
                        f"reposteward ci diagnose {normalized_repository} {pull_number}"
                    ),
                )
            )
            continue
        review = str(pull.get("review_decision") or "").casefold()
        unresolved = int(pull.get("unresolved_conversations") or 0)
        if review == "changes_requested" or unresolved:
            items.append(
                _item(
                    **common,
                    priority=90,
                    reason_code="review_feedback_required",
                    summary=f"PR #{pull_number} 有待处理的 Review 反馈",
                    next_command=f"reposteward follow-up {run_id}",
                )
            )
            continue
        items.append(
            _item(
                **common,
                priority=40,
                reason_code="merge_check_required",
                summary=f"PR #{pull_number} 等待完整合并资格判断",
                next_command=f"reposteward merge-decision {run_id}",
            )
        )

    for pull_number, pull in pulls.items():
        if pull_number in tracked_pulls:
            continue
        facts_complete = bool(pull.get("facts_complete"))
        items.append(
            _item(
                item_id=f"pull-request:{pull_number}",
                repository=normalized_repository,
                priority=30 if facts_complete else 80,
                reason_code=(
                    "untracked_pull_request" if facts_complete else "refresh_required"
                ),
                summary=(
                    f"PR #{pull_number} 没有关联的 RepoSteward run"
                    if facts_complete
                    else f"未跟踪 PR #{pull_number} 的在线事实不完整"
                ),
                source_updated_at=str(pull.get("updated_at") or ""),
                next_command=(f"reposteward portfolio inspect {normalized_repository}"),
                pull_number=pull_number,
            )
        )

    if error:
        items.append(
            _item(
                item_id=f"repository:{normalized_repository}",
                repository=normalized_repository,
                priority=110,
                reason_code="github_refresh_failed",
                summary=f"GitHub 状态读取失败：{error[:300]}",
                source_updated_at=observed_at,
                next_command=f"reposteward inbox --repo {normalized_repository}",
            )
        )
    ordered = sorted(
        items,
        key=lambda value: (
            -int(value["priority"]),
            str(value["source_updated_at"]),
            str(value["id"]),
        ),
    )
    limit = min(max(limit, 1), 500)
    omitted = max(0, len(ordered) - limit)
    selected = ordered[:limit]
    facts = {
        "schema_version": INBOX_SCHEMA_VERSION,
        "repository": normalized_repository,
        "items": selected,
        "omitted_count": omitted,
        "complete": not error and portfolio_complete,
    }
    return {
        **facts,
        "observed_at": observed_at,
        "inbox_digest": _canonical_digest(facts),
        "harness_invoked": False,
        "workspace_modified": False,
        "public_write": False,
    }


def render_inbox_text(result: dict[str, Any]) -> str:
    lines = [
        f"Maintainer inbox: {result['repository']}",
        f"Complete: {'yes' if result['complete'] else 'no'}",
        f"Items: {len(result['items'])}; omitted: {result['omitted_count']}",
    ]
    for item in result["items"][:MAX_TEXT_ITEMS]:
        lines.append(
            f"P{item['priority']} {item['reason_code']} {item['summary']}\n"
            f"  next: {item['next_command']}"
        )
    return "\n".join(lines)
