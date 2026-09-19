"""Bounded scan previews and explicit local scan commands."""

from fastapi import APIRouter
from fastapi import Response as HTTPResponse
from pydantic import BaseModel, ConfigDict, Field

from reposteward.tasks.local_operations import OperationError
from reposteward.web.api.operations import Key, Operation, ProjectID, envelope
from reposteward.web.api.schemas import Record, Response


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    binding_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    rebuild: bool = False


class ScanPlan(Record):
    project_id: str
    binding_id: str
    root: str
    revision: str
    source_digest: str
    rebuild: bool
    state: Record
    coverage: Record
    limits: Record


def routes(service, *, manage_local):
    router = APIRouter(prefix="/api/v1")

    def current(command=False):
        if service is None or (command and not manage_local):
            raise OperationError("read_only", "当前会话仅允许读取。", 403)
        service.workbench.check_configuration()
        return service.scans

    @router.get(
        "/scan-plan", response_model=Response[ScanPlan], operation_id="scanPlan"
    )
    def preview(project_id: ProjectID, binding_id: ProjectID, rebuild: bool = False):
        return envelope(current().preview(project_id, binding_id, rebuild=rebuild))

    @router.post(
        "/commands/workspaces/scan",
        response_model=Response[Operation],
        status_code=202,
        operation_id="scanWorkspace",
    )
    def scan(request: ScanRequest, key: Key, response: HTTPResponse):
        result = current(True).enqueue(**request.model_dump(), key=key)
        response.headers["Location"] = "/api/v1/operation?operation_id=" + result["id"]
        return envelope(result)

    return router
