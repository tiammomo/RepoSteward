from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from .github import PullRequest
from .models import RepositoryInfo

BRANCH_CLEANUP_SCHEMA_VERSION = 1
TERMINAL_MERGE_OUTCOMES = frozenset({"merged", "already_merged"})
SUCCESSFUL_CLEANUP_OUTCOMES = frozenset(
    {"deleted", "already_absent", "reconciled_deleted"}
)
MAX_TEXT_ITEMS = 100
MAX_TEXT_CHARS = 20_000


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _managed_run(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "run_id": str(value.get("run_id") or ""),
        "issue_number": int(value.get("issue_number") or 0),
        "branch": str(value.get("branch") or ""),
        "head_sha": str(value.get("head_sha") or ""),
        "pull_number": int(value.get("pull_number") or 0),
        "merge_outcome": str(value.get("merge_outcome") or ""),
        "merge_head_sha": str(value.get("merge_head_sha") or ""),
        "cleanup_outcome": str(value.get("cleanup_outcome") or ""),
    }
    if (
        not re.fullmatch(r"[0-9a-f]{32}", result["run_id"])
        or result["issue_number"] < 1
        or not result["branch"]
        or len(result["branch"]) > 255
        or not re.fullmatch(r"[0-9a-f]{40}", result["head_sha"])
        or result["pull_number"] < 1
    ):
        raise ValueError("managed branch cleanup run is incomplete")
    return result


def _pull_fact(pull: PullRequest) -> dict[str, Any]:
    return {
        "number": pull.number,
        "state": pull.state,
        "merged": pull.merged,
        "head_repository": pull.head_repository,
        "head_branch": pull.head_branch,
        "head_sha": pull.head_sha,
    }


