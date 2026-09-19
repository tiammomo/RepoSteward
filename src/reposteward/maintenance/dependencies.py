from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import defaultdict
from typing import Any

DEPENDENCY_SCHEMA_VERSION = 1
MAX_SUGGESTION_FILES = 20
MAX_TEXT_EDGES = 50
MAX_TEXT_SUGGESTIONS = 20
MAX_TEXT_ORDER = 50
MAX_TEXT_BLOCKED_PULLS = 50
MAX_TEXT_BLOCKERS_PER_PULL = 10

_DECLARATION = re.compile(
    r"(?:[-*][ \t]+)?depends[ \t]+on[ \t]+"
    r"(?:(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?"
    r"#(?P<number>[1-9][0-9]*)",
    re.IGNORECASE,
)


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_dependency_declarations(
    repository: str,
    pull_number: int,
    head_sha: str,
    body: str,
    *,
    actor: str = "",
) -> list[dict[str, Any]]:
    """Parse only strict, standalone dependency lines outside quotes and fences."""
    normalized_repository = repository.casefold()
    declarations: dict[tuple[str, int], dict[str, Any]] = {}
    fence_character = ""
    fence_length = 0
    html_comment = False
    for line_number, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if html_comment:
            if "-->" in line:
                html_comment = False
            continue
        if "<!--" in line:
            html_comment = "-->" not in line.split("<!--", 1)[1]
            continue
        fence = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_character:
            if re.fullmatch(
                re.escape(fence_character) + "{" + str(fence_length) + r",}\s*",
                stripped,
            ):
                fence_character = ""
                fence_length = 0
            continue
        if fence is not None:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        if line.lstrip().startswith(">"):
            continue
        match = _DECLARATION.fullmatch(stripped)
        if match is None:
            continue
        target_repository = str(
            match.group("repository") or normalized_repository
        ).casefold()
        dependency_number = int(match.group("number"))
        key = (target_repository, dependency_number)
        declarations.setdefault(
            key,
            {
                "pull_number": pull_number,
                "dependency_repository": target_repository,
                "dependency_number": dependency_number,
                "head_sha": head_sha,
                "source": "explicit_pr_body",
                "actor": actor,
                "line": line_number,
                "source_digest": _canonical_digest(
                    {
                        "repository": normalized_repository,
                        "pull_number": pull_number,
                        "head_sha": head_sha,
                        "actor": actor,
                        "dependency_repository": target_repository,
                        "dependency_number": dependency_number,
                    }
                ),
            },
        )
    return sorted(
        declarations.values(),
        key=lambda value: (
            value["pull_number"],
            value["dependency_repository"],
            value["dependency_number"],
        ),
    )


