from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import defaultdict
from typing import Any

BATCH_SCHEMA_VERSION = 1
MAX_TEXT_PULLS = 50
MAX_PARALLEL = 32


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _pull_number(run: dict[str, Any]) -> int:
    details = run.get("details")
    if not isinstance(details, dict):
        return 0
    locator = str(details.get("pr_url") or run.get("submission_pr_url") or "")
    match = re.search(r"/pull/([1-9][0-9]*)/?$", locator)
    return int(match.group(1)) if match else 0


def _overlap_groups(
    pull_numbers: set[int], overlaps: list[dict[str, Any]]
) -> tuple[list[list[int]], dict[int, int]]:
    parent = {number: number for number in pull_numbers}

    def find(number: int) -> int:
        root = number
        while parent[root] != root:
            root = parent[root]
        while parent[number] != number:
            next_number = parent[number]
            parent[number] = root
            number = next_number
        return root

    for value in overlaps:
        left = int(value["left"])
        right = int(value["right"])
        if left not in parent or right not in parent:
            continue
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)
    grouped: dict[int, list[int]] = defaultdict(list)
    for number in sorted(pull_numbers):
        grouped[find(number)].append(number)
    groups = [values for values in grouped.values() if len(values) > 1]
    groups.sort(key=lambda values: values)
    membership = {
        number: index
        for index, values in enumerate(groups, start=1)
        for number in values
    }
    return groups, membership


def _parallel_sets(
    order: list[int],
    prerequisites: dict[int, set[int]],
    conflicts: dict[int, set[int]],
    *,
    max_parallel: int,
) -> list[list[int]]:
    index = {number: offset for offset, number in enumerate(order)}
    nodes = set(order)
    remaining_dependencies = {
        number: set(prerequisites.get(number, ())) & nodes for number in order
    }
    dependents: dict[int, set[int]] = defaultdict(set)
    for source, dependencies in remaining_dependencies.items():
        for dependency in dependencies:
            dependents[dependency].add(source)
    ready = [
        (index[number], number)
        for number in order
        if not remaining_dependencies[number]
    ]
    heapq.heapify(ready)
    result: list[list[int]] = []
    completed: set[int] = set()
    while ready:
        wave: list[int] = []
        deferred: list[tuple[int, int]] = []
        wave_members: set[int] = set()
        while ready and len(wave) < max_parallel:
            item = heapq.heappop(ready)
            number = item[1]
            if conflicts.get(number, set()) & wave_members:
                deferred.append(item)
                continue
            wave.append(number)
            wave_members.add(number)
        for item in deferred:
            heapq.heappush(ready, item)
        if not wave:
            # The dependency planner already rejects cycles. This keeps the batch
            # planner fail-closed if a malformed caller bypasses that invariant.
            break
        result.append(wave)
        completed.update(wave)
        for number in wave:
            for dependent in dependents.get(number, ()):
                remaining_dependencies[dependent].discard(number)
                if not remaining_dependencies[dependent]:
                    heapq.heappush(ready, (index[dependent], dependent))
    if completed != nodes:
        return []
    return result