def build_branch_cleanup_plan(
    repository: RepositoryInfo,
    branches: tuple[dict[str, Any], ...],
    pulls: tuple[PullRequest, ...],
    managed_runs: list[dict[str, Any]],
    incomplete_attempts: list[dict[str, Any]],
    policy_digest: str,
) -> dict[str, Any]:
    """Classify only RepoSteward-managed terminal branches from complete facts."""
    if not re.fullmatch(r"[0-9a-f]{64}", policy_digest):
        raise ValueError("branch cleanup policy digest is invalid")
    if (
        not re.fullmatch(r"[^/\s]+/[^/\s]+", repository.full_name)
        or not repository.default_branch
        or not repository.owner_login
    ):
        raise ValueError("GitHub repository facts are incomplete")
    normalized_runs = sorted(
        (_managed_run(value) for value in managed_runs),
        key=lambda value: (value["branch"], value["run_id"]),
    )
    branch_map = {str(value["name"]): dict(value) for value in branches}
    if len(branch_map) != len(branches):
        raise ValueError("GitHub branch snapshot contains duplicate names")
    run_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in normalized_runs:
        run_groups[str(run["branch"])].append(run)

    pull_facts = tuple(_pull_fact(pull) for pull in pulls)
    if len({int(value["number"]) for value in pull_facts}) != len(pull_facts):
        raise ValueError("GitHub pull request snapshot contains duplicate numbers")
    pending_by_run: dict[str, dict[str, Any]] = {}
    for value in incomplete_attempts:
        pending = {
            "attempt_id": str(value.get("attempt_id") or ""),
            "run_id": str(value.get("run_id") or ""),
            "issue_number": int(value.get("issue_number") or 0),
            "pull_number": int(value.get("pull_number") or 0),
            "branch": str(value.get("branch") or ""),
            "head_sha": str(value.get("head_sha") or ""),
            "plan_digest": str(value.get("plan_digest") or ""),
            "actor": str(value.get("actor") or ""),
        }
        if (
            not re.fullmatch(r"[0-9a-f]{32}", pending["attempt_id"])
            or not re.fullmatch(r"[0-9a-f]{32}", pending["run_id"])
            or pending["issue_number"] < 1
            or pending["pull_number"] < 1
            or not pending["branch"]
            or len(pending["branch"]) > 255
            or not re.fullmatch(r"[0-9a-f]{40}", pending["head_sha"])
            or not re.fullmatch(r"[0-9a-f]{64}", pending["plan_digest"])
            or not pending["actor"]
            or len(pending["actor"]) > 128
        ):
            raise ValueError("pending branch cleanup attempt is incomplete")
        if pending["run_id"] in pending_by_run:
            raise ValueError("a run has multiple incomplete cleanup attempts")
        pending_by_run[pending["run_id"]] = pending
    managed_run_ids = {str(value["run_id"]) for value in normalized_runs}
    if set(pending_by_run) - managed_run_ids:
        raise ValueError("pending cleanup attempt has no submitted managed run")

    candidates: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    folded_repository = repository.full_name.casefold()
    for branch_name, bindings in sorted(run_groups.items()):
        same_repository_pulls = [
            value
            for value in pull_facts
            if str(value["head_repository"]).casefold() == folded_repository
            and value["head_branch"] == branch_name
        ]
        for binding in bindings:
            pending_attempt = pending_by_run.get(str(binding["run_id"]))
            if pending_attempt is not None:
                if any(
                    pending_attempt[key] != binding[key]
                    for key in (
                        "issue_number",
                        "pull_number",
                        "branch",
                        "head_sha",
                    )
                ):
                    raise ValueError("pending cleanup attempt changed its run binding")
                pending.append(pending_attempt)
                continue
            branch = branch_map.get(branch_name)
            reasons: list[str] = []
            if binding["cleanup_outcome"] in SUCCESSFUL_CLEANUP_OUTCOMES:
                if branch is None:
                    completed.append({**binding, "status": "cleanup_complete"})
                else:
                    retained.append(
                        {
                            **binding,
                            "current_head_sha": str(branch.get("head_sha") or ""),
                            "reasons": ["branch_recreated_after_cleanup"],
                        }
                    )
                continue
            if len(bindings) != 1:
                reasons.append("ambiguous_managed_runs")
            if binding["merge_outcome"] not in TERMINAL_MERGE_OUTCOMES:
                reasons.append("no_successful_merge_audit")
            if binding["merge_head_sha"] != binding["head_sha"]:
                reasons.append("merge_audit_head_mismatch")
            matching = [
                pull
                for pull in same_repository_pulls
                if pull["number"] == binding["pull_number"]
                and pull["merged"]
                and pull["head_sha"] == binding["head_sha"]
            ]
            if any(
                str(pull["state"]).casefold() == "open"
                for pull in same_repository_pulls
            ):
                reasons.append("open_pull_request")
            if len(same_repository_pulls) != 1:
                reasons.append("shared_branch_history")
            if len(matching) != 1:
                exact_pull = next(
                    (
                        pull
                        for pull in pull_facts
                        if pull["number"] == binding["pull_number"]
                    ),
                    None,
                )
                if exact_pull is not None and not exact_pull["merged"]:
                    reasons.append("closed_unmerged_or_open")
                else:
                    reasons.append("no_exact_merged_pull")
            if branch is None:
                if reasons:
                    retained.append(
                        {
                            **binding,
                            "status": "absent_but_unproven",
                            "reasons": sorted(set(reasons)),
                        }
                    )
                else:
                    absent.append({**binding, "status": "already_absent"})
                continue
            if branch_name == repository.default_branch:
                reasons.append("default_branch")
            if branch.get("protected") is True:
                reasons.append("protected_branch")
            elif branch.get("protected") is not False:
                reasons.append("protection_unknown")
            if str(branch.get("head_sha") or "") != binding["head_sha"]:
                reasons.append("head_changed")
            if reasons:
                retained.append(
                    {
                        **binding,
                        "current_head_sha": str(branch.get("head_sha") or ""),
                        "reasons": sorted(set(reasons)),
                    }
                )
            else:
                candidates.append(binding)

    facts = {
        "schema_version": BRANCH_CLEANUP_SCHEMA_VERSION,
        "repository": repository.full_name.casefold(),
        "default_branch": repository.default_branch,
        "delete_branch_on_merge": repository.delete_branch_on_merge,
        "policy_digest": policy_digest,
        "managed_runs": normalized_runs,
        "candidates": candidates,
        "retained": retained,
        "absent": absent,
        "completed": completed,
        "pending": sorted(pending, key=lambda value: value["attempt_id"]),
    }
    return {
        **facts,
        "counts": {
            "candidates": len(candidates),
            "retained": len(retained),
            "already_absent": len(absent),
            "completed": len(completed),
            "pending": len(pending),
            "total": len(normalized_runs),
        },
        "plan_digest": _digest(facts),
        "harness_invoked": False,
        "workspace_modified": False,
        "public_write": False,
    }


