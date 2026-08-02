from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    full_name: str
    default_branch: str
    stars: int
    forks: int
    open_issues: int
    pushed_at: str
    archived: bool
    is_fork: bool
    license_spdx: str | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    repository: str
    number: int
    node_id: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    comments: int
    created_at: str
    updated_at: str
    author_login: str
    author_association: str
    state: str = "open"
    assignees: tuple[str, ...] = ()
    locked: bool = False


@dataclass(frozen=True, slots=True)
class Candidate:
    issue: Issue
    repository: RepositoryInfo
    score: float
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    discovered_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.issue.repository}#{self.issue.number}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Candidate:
        issue_data = dict(value["issue"])
        issue_data["labels"] = tuple(issue_data.get("labels", ()))
        issue_data["assignees"] = tuple(issue_data.get("assignees", ()))
        repo_data = dict(value["repository"])
        return cls(
            issue=Issue(**issue_data),
            repository=RepositoryInfo(**repo_data),
            score=float(value["score"]),
            reasons=tuple(value.get("reasons", ())),
            blockers=tuple(value.get("blockers", ())),
            discovered_at=str(value.get("discovered_at", "")),
        )


@dataclass(frozen=True, slots=True)
class AgentResult:
    summary: str
    pr_title: str
    implementation_notes: str
    verification_commands: tuple[str, ...]
    tests_observed: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentResult:
        return cls(
            summary=str(value["summary"]).strip(),
            pr_title=str(value["pr_title"]).strip(),
            implementation_notes=str(value["implementation_notes"]).strip(),
            verification_commands=tuple(str(x) for x in value["verification_commands"]),
            tests_observed=tuple(str(x) for x in value.get("tests_observed", ())),
            risks=tuple(str(x) for x in value.get("risks", ())),
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int
    output: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    commands: tuple[CommandResult, ...]
    reason: str = ""


@dataclass(slots=True)
class PreparedChange:
    repository: str
    issue_number: int
    worktree: str
    base_branch: str
    branch: str
    changed_files: list[str]
    added_lines: int
    deleted_lines: int
    agent_result: AgentResult
    verification: VerificationResult
    commit_sha: str = ""
    pr_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
