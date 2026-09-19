"""Project import commands retain the same authenticated local boundary."""

from typing import Literal

from fastapi import APIRouter
from fastapi import Response as HTTPResponse
from pydantic import BaseModel, ConfigDict, Field

from reposteward.tasks.local_operations import OperationError
from reposteward.web.api.operations import Key, Operation, ProjectID, envelope
from reposteward.web.api.schemas import Record, Response


class ImportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["github_url", "local_path"]
    value: str = Field(min_length=1, max_length=4096)


class ImportPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    inspection_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    method: Literal["link", "clone", "watch"]
    purpose: Literal["maintain", "contribute", "watch"]
    target: str = Field(default="", max_length=4096)


class ImportApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportPreview(Record):
    id: str
    digest: str
    plan: Record


class ImportView(Record):
    id: str
    source: ImportSource
    inspection: Record | None
    inspection_id: str
    preview: Record | None
    preview_id: str
    operations: list[Operation]


def routes(service, *, manage_local):
    router = APIRouter(prefix="/api/v1")

    def current(command=False):
        if service is None or (command and not manage_local):
            raise OperationError("read_only", "当前会话仅允许读取。", 403)
        service.workbench.check_configuration()
        return service.imports

    @router.get(
        "/import", response_model=Response[ImportView], operation_id="projectImport"
    )
    def show(import_id: ProjectID):
        return envelope(current().show(import_id))

    @router.post(
        "/commands/projects/inspect",
        response_model=Response[Operation],
        status_code=202,
        operation_id="inspectProject",
    )
    def inspect(request: ImportSource, key: Key, response: HTTPResponse):
        result = current(True).inspect(request.kind, request.value, key)
        response.headers["Location"] = "/api/v1/operation?operation_id=" + result["id"]
        return envelope(result)

    @router.post(
        "/commands/projects/plan",
        response_model=Response[ImportPreview],
        operation_id="planProject",
    )
    def plan(request: ImportPlanRequest, key: Key):
        return envelope(current(True).plan(**request.model_dump(), key=key))

    @router.post(
        "/commands/projects/apply",
        response_model=Response[Operation],
        status_code=202,
        operation_id="applyProject",
    )
    def apply(request: ImportApplyRequest, key: Key, response: HTTPResponse):
        result = current(True).apply(**request.model_dump(), key=key)
        response.headers["Location"] = "/api/v1/operation?operation_id=" + result["id"]
        return envelope(result)

    return router