def build_batch_plan(
    portfolio_snapshot: dict[str, Any],
    dependency_plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    wip_limit: int,
    max_parallel: int,
) -> dict[str, Any]:
    """Combine online PR facts and local runs into one deterministic train plan."""
    if not 1 <= max_parallel <= MAX_PARALLEL:
        raise ValueError(f"max_parallel must be between 1 and {MAX_PARALLEL}")
    if wip_limit < 1:
        raise ValueError("wip_limit must be positive")
    repository = str(portfolio_snapshot["repository"]).casefold()
    if str(dependency_plan["repository"]).casefold() != repository:
        raise ValueError("batch dependency plan belongs to another repository")
    if str(dependency_plan.get("portfolio_snapshot_digest") or "") != str(
        portfolio_snapshot.get("snapshot_digest") or ""
    ):
        raise ValueError("batch inputs were built from different portfolio snapshots")

    pulls = {
        int(value["number"]): value
        for value in portfolio_snapshot.get("pull_requests", ())
    }
    runs_by_pull: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        number = _pull_number(run)
        if number in pulls:
            runs_by_pull[number].append(run)
    overlaps = list(portfolio_snapshot.get("overlaps", ()))
    overlap_groups, membership = _overlap_groups(set(pulls), overlaps)
    hard_dependency_blockers = {
        int(number): [
            str(value)
            for value in values
            if not str(value).startswith("dependency_open:")
        ]
        for number, values in dependency_plan.get("ready_blockers", {}).items()
    }

    entries: list[dict[str, Any]] = []
    runnable: set[int] = set()
    for number, pull in sorted(pulls.items()):
        blockers = list(hard_dependency_blockers.get(number, ()))
        matching_runs = runs_by_pull.get(number, [])
        run = matching_runs[0] if len(matching_runs) == 1 else None
        if not portfolio_snapshot.get("complete"):
            blockers.append("portfolio_snapshot_incomplete")
        if not bool(pull.get("facts_complete")):
            blockers.append("pull_facts_incomplete")
        if bool(pull.get("draft")):
            blockers.append("pull_is_draft")
        if len(matching_runs) > 1:
            blockers.append("ambiguous_local_run")
        elif run is None:
            blockers.append("local_run_missing")
        details = run.get("details", {}) if run is not None else {}
        if not isinstance(details, dict):
            details = {}
        run_status = str(run.get("status") or "") if run is not None else ""
        if run is not None and run_status != "submitted":
            blockers.append(f"local_run_not_submitted:{run_status or 'unknown'}")
        if run is not None and str(details.get("branch") or "") != str(
            pull.get("head_branch") or ""
        ):
            blockers.append("tracked_branch_changed")
        expected_head = str(details.get("commit_sha") or "")
        online_head = str(pull.get("head_sha") or "")
        online_base = str(pull.get("base_sha") or "")
        if not re.fullmatch(r"[a-f0-9]{40}", online_head) or not re.fullmatch(
            r"[a-f0-9]{40}", online_base
        ):
            blockers.append("pull_identity_incomplete")
        if run is not None and not re.fullmatch(r"[a-f0-9]{40}", expected_head):
            blockers.append("verified_head_missing")
        if run_status == "submitted" and expected_head and expected_head != online_head:
            blockers.append("pull_head_changed")
        if run is not None and str(details.get("base_branch") or "") != str(
            pull.get("base_branch") or ""
        ):
            blockers.append("base_branch_changed")
        verified_base = str(details.get("base_commit") or "")
        if run is not None and not re.fullmatch(r"[a-f0-9]{40}", verified_base):
            blockers.append("verified_base_missing")
        replay_required = bool(
            run_status == "submitted" and verified_base and verified_base != online_base
        )
        if replay_required:
            if not bool(run.get("batch_worktree_available", True)):
                blockers.append("replay_worktree_missing")
            agent = details.get("agent_result")
            raw_commands = (
                agent.get("verification_commands", ())
                if isinstance(agent, dict)
                else ()
            )
            if not isinstance(raw_commands, (list, tuple)) or not raw_commands:
                blockers.append("replay_verification_missing")
        blockers = sorted(set(blockers))
        if not blockers:
            runnable.add(number)
        entries.append(
            {
                "pull_number": number,
                "run_id": str(run.get("id") or "") if run is not None else "",
                "issue_number": int(run.get("issue_number") or 0)
                if run is not None
                else 0,
                "run_status": run_status,
                "head_sha": str(pull.get("head_sha") or ""),
                "base_sha": str(pull.get("base_sha") or ""),
                "verified_head_sha": expected_head,
                "verified_base_sha": verified_base,
                "replay_required": replay_required,
                "overlap_group": membership.get(number, 0),
                "blockers": blockers,
            }
        )

    entries_by_number = {int(value["pull_number"]): value for value in entries}
    batch_dependents: dict[int, set[int]] = defaultdict(set)
    for edge in dependency_plan.get("authoritative_edges", ()):
        if str(edge.get("status") or "") == "open":
            batch_dependents[int(edge["dependency_number"])].add(
                int(edge["pull_number"])
            )
    blocked_queue = [number for number in pulls if number not in runnable]
    heapq.heapify(blocked_queue)
    while blocked_queue:
        dependency = heapq.heappop(blocked_queue)
        for dependent in batch_dependents.get(dependency, ()):
            if dependent not in runnable:
                continue
            runnable.remove(dependent)
            entry = entries_by_number[dependent]
            entry["blockers"] = sorted(
                {
                    *entry["blockers"],
                    f"dependency_prerequisite_not_runnable:#{dependency}",
                }
            )
            heapq.heappush(blocked_queue, dependent)

    dependency_order = [
        int(value) for value in dependency_plan.get("suggested_merge_order", ())
    ]
    queue_order = [number for number in dependency_order if number in runnable]
    prerequisites: dict[int, set[int]] = defaultdict(set)
    for edge in dependency_plan.get("authoritative_edges", ()):
        source = int(edge["pull_number"])
        target = int(edge["dependency_number"])
        if str(edge.get("status") or "") == "open" and target in runnable:
            prerequisites[source].add(target)
    conflicts: dict[int, set[int]] = defaultdict(set)
    for value in overlaps:
        left, right = int(value["left"]), int(value["right"])
        if left in runnable and right in runnable:
            conflicts[left].add(right)
            conflicts[right].add(left)
    parallel_sets = _parallel_sets(
        queue_order, prerequisites, conflicts, max_parallel=max_parallel
    )
    complete = bool(portfolio_snapshot.get("complete")) and bool(
        dependency_plan.get("complete")
    )
    material = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "repository": repository,
        "portfolio_snapshot_digest": str(
            portfolio_snapshot.get("snapshot_digest") or ""
        ),
        "dependency_plan_digest": str(dependency_plan.get("plan_digest") or ""),
        "complete": complete,
        "max_parallel": max_parallel,
        "wip": {
            "active": len(pulls),
            "limit": wip_limit,
            "over_limit": len(pulls) > wip_limit,
        },
        "queue_order": queue_order,
        "parallel_sets": parallel_sets,
        "overlap_groups": overlap_groups,
        "pull_requests": entries,
        "blocked_pull_requests": {
            str(value["pull_number"]): value["blockers"]
            for value in entries
            if value["blockers"]
        },
        "serialization": "repository_merge_train",
        "overlap_is_authoritative": False,
    }
    return {**material, "batch_digest": _digest(material)}