def _authoritative_edges(
    repository: str,
    pulls: dict[int, dict[str, Any]],
    declarations: list[dict[str, Any]],
    attestations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized_repository = repository.casefold()
    sources_by_edge: dict[tuple[int, str, int], list[dict[str, Any]]] = defaultdict(
        list
    )
    for value in declarations:
        pull_number = int(value["pull_number"])
        if pull_number not in pulls:
            continue
        key = (
            pull_number,
            str(value["dependency_repository"]).casefold(),
            int(value["dependency_number"]),
        )
        sources_by_edge[key].append(
            {
                "source": "explicit_pr_body",
                "actor": str(value.get("actor") or ""),
                "head_sha": str(value["head_sha"]),
                "source_digest": str(value["source_digest"]),
            }
        )

    stale: list[dict[str, Any]] = []
    for value in attestations:
        if str(value.get("action") or "") != "confirm":
            continue
        pull_number = int(value["pull_number"])
        pull = pulls.get(pull_number)
        if pull is None:
            continue
        expected_head = str(value["head_sha"])
        if str(pull.get("head_sha") or "") != expected_head:
            current_key = (
                pull_number,
                normalized_repository,
                int(value["dependency_number"]),
            )
            if current_key not in sources_by_edge:
                stale.append(
                    {
                        "pull_number": pull_number,
                        "dependency_number": int(value["dependency_number"]),
                        "confirmed_head_sha": expected_head,
                        "current_head_sha": str(pull.get("head_sha") or ""),
                        "actor": str(value.get("actor") or ""),
                        "event_digest": str(value.get("event_digest") or ""),
                    }
                )
            continue
        key = (
            pull_number,
            normalized_repository,
            int(value["dependency_number"]),
        )
        sources_by_edge[key].append(
            {
                "source": "maintainer_confirmed",
                "actor": str(value.get("actor") or ""),
                "head_sha": expected_head,
                "source_digest": str(value.get("event_digest") or ""),
            }
        )

    edges = []
    for (pull_number, dependency_repository, dependency_number), sources in sorted(
        sources_by_edge.items()
    ):
        unique_sources = {
            (
                str(value["source"]),
                str(value["actor"]),
                str(value["head_sha"]),
                str(value["source_digest"]),
            ): value
            for value in sources
        }
        edges.append(
            {
                "pull_number": pull_number,
                "dependency_repository": dependency_repository,
                "dependency_number": dependency_number,
                "sources": [
                    unique_sources[key]
                    for key in sorted(unique_sources, key=lambda value: value)
                ],
            }
        )
    stale.sort(key=lambda value: (value["pull_number"], value["dependency_number"]))
    return edges, stale


def _classify_edges(
    repository: str,
    pulls: dict[int, dict[str, Any]],
    edges: list[dict[str, Any]],
    target_states: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_repository = repository.casefold()
    classified = []
    for edge in edges:
        pull_number = int(edge["pull_number"])
        dependency_repository = str(edge["dependency_repository"]).casefold()
        dependency_number = int(edge["dependency_number"])
        if dependency_repository != normalized_repository:
            status = "cross_repository"
        elif pull_number == dependency_number:
            status = "self_dependency"
        elif dependency_number in pulls:
            status = "open"
        else:
            target = target_states.get(
                (dependency_repository, dependency_number), {"state": "unknown"}
            )
            state = str(target.get("state") or "unknown").casefold()
            if bool(target.get("merged")) or state == "merged":
                status = "merged"
            elif state == "open":
                status = "open"
            elif state == "closed":
                status = "closed_unmerged"
            elif state == "missing":
                status = "missing"
            else:
                status = "unknown"
        classified.append({**edge, "status": status})
    return classified


def _dependency_cycles(
    nodes: list[int], adjacency: dict[int, set[int]]
) -> list[list[int]]:
    """Return stable strongly connected dependency components without recursion."""
    visited: set[int] = set()
    finished: list[int] = []
    for root in sorted(nodes):
        if root in visited:
            continue
        visited.add(root)
        stack: list[tuple[int, Any]] = [(root, iter(sorted(adjacency.get(root, ()))))]
        while stack:
            node, targets = stack[-1]
            try:
                target = next(targets)
            except StopIteration:
                finished.append(node)
                stack.pop()
                continue
            if target not in visited:
                visited.add(target)
                stack.append((target, iter(sorted(adjacency.get(target, ())))))

    reverse: dict[int, set[int]] = defaultdict(set)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    assigned: set[int] = set()
    components: list[list[int]] = []
    for root in reversed(finished):
        if root in assigned:
            continue
        assigned.add(root)
        component = []
        stack = [root]
        while stack:
            node = stack.pop()
            component.append(node)
            for target in sorted(reverse.get(node, ()), reverse=True):
                if target not in assigned:
                    assigned.add(target)
                    stack.append(target)
        component.sort()
        if len(component) > 1 or component[0] in adjacency.get(component[0], set()):
            components.append(component)
    return sorted(components)


def _topological_order(
    nodes: list[int], adjacency: dict[int, set[int]]
) -> tuple[list[int], list[int]]:
    prerequisites = {node: set(adjacency.get(node, ())) for node in nodes}
    dependents: dict[int, set[int]] = defaultdict(set)
    for source, targets in prerequisites.items():
        for target in targets:
            dependents[target].add(source)
    ready = [node for node in nodes if not prerequisites[node]]
    heapq.heapify(ready)
    order = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for dependent in sorted(dependents.get(node, ())):
            prerequisites[dependent].discard(node)
            if not prerequisites[dependent]:
                heapq.heappush(ready, dependent)
    scheduled = set(order)
    return order, sorted(node for node in nodes if node not in scheduled)


def build_dependency_plan(
    portfolio_snapshot: dict[str, Any],
    declarations: list[dict[str, Any]],
    attestations: list[dict[str, Any]],
    target_states: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    repository = str(portfolio_snapshot["repository"]).casefold()
    pulls = {
        int(value["number"]): value for value in portfolio_snapshot["pull_requests"]
    }
    edges, stale = _authoritative_edges(repository, pulls, declarations, attestations)
    classified = _classify_edges(repository, pulls, edges, target_states)
    adjacency: dict[int, set[int]] = defaultdict(set)
    blockers: dict[int, list[str]] = {number: [] for number in pulls}
    revalidation: dict[int, list[int]] = defaultdict(list)
    complete = bool(portfolio_snapshot.get("complete"))
    for edge in classified:
        pull_number = int(edge["pull_number"])
        dependency_number = int(edge["dependency_number"])
        status = str(edge["status"])
        if status in {"open", "self_dependency"}:
            adjacency[pull_number].add(dependency_number)
            blockers[pull_number].append(f"dependency_{status}:#{dependency_number}")
        elif status == "merged":
            revalidation[pull_number].append(dependency_number)
        elif status != "merged":
            blockers[pull_number].append(f"dependency_{status}:#{dependency_number}")
            if status == "unknown":
                complete = False
    for value in stale:
        blockers[int(value["pull_number"])].append(
            f"dependency_confirmation_stale:#{value['dependency_number']}"
        )
    cycles = _dependency_cycles(sorted(pulls), adjacency)
    for cycle in cycles:
        label = ",".join(f"#{number}" for number in cycle)
        for number in cycle:
            blockers[number].append(f"dependency_cycle:{label}")
    if not bool(portfolio_snapshot.get("complete")):
        for values in blockers.values():
            values.append("portfolio_snapshot_incomplete")
    initially_hard_blocked = {
        number
        for number, values in blockers.items()
        if any(not value.startswith("dependency_open:") for value in values)
    }
    dependents: dict[int, set[int]] = defaultdict(set)
    for source, dependencies in adjacency.items():
        for dependency in dependencies:
            dependents[dependency].add(source)
    hard_blocked = set(initially_hard_blocked)
    pending_blocked = list(initially_hard_blocked)
    heapq.heapify(pending_blocked)
    while pending_blocked:
        dependency = heapq.heappop(pending_blocked)
        for dependent in sorted(dependents.get(dependency, ())):
            if dependent in hard_blocked:
                continue
            hard_blocked.add(dependent)
            heapq.heappush(pending_blocked, dependent)
    for source, dependencies in adjacency.items():
        if source in initially_hard_blocked:
            continue
        blockers[source].extend(
            f"dependency_prerequisite_blocked:#{value}"
            for value in sorted(dependencies & hard_blocked)
        )
    normalized_blockers = {
        str(number): sorted(set(values))
        for number, values in sorted(blockers.items())
        if values
    }
    raw_order, cyclic_or_downstream = _topological_order(sorted(pulls), adjacency)
    order = [number for number in raw_order if number not in hard_blocked]
    unscheduled = sorted(set(cyclic_or_downstream) | hard_blocked)
    suggestions = [
        {
            "left": int(value["left"]),
            "right": int(value["right"]),
            "kind": "file_overlap",
            "file_count": int(value["file_count"]),
            "files": list(value["files"][:MAX_SUGGESTION_FILES]),
            "files_omitted": max(0, len(value["files"]) - MAX_SUGGESTION_FILES),
            "authoritative": False,
        }
        for value in portfolio_snapshot.get("overlaps", ())
    ]
    material = {
        "schema_version": DEPENDENCY_SCHEMA_VERSION,
        "repository": repository,
        "portfolio_snapshot_digest": str(
            portfolio_snapshot.get("snapshot_digest") or ""
        ),
        "complete": complete,
        "authoritative_edges": classified,
        "stale_confirmations": stale,
        "cycles": cycles,
        "suggested_merge_order": order,
        "unscheduled_pull_requests": unscheduled,
        "dependency_ready_pull_requests": [
            number for number in sorted(pulls) if str(number) not in normalized_blockers
        ],
        "ready_blockers": normalized_blockers,
        "revalidation_recommended": {
            str(number): sorted(set(values))
            for number, values in sorted(revalidation.items())
        },
        "suggestions": suggestions,
    }
    return {**material, "plan_digest": _canonical_digest(material)}


def direct_dependency_requirements(
    *,
    repository: str,
    pull_number: int,
    head_sha: str,
    body: str,
    attestations: list[dict[str, Any]],
    target_states: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    pull = {"number": pull_number, "head_sha": head_sha}
    declarations = parse_dependency_declarations(
        repository, pull_number, head_sha, body
    )
    edges, stale = _authoritative_edges(
        repository, {pull_number: pull}, declarations, attestations
    )
    classified = _classify_edges(repository, {pull_number: pull}, edges, target_states)
    blockers = []
    complete = True
    for edge in classified:
        status = str(edge["status"])
        dependency_number = int(edge["dependency_number"])
        if status == "merged":
            continue
        blockers.append(f"dependency_{status}:#{dependency_number}")
        if status == "unknown":
            complete = False
    blockers.extend(
        f"dependency_confirmation_stale:#{value['dependency_number']}"
        for value in stale
    )
    material = {
        "edges": classified,
        "stale_confirmations": stale,
        "complete": complete,
        "blockers": sorted(set(blockers)),
    }
    return {**material, "dependency_digest": _canonical_digest(material)}


def render_dependency_plan_text(result: dict[str, Any]) -> str:
    plan = result["plan"]
    lines = [
        f"Dependency plan: {plan['repository']}",
        f"Plan: {result['plan_digest']}",
        f"Portfolio snapshot: {plan['portfolio_snapshot_digest']}",
        f"Complete: {'yes' if plan['complete'] else 'no'}",
        "Suggested merge order: "
        + (
            " -> ".join(
                f"#{value}" for value in plan["suggested_merge_order"][:MAX_TEXT_ORDER]
            )
            or "none"
        ),
    ]
    expected = result.get("expected_digest")
    if expected:
        state = "current" if result.get("matches_expected_digest") else "stale"
        lines.append(f"Expected plan: {state}")
    edges = plan["authoritative_edges"]
    for edge in edges[:MAX_TEXT_EDGES]:
        sources = ",".join(value["source"] for value in edge["sources"])
        lines.append(
            f"#{edge['pull_number']} depends on "
            f"{edge['dependency_repository']}#{edge['dependency_number']} "
            f"[{edge['status']}; {sources}]"
        )
    if len(edges) > MAX_TEXT_EDGES:
        lines.append(f"... {len(edges) - MAX_TEXT_EDGES} dependency edges omitted")
    order = plan["suggested_merge_order"]
    if len(order) > MAX_TEXT_ORDER:
        lines.append(f"... {len(order) - MAX_TEXT_ORDER} merge-order entries omitted")
    blocked = list(plan["ready_blockers"].items())
    for pull_number, blockers in blocked[:MAX_TEXT_BLOCKED_PULLS]:
        shown = blockers[:MAX_TEXT_BLOCKERS_PER_PULL]
        omitted = len(blockers) - len(shown)
        suffix = f" (+{omitted} more)" if omitted else ""
        lines.append(f"Blocked #{pull_number}: {', '.join(shown)}{suffix}")
    if len(blocked) > MAX_TEXT_BLOCKED_PULLS:
        lines.append(
            f"... {len(blocked) - MAX_TEXT_BLOCKED_PULLS} blocked pull requests omitted"
        )
    suggestions = plan["suggestions"]
    for value in suggestions[:MAX_TEXT_SUGGESTIONS]:
        lines.append(
            f"Suggestion #{value['left']} <-> #{value['right']}: "
            f"{value['file_count']} overlapping file(s), not authoritative"
        )
    if len(suggestions) > MAX_TEXT_SUGGESTIONS:
        lines.append(
            f"... {len(suggestions) - MAX_TEXT_SUGGESTIONS} suggestions omitted"
        )
    return "\n".join(lines)
