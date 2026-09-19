"""Versioned local command/query routes; no GitHub mutation endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from reposteward.tasks.local_operations import LocalOperations, OperationError
from reposteward.web.api.schemas import Record, Response

ProjectID = Annotated[str, Query(pattern=r"^[0-9a-f]{32}$")]
Key = Annotated[
    str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._:-]{1,128}$")
]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class Operation(Record):
    id: str
    project_id: str
    import_id: str = ""
    binding_id: str = ""
    action: str
    repository: str
    state: Literal["pending", "running", "completed", "failed", "cancelled"]
    revision: str
    attempt_count: int
    max_attempts: int
    created_at: str
    updated_at: str
    available_at: str
    last_error_code: str
    can_cancel: bool
    can_retry: bool
    stages: list[dict[str, JsonValue]]
    attempts: list[dict[str, JsonValue]]


class OperationList(BaseModel):
    items: list[Operation]
    next_before: int


class GitHubItem(Record):
    number: int
    title: str
    state: Literal["open", "closed", "merged"]
    author: str
    url: str
    updated_at: str
    observed_at: str
    head_sha: str
    checks: dict[str, JsonValue] | None = None


class GitHubView(Record):
    project_id: str
    repository: str
    kind: str
    items: list[GitHubItem]
    sources: list[dict[str, JsonValue]]
    snapshot: str
    next_cursor: str
    has_more_remote: bool
    total: int | None
    observed_count: int
    never_synced: bool
    coverage: str


def envelope(data):
    return {
        "data": data,
        "meta": {
            "request_id": uuid.uuid4().hex,
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def routes(service: LocalOperations | None, *, manage_local: bool) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def current(*, command=False):
        if service is None or (command and not manage_local):
            raise OperationError("read_only", "当前会话仅允许读取。", 403)
        service.workbench.check_configuration()
        return service

    @router.get("/github", response_model=Response[GitHubView], operation_id="github")
    def github(
        project_id: ProjectID,
        kind: Literal["pulls", "issues", "activity"] = "pulls",
        cursor: Annotated[str, Query(max_length=100)] = "",
        number: Annotated[int, Query(ge=0, le=999999999)] = 0,
    ):
        return envelope(
            current().github(project_id, kind=kind, cursor=cursor, number=number)
        )

    @router.get(
        "/operations", response_model=Response[OperationList], operation_id="operations"
    )
    def operations(
        project_id: Annotated[str, Query(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
        before: Annotated[int, Query(ge=0)] = 0,
    ):
        return envelope(current().listing(project_id, before))

    @router.get(
        "/operation", response_model=Response[Operation], operation_id="operation"
    )
    def operation(operation_id: ProjectID):
        return envelope(current().operation(operation_id))

    @router.post(
        "/commands/github/sync",
        response_model=Response[Operation],
        status_code=202,
        operation_id="syncGitHub",
    )
    def sync(request: SyncRequest, key: Key):
        return envelope(current(command=True).sync(request.project_id, key))

    @router.post(
        "/commands/operations/cancel",
        response_model=Response[Operation],
        status_code=202,
        operation_id="cancelOperation",
    )
    def cancel(request: ControlRequest, key: Key):
        return envelope(
            current(command=True).control(
                request.operation_id, "cancel", request.expected_revision, key
            )
        )

    @router.post(
        "/commands/operations/retry",
        response_model=Response[Operation],
        status_code=202,
        operation_id="retryOperation",
    )
    def retry(request: ControlRequest, key: Key):
        return envelope(
            current(command=True).control(
                request.operation_id, "retry", request.expected_revision, key
            )
        )

    return router
