"""Source-bound requirements and explicit operator review.

An unreviewed Issue stays complete in the mandatory contract. Shorter requirements
are accepted only with an explicit review tied to that exact Issue version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .models import Issue


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def issue_digest(issue: Issue) -> str:
    return digest(
        {
            key: getattr(issue, key)
            for key in ("repository", "number", "title", "body", "updated_at")
        }
    )


@dataclass(frozen=True, slots=True)
class TaskContract:
    schema_version: int
    goal: str
    acceptance_criteria: tuple[str, ...]
    scope_boundaries: tuple[str, ...]
    source_requirements: str
    source_digest: str
    source_locator: str
    source_updated_at: str
    review_status: str
    reviewed_by: str
    supersedes: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def source_contract(issue: Issue) -> TaskContract:
    material = {
        "schema_version": 1,
        "goal": issue.title,
        "acceptance_criteria": (),
        "scope_boundaries": (),
        "source_requirements": issue.body,
        "source_digest": issue_digest(issue),
        "source_locator": issue.url,
        "source_updated_at": issue.updated_at,
        "review_status": "source_bound",
        "reviewed_by": "",
        "supersedes": "",
    }
    return TaskContract(**material, digest=digest(material))


def review_contract(
    issue: Issue, proposal: dict[str, Any], *, reviewed_by: str
) -> TaskContract:
    """Caller supplies the configured operator identity through an explicit review."""
    if not reviewed_by.strip() or len(reviewed_by) > 100:
        raise ValueError("task contract requires an operator reviewer")
    if set(proposal) - {
        "goal",
        "acceptance_criteria",
        "scope_boundaries",
        "source_digest",
        "supersedes",
    }:
        raise ValueError("unknown task contract proposal fields")
    if proposal.get("source_digest") != issue_digest(issue):
        raise ValueError("task contract source changed; review the current Issue")
    goal = proposal.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise ValueError("task contract needs a bounded goal")
    lists = {}
    for key in ("acceptance_criteria", "scope_boundaries"):
        values = proposal.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > 96
            or any(
                not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in values
            )
        ):
            raise ValueError(f"invalid task contract {key}")
        lists[key] = tuple(values)
    if not lists["acceptance_criteria"]:
        raise ValueError("reviewed task contract needs acceptance criteria")
    supersedes = proposal.get("supersedes", "")
    if not isinstance(supersedes, str) or (
        supersedes
        and (
            len(supersedes) != 64
            or any(c not in "0123456789abcdef" for c in supersedes)
        )
    ):
        raise ValueError("invalid superseded contract digest")
    material = dict(
        schema_version=1,
        goal=goal,
        **lists,
        source_requirements="",
        source_digest=issue_digest(issue),
        source_locator=issue.url,
        source_updated_at=issue.updated_at,
        review_status="operator_reviewed",
        reviewed_by=reviewed_by,
        supersedes=supersedes,
    )
    return TaskContract(**material, digest=digest(material))


def validate_contract(value: dict[str, Any], source: dict[str, Any]) -> None:
    if value["digest"] != digest(
        {key: item for key, item in value.items() if key != "digest"}
    ):
        raise ValueError("task contract digest is inconsistent")
    if (
        value["source_digest"] != source["digest"]
        or value["source_locator"] != source["locator"]
        or value["source_updated_at"] != source["updated_at"]
    ):
        raise ValueError("task contract is bound to a different Issue version")
    if value["review_status"] == "source_bound" and value["reviewed_by"]:
        raise ValueError("source-bound requirements cannot claim operator review")
    if value["review_status"] == "operator_reviewed" and (
        not value["reviewed_by"]
        or not value["acceptance_criteria"]
        or value["source_requirements"]
    ):
        raise ValueError("reviewed task contract is incomplete")
