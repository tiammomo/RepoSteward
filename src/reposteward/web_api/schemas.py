"""Public workbench DTOs; historical protocol payloads remain versioned JSON."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Record(BaseModel):
    # Workbench already curates these domain records. Preserve provenance and
    # coverage extensions instead of silently discarding historical evidence.
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class Meta(BaseModel):
    request_id: str
    generated_at: str
    api_version: Literal["1"] = "1"


class Response[T](BaseModel):
    data: T
    meta: Meta


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str = ""
    retryable: bool = False


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Policy(BaseModel):
    mode: str
    enabled: bool
    task_access: bool


class WorkspaceRef(BaseModel):
    id: str
    root: str


class Project(Record):
    id: str
    identity: str
    host: str
    repository: str
    name: str
    created_at: str
    workspace_count: int = 0
    missing_workspaces: int = 0
    workspace_check_omitted: int = 0
    workspaces: list[WorkspaceRef] = Field(default_factory=list)
    policy: Policy


class Projects(Record):
    projects: list[Project]
    omitted: int
    public_write: Literal[False] = False


class Attention(Record):
    id: str
    priority: int
    summary: str
    reason_code: str
    run_id: str = ""
    pull_number: int = 0
    issue_number: int = 0
    next_command: str = ""
    source_updated_at: str = ""


class SourceStatus(Record):
    status: str
    fetched_at: str = ""
    error: str = ""


class OverviewProject(Record):
    project_id: str
    repository: str
    complete: bool
    items: list[Attention]
    omitted: int
    sources: SourceStatus


class Overview(Record):
    projects: list[OverviewProject]
    complete: bool
    omitted_projects: int
    excluded_projects: int
    observed_at: str
    overview_digest: str


class Source(Record):
    evidence_id: str
    path: str
    line: int
    end_line: int
    digest: str
    trust: str
    url: str = ""


class ReadingStep(Record):
    step: int
    path: str
    reason: str
    summary: str = ""
    language: str
    source: Source


class Guide(Record):
    status: str
    repository: str
    reading_path: list[ReadingStep] = Field(default_factory=list)
    scanned_at: str = ""
    limitations: list[str] = Field(default_factory=list)


class WorkspaceState(BaseModel):
    head: str
    branch: str
    dirty: bool


class Commands(BaseModel):
    scan: str
    mcp_clients: dict[str, str]


class Workspace(Record):
    project: dict[str, JsonValue]
    binding: dict[str, JsonValue]
    workspace: WorkspaceState
    policy: Policy
    guide: Guide
    commands: Commands
    command_shell: str


class Code(Record):
    evidence_id: str
    status: str
    text: str
    source: Source | None = None
    truncated: bool = False


class TaskAttempt(BaseModel):
    id: str
    issue_number: int
    stage: str
    status: str
    updated_at: str
    title: str | None


class Tasks(Record):
    tasks: list[TaskAttempt]
    omitted: int
    status: str


class Task(Record):
    run_id: str
    kind: Literal["external", "managed"]
    context: dict[str, JsonValue] | None
    context_status: str
    current_applicability: str
    command: str


class Review(Record):
    run_id: str
    trace: dict[str, JsonValue]
    verification: dict[str, JsonValue] | None
    decision_freshness: Literal["historical_only"]
    publication_eligible: Literal[False]


class Settings(Record):
    installation: dict[str, JsonValue]
    configuration: dict[str, JsonValue]
    databases: dict[str, JsonValue]
    next_actions: list[str]


class Session(BaseModel):
    capabilities: list[Literal["read_local"]]
    expires_in_seconds: int
    api_version: Literal["1"] = "1"
    frontend_digest: str