def fresh_candidate_blockers(
    repository: RepositoryInfo,
    branch: dict[str, Any] | None,
    pull: PullRequest,
    history: tuple[PullRequest, ...],
    candidate: dict[str, Any],
) -> tuple[list[str], bool]:
    """Recheck one candidate immediately before a leased delete."""
    expected_branch = str(candidate["branch"])
    expected_sha = str(candidate["head_sha"])
    expected_pull = int(candidate["pull_number"])
    blockers = []
    if branch is not None:
        if expected_branch == repository.default_branch:
            blockers.append("default_branch")
        if branch.get("protected") is True:
            blockers.append("protected_branch")
        elif branch.get("protected") is not False:
            blockers.append("protection_unknown")
        if str(branch.get("head_sha") or "") != expected_sha:
            blockers.append("head_changed")
    exact_history = tuple(
        value
        for value in history
        if value.head_repository.casefold() == repository.full_name.casefold()
        and value.head_branch == expected_branch
    )
    if any(value.state.casefold() == "open" for value in exact_history):
        blockers.append("open_pull_request")
    if len(exact_history) != 1:
        blockers.append("shared_branch_history")
    if (
        pull.number != expected_pull
        or pull.head_repository.casefold() != repository.full_name.casefold()
        or pull.head_branch != expected_branch
    ):
        blockers.append("pull_head_changed")
    if pull.head_sha != expected_sha:
        blockers.append("pull_sha_changed")
    if not pull.merged or pull.state.casefold() != "closed":
        blockers.append("pull_not_merged")
    if not any(value.number == expected_pull for value in exact_history):
        blockers.append("pull_history_changed")
    return sorted(set(blockers)), branch is None


def render_branch_cleanup_text(plan: dict[str, Any]) -> str:
    lines = [
        f"Branch cleanup: {plan['repository']}",
        f"Plan: {plan['plan_digest']}",
        f"GitHub auto-delete: {'yes' if plan['delete_branch_on_merge'] else 'no'}",
        (
            "Counts: "
            f"candidates={plan['counts']['candidates']} "
            f"pending={plan['counts']['pending']} "
            f"absent={plan['counts']['already_absent']} "
            f"completed={plan['counts']['completed']} "
            f"retained={plan['counts']['retained']}"
        ),
    ]
    items = []
    for category in ("candidates", "pending", "absent", "completed", "retained"):
        for value in plan[category]:
            reasons = ",".join(value.get("reasons", ()))
            items.append(
                f"- {category}: {value['branch']}@{value['head_sha'][:12]} "
                f"PR #{value['pull_number']} {reasons}".rstrip()
            )
    lines.extend(items[:MAX_TEXT_ITEMS])
    if len(items) > MAX_TEXT_ITEMS:
        lines.append(f"- ... {len(items) - MAX_TEXT_ITEMS} items omitted")
    result = "\n".join(lines)
    if len(result) <= MAX_TEXT_CHARS:
        return result
    suffix = "\n... output clipped to 20000 characters"
    return result[: MAX_TEXT_CHARS - len(suffix)] + suffix