def render_batch_plan_text(result: dict[str, Any]) -> str:
    plan = result["plan"]
    lines = [
        f"Batch plan: {plan['repository']}",
        f"Batch: {result['batch_digest']}",
        f"Complete: {'yes' if plan['complete'] else 'no'}",
        "Queue order: "
        + (" -> ".join(f"#{value}" for value in plan["queue_order"]) or "none"),
        (
            f"WIP: {plan['wip']['active']}/{plan['wip']['limit']}"
            + (" (over limit)" if plan["wip"]["over_limit"] else "")
        ),
    ]
    expected = result.get("expected_digest")
    if expected:
        lines.append(
            "Expected batch: "
            + ("current" if result.get("matches_expected_digest") else "stale")
        )
    for wave, values in enumerate(plan["parallel_sets"], start=1):
        lines.append(
            f"Parallel set {wave}: " + ", ".join(f"#{value}" for value in values)
        )
    blocked = list(plan["blocked_pull_requests"].items())
    for number, blockers in blocked[:MAX_TEXT_PULLS]:
        lines.append(f"Blocked #{number}: {', '.join(blockers)}")
    if len(blocked) > MAX_TEXT_PULLS:
        lines.append(f"... {len(blocked) - MAX_TEXT_PULLS} blocked PRs omitted")
    return "\n".join(lines)
