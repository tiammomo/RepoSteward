"""Bounded task context, immutable handoffs and explicit verification commands."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from reposteward.tasks.handoffs import TaskHandoffs
from reposteward.tasks.local_operations import OperationError
from reposteward.web.api.operations import Key, Operation, ProjectID, envelope
from reposteward.web.api.schemas import Record, Response


class TaskPreview(Record):
    project_id: str
    run_id: str
    kind: str
    context: dict[str, JsonValue]
    authority: dict[str, JsonValue]
    plan_digest: str
    budget: int
    estimated_tokens: int
    export_available: bool
    verification_available: bool
    validity: list[str]
    profiles: list[dict[str, JsonValue]]
    coverage: list[dict[str, JsonValue]]


class Handoff(Record):
    id: str
    project_id: str
    run_id: str
    client: str
    digest: str
    created_at: str
    package_state: str
    current_applicability: str
    acknowledgement: dict[str, JsonValue] | None
    execution_observed: bool
    verification_granted: bool
    content: dict[str, JsonValue] | None


class HandoffList(BaseModel):
    items: list[Handoff]
    has_more: bool


class TaskPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    budget: int = Field(ge=512, le=100000, strict=True)
    expected_plan: str = Field(pattern=r"^[0-9a-f]{64}$")


class HandoffRequest(TaskPlanRequest):
    client: Literal["codex", "claude-code", "copilot-vscode"]


class TaskVerificationRequest(TaskPlanRequest):
    profile: str = Field(min_length=1, max_length=128)


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    handoff_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def routes(local, *, manage_local):
    router = APIRouter(prefix="/api/v1")

    def current(*, command=False):
        if local is None or (command and not manage_local):
            raise OperationError("read_only", "当前会话仅允许读取。", 403)
        local.workbench.check_configuration()
        return TaskHandoffs(local)

    @router.get(
        "/task-preview",
        response_model=Response[TaskPreview],
        operation_id="taskPreview",
    )
    def preview(
        project_id: ProjectID,
        run_id: ProjectID,
        budget: Annotated[int, Query(ge=512, le=100000)] = 24000,
    ):
        return envelope(current().preview(project_id, run_id, budget))

    @router.get(
        "/handoffs", response_model=Response[HandoffList], operation_id="handoffs"
    )
    def handoffs(project_id: ProjectID, run_id: ProjectID):
        return envelope(current().listing(project_id, run_id))

    @router.get("/handoff", response_model=Response[Handoff], operation_id="handoff")
    def handoff(project_id: ProjectID, run_id: ProjectID, handoff_id: ProjectID):
        return envelope(current().get(project_id, run_id, handoff_id, content=True))

    @router.post(
        "/commands/tasks/handoff",
        response_model=Response[Handoff],
        operation_id="exportHandoff",
    )
    def export(request: HandoffRequest, key: Key):
        return envelope(current(command=True).export(**request.model_dump(), key=key))

    @router.post(
        "/commands/tasks/acknowledge",
        response_model=Response[Handoff],
        operation_id="acknowledgeHandoff",
    )
    def acknowledge(request: ReceiptRequest, key: Key):
        # Receipt identity is the immutable handoff ID; repeat confirmation is a no-op.
        return envelope(current(command=True).acknowledge(**request.model_dump()))

    @router.post(
        "/commands/tasks/verify",
        response_model=Response[Operation],
        status_code=202,
        operation_id="verifyTask",
    )
    def verify(request: TaskVerificationRequest, key: Key):
        return envelope(current(command=True).verify(**request.model_dump(), key=key))

    return router
