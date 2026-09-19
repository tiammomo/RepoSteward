from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from itertools import combinations
from typing import Any

PORTFOLIO_SCHEMA_VERSION = 1
MAX_TEXT_OVERLAP_FILES = 5
MAX_TEXT_PULL_REQUESTS = 50
MAX_TEXT_OVERLAPS = 50
MAX_TEXT_ERRORS = 20


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _normalized_check(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(value.get("name") or ""),
        "status": str(value.get("status") or ""),
        "conclusion": str(value.get("conclusion") or ""),
        "required": bool(value.get("required")),
    }


def _normalized_pull(value: dict[str, Any]) -> dict[str, Any]:
    checks = sorted(
        (
            _normalized_check(check)
            for check in value.get("checks", ())
            if isinstance(check, dict)
        ),
        key=lambda check: (
            check["name"].casefold(),
            check["status"].casefold(),
            check["conclusion"].casefold(),
            check["required"],
        ),
    )
    files = sorted(
        {str(path) for path in value.get("files", ()) if isinstance(path, str) and path}
    )
    files_complete = bool(value.get("files_complete"))
    conversations_complete = bool(value.get("conversations_complete"))
    checks_complete = bool(value.get("checks_complete"))
    return {
        "number": int(value["pull_number"]),
        "title": str(value.get("title") or ""),
        "url": str(value.get("url") or ""),
        "state": str(value.get("state") or ""),
        "draft": bool(value.get("draft")),
        "updated_at": str(value.get("updated_at") or ""),
        "head_branch": str(value.get("head_branch") or ""),
        "head_sha": str(value.get("head_sha") or ""),
        "base_branch": str(value.get("base_branch") or ""),
        "base_sha": str(value.get("base_sha") or ""),
        "mergeable": str(value.get("mergeable") or ""),
        "review_decision": str(value.get("review_decision") or ""),
        "unresolved_conversations": int(value.get("unresolved_conversations") or 0),
        "files": files,
        "additions": int(value.get("additions") or 0),
        "deletions": int(value.get("deletions") or 0),
        "checks": checks,
        "files_complete": files_complete,
        "conversations_complete": conversations_complete,
        "checks_complete": checks_complete,
        "facts_complete": bool(
            files_complete and conversations_complete and checks_complete
        ),
    }


def build_portfolio_snapshot(
    repository: str,
    pull_requests: list[dict[str, Any]],
    *,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable repository-wide snapshot without model assumptions."""
    pulls = sorted(
        (_normalized_pull(value) for value in pull_requests),
        key=lambda value: value["number"],
    )
    numbers = [value["number"] for value in pulls]
    if len(numbers) != len(set(numbers)):
        raise ValueError("portfolio snapshot contains duplicate pull requests")

    pulls_by_file: dict[str, list[int]] = defaultdict(list)
    for pull in pulls:
        if not pull["files_complete"]:
            continue
        for path in pull["files"]:
            pulls_by_file[path].append(pull["number"])

    files_by_pair: dict[tuple[int, int], list[str]] = defaultdict(list)
    for path, pull_numbers in pulls_by_file.items():
        for pair in combinations(sorted(set(pull_numbers)), 2):
            files_by_pair[pair].append(path)
    overlaps = [
        {
            "left": pair[0],
            "right": pair[1],
            "file_count": len(paths),
            "files": sorted(paths),
        }
        for pair, paths in sorted(files_by_pair.items())
    ]
    normalized_errors = sorted(
        (
            {
                "pull_number": int(value.get("pull_number") or 0),
                "message": str(value.get("message") or "")[:500],
            }
            for value in (errors or ())
        ),
        key=lambda value: (value["pull_number"], value["message"]),
    )
    snapshot = {
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "repository": repository.casefold(),
        "complete": not normalized_errors
        and all(value["facts_complete"] for value in pulls),
        "pull_requests": pulls,
        "overlaps": overlaps,
        "errors": normalized_errors,
        "stats": {
            "pull_requests": len(pulls),
            "draft_pull_requests": sum(value["draft"] for value in pulls),
            "overlapping_pairs": len(overlaps),
            "files_in_overlaps": sum(
                len(set(pull_numbers)) > 1 for pull_numbers in pulls_by_file.values()
            ),
            "incomplete_pull_requests": sum(
                not value["facts_complete"] for value in pulls
            ),
        },
    }
    return {**snapshot, "snapshot_digest": _digest(snapshot)}


def render_portfolio_text(result: dict[str, Any]) -> str:
    snapshot = result["snapshot"]
    stats = snapshot["stats"]
    lines = [
        f"Portfolio: {snapshot['repository']}",
        f"Snapshot: {result['snapshot_digest']}",
        f"Complete: {'yes' if snapshot['complete'] else 'no'}",
        (
            "Pull requests: "
            f"{stats['pull_requests']} ({stats['draft_pull_requests']} draft); "
            f"overlapping pairs: {stats['overlapping_pairs']}"
        ),
    ]
    expected = result.get("expected_digest")
    if expected:
        state = "current" if result.get("matches_expected_digest") else "stale"
        lines.append(f"Expected snapshot: {state}")
    pulls = snapshot["pull_requests"]
    for pull in pulls[:MAX_TEXT_PULL_REQUESTS]:
        required = [value for value in pull["checks"] if value["required"]]
        successful = sum(
            str(value["conclusion"]).casefold() in {"success", "neutral", "skipped"}
            for value in required
        )
        lines.append(
            f"#{pull['number']} "
            f"{'draft' if pull['draft'] else 'open'} "
            f"base={pull['base_branch']}@{pull['base_sha'][:10]} "
            f"head={pull['head_sha'][:10]} files={len(pull['files'])} "
            f"required_checks={successful}/{len(required)} "
            f"review={pull['review_decision'] or 'NONE'} "
            f"threads={pull['unresolved_conversations']}"
        )
    if len(pulls) > MAX_TEXT_PULL_REQUESTS:
        lines.append(
            f"... {len(pulls) - MAX_TEXT_PULL_REQUESTS} additional pull requests "
            "omitted; use --format json for complete facts"
        )
    overlaps = snapshot["overlaps"]
    for overlap in overlaps[:MAX_TEXT_OVERLAPS]:
        shown = overlap["files"][:MAX_TEXT_OVERLAP_FILES]
        omitted = overlap["file_count"] - len(shown)
        suffix = f" (+{omitted} more)" if omitted else ""
        lines.append(
            f"#{overlap['left']} <-> #{overlap['right']}: {', '.join(shown)}{suffix}"
        )
    if len(overlaps) > MAX_TEXT_OVERLAPS:
        lines.append(
            f"... {len(overlaps) - MAX_TEXT_OVERLAPS} additional overlaps omitted; "
            "use --format json for complete facts"
        )
    errors = snapshot["errors"]
    for error in errors[:MAX_TEXT_ERRORS]:
        lines.append(f"Incomplete #{error['pull_number']}: {error['message']}")
    if len(errors) > MAX_TEXT_ERRORS:
        lines.append(
            f"... {len(errors) - MAX_TEXT_ERRORS} additional errors omitted; "
            "use --format json for complete facts"
        )
    return "\n".join(lines)
