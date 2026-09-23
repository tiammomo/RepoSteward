"""FastAPI queries with an exact loopback authority and bounded ASGI transport."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from reposteward.context.budget import ContextBudgetError
from reposteward.projects.registry import ProjectError
from reposteward.tasks.external import TaskConflict
from reposteward.tasks.local_operations import LocalOperations, OperationError
from reposteward.web.api import schemas as dto
from reposteward.web.api.assets import load_assets
from reposteward.web.api.imports import routes as import_routes
from reposteward.web.api.operations import routes
from reposteward.web.api.scans import routes as scan_routes
from reposteward.web.api.tasks import routes as task_routes
from reposteward.web.workbench import Workbench

COMMAND_PATHS = {
    "/api/v1/commands/tasks/handoff",
    "/api/v1/commands/tasks/acknowledge",
    "/api/v1/commands/tasks/verify",
    "/api/v1/commands/workspaces/scan",
    "/api/v1/commands/projects/inspect",
    "/api/v1/commands/projects/plan",
    "/api/v1/commands/projects/apply",
    "/api/v1/commands/github/sync",
    "/api/v1/commands/operations/cancel",
    "/api/v1/commands/operations/retry",
}
MAX_RESPONSE_BYTES = 400_000
SESSION_SECONDS = 12 * 60 * 60
ID = Annotated[str, Query(pattern=r"^[0-9a-f]{32}$")]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'",
}
LEGACY = {
    "/api/overview": ("overview", (), ()),
    "/api/projects": ("projects", (), ()),
    "/api/workspace": ("workspace", ("project_id", "binding_id"), ("focus",)),
    "/api/code": ("code", ("project_id", "binding_id", "evidence_id"), ("start_line",)),
    "/api/tasks": ("tasks", ("project_id",), ()),
    "/api/task": ("task", ("project_id", "run_id"), ()),
    "/api/review": ("review", ("project_id", "run_id"), ()),
    "/api/settings": ("settings", (), ()),
}
UI_ROUTE = re.compile(
    r"/(?:imports(?:/[0-9a-f]{32})?|projects(?:/[0-9a-f]{32}(?:/(?:workspaces/[0-9a-f]{32}|"
    r"github|tasks(?:/[0-9a-f]{32}(?:/review)?)?))?)?|operations(?:/[0-9a-f]{32})?|settings|tasks|review)?"
)


@dataclass
class LocalSession:
    authority: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: time.monotonic() + SESSION_SECONDS)

    @property
    def origin(self) -> str:
        return "http://" + self.authority


def error(status: int, code: str, message: str, request_id: str = "") -> JSONResponse:
    return JSONResponse(
        dto.ErrorResponse(
            error=dto.ErrorDetail(code=code, message=message, request_id=request_id)
        ).model_dump(),
        status_code=status,
        headers=SECURITY_HEADERS,
    )


class Boundary:
    def __init__(self, app, *, session: LocalSession, manage_local: bool = False):
        self.app, self.session = app, session
        self.manage_local = manage_local
        self.slots = BoundedSemaphore(4)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)
        headers = scope["headers"]

        def values(name):
            return [v for k, v in headers if k == name]

        async def reject(status, code, message):
            await error(status, code, message)(scope, receive, send)

        if (
            scope.get("client", ("127.0.0.1",))[0] != "127.0.0.1"
            or values(b"host") != [self.session.authority.encode()]
            or values(b"origin") not in ([], [self.session.origin.encode()])
            or values(b"sec-fetch-site") not in ([], [b"none"], [b"same-origin"])
        ):
            return await reject(403, "origin", "仅接受当前本地工作台的请求。")
        raw = scope.get("raw_path", b"")
        if (
            len(raw) + len(scope["query_string"]) > 4096
            or sum(len(k) + len(v) for k, v in headers) > 16_000
            or b"%" in raw
            or b"\\" in raw
            or not raw.startswith(b"/")
            or raw.startswith(b"//")
        ):
            return await reject(400, "invalid_request", "请求超出读取边界。")
        command = scope["method"] == "POST" and scope["path"] in COMMAND_PATHS
        if scope["method"] != "GET" and not (command and self.manage_local):
            return await reject(
                405, "read_only", "此版本提供读取，请使用已有 CLI 执行操作。"
            )
        if not command and (
            values(b"transfer-encoding")
            or values(b"content-length") not in ([], [b"0"])
        ):
            return await reject(400, "invalid_request", "读取请求不能包含请求体。")
        if scope["path"].startswith("/api/"):
            auth = values(b"authorization")
            if (
                len(auth) != 1
                or not hmac.compare_digest(
                    auth[0], ("Bearer " + self.session.token).encode()
                )
                or time.monotonic() >= self.session.expires
            ):
                return await reject(
                    401, "session", "请使用终端打印的本地会话链接重新连接。"
                )
        if command:
            if values(b"origin") != [self.session.origin.encode()]:
                return await reject(403, "origin", "操作必须来自当前本地工作台。")
            content_type = values(b"content-type")
            if (
                len(content_type) != 1
                or content_type[0].split(b";")[0].strip().lower() != b"application/json"
            ):
                return await reject(415, "content_type", "操作请求需要 JSON。")
            keys = values(b"idempotency-key")
            if len(keys) != 1 or not re.fullmatch(rb"[A-Za-z0-9._:-]{1,128}", keys[0]):
                return await reject(400, "idempotency_key", "操作缺少有效的请求编号。")
        if not self.slots.acquire(blocking=False):
            return await reject(429, "busy", "本地读取繁忙，请稍后重试。")
        start = None
        body = bytearray()
        too_large = False

        async def bounded_send(message):
            nonlocal start, too_large
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                limit = (
                    MAX_RESPONSE_BYTES
                    if scope["path"].startswith("/api/")
                    else 2_000_000
                )
                if len(body) + len(chunk) > limit:
                    too_large = True
                if not too_large:
                    body.extend(chunk)
                if not message.get("more_body", False):
                    if too_large:
                        await reject(
                            400,
                            "response_limit",
                            "读取结果超出范围，请缩小选择或使用 CLI。",
                        )
                    else:
                        replaced = {key.lower().encode() for key in SECURITY_HEADERS}
                        start["headers"] = [
                            (k, v)
                            for k, v in start["headers"]
                            if k.lower() not in replaced
                        ]
                        start["headers"].extend(
                            (k.encode(), v.encode())
                            for k, v in SECURITY_HEADERS.items()
                        )
                        await send(start)
                        await send({"type": "http.response.body", "body": bytes(body)})

        try:
            if command:
                incoming = bytearray()
                try:
                    async with asyncio.timeout(5):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            incoming.extend(message.get("body", b""))
                            if len(incoming) > 64_000:
                                return await reject(
                                    413, "request_limit", "操作请求超出大小限制。"
                                )
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    return await reject(408, "request_timeout", "操作请求未及时完成。")
                original_receive = receive
                delivered = False

                async def receive_body():
                    nonlocal delivered
                    if delivered:
                        return await original_receive()
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(incoming),
                        "more_body": False,
                    }

                receive = receive_body
            await self.app(scope, receive, bounded_send)
        finally:
            self.slots.release()


def create_app(
    workbench: Workbench | None = None,
    *,
    session: LocalSession | None = None,
    manage_local: bool = False,
    operations: LocalOperations | None = None,
) -> FastAPI:
    """Construct routes without authentication, database initialization or network."""
    session = session or LocalSession("127.0.0.1:0")
    service = operations or (LocalOperations(workbench) if workbench else None)

    @asynccontextmanager
    async def lifespan(app):
        if manage_local and service:
            service.start()
        try:
            yield
        finally:
            if manage_local and service:
                await run_in_threadpool(service.stop)

    app = FastAPI(
        title="RepoSteward local workbench",
        lifespan=lifespan,
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.workbench = workbench
    app.state.session = session

    async def failed(request: Request, exc: Exception):
        if isinstance(exc, OperationError):
            return error(exc.status, exc.code, str(exc))
        if isinstance(exc, ContextBudgetError):
            return error(
                400,
                "context_budget",
                "必要任务信息超出预算，请提高上下文预算后重新预览。",
            )
        if isinstance(exc, TaskConflict):
            return error(409, "changed", "配置或工作区已变化，请核对后重新打开工作台。")
        if isinstance(exc, (ProjectError, OSError)) and (
            request.url.path.startswith("/api/v1/commands/projects/")
            or request.url.path == "/api/v1/import"
        ):
            from reposteward.projects.imports import import_problem

            code, message = import_problem(exc)
            return error(409, code, message)
        if isinstance(exc, ProjectError):
            return error(
                409, "binding_or_policy", "项目绑定或策略不可用，请检查关联与配置。"
            )
        if isinstance(exc, KeyError):
            return error(404, "not_found", "所选对象不存在，或不属于当前项目。")
        if isinstance(exc, (ValueError, RequestValidationError)):
            return error(400, "invalid_request", "请求参数不可用或超出读取范围。")
        if isinstance(exc, HTTPException):
            return error(exc.status_code, "not_found", "没有这个读取入口。")
        return error(503, "unavailable", "本地数据暂不可读，请检查设置诊断。")

    for cls in (
        OperationError,
        TaskConflict,
        ProjectError,
        KeyError,
        ValueError,
        RequestValidationError,
        HTTPException,
        ResponseValidationError,
        OSError,
        RuntimeError,
        sqlite3.Error,
    ):
        app.add_exception_handler(cls, failed)

    def read(method, **kwargs):
        if workbench is None:
            raise RuntimeError("no runtime service configured")
        workbench.check_configuration()
        result = getattr(workbench, method)(**kwargs)
        workbench.check_configuration()
        return {
            "data": result,
            "meta": {
                "request_id": uuid.uuid4().hex,
                "generated_at": datetime.now(UTC).isoformat(),
                "api_version": "1",
            },
        }

    @app.get(
        "/api/v1/session",
        response_model=dto.Response[dto.Session],
        operation_id="session",
    )
    def current_session():
        _, digest = load_assets()
        return {
            "data": {
                "capabilities": ["read_local", "manage_local"]
                if manage_local
                else ["read_local"],
                "expires_in_seconds": max(0, int(session.expires - time.monotonic())),
                "frontend_digest": digest,
            },
            "meta": {
                "request_id": uuid.uuid4().hex,
                "generated_at": datetime.now(UTC).isoformat(),
            },
        }

    @app.get(
        "/api/v1/projects",
        response_model=dto.Response[dto.Projects],
        operation_id="projects",
    )
    def projects():
        return read("projects")

    @app.get(
        "/api/v1/overview",
        response_model=dto.Response[dto.Overview],
        operation_id="overview",
    )
    def overview():
        return read("overview")

    @app.get(
        "/api/v1/workspace",
        response_model=dto.Response[dto.Workspace],
        operation_id="workspace",
    )
    def workspace(
        project_id: ID,
        binding_id: ID,
        focus: Annotated[str, Query(max_length=2000)] = "",
    ):
        return read(
            "workspace", project_id=project_id, binding_id=binding_id, focus=focus
        )

    @app.get("/api/v1/code", response_model=dto.Response[dto.Code], operation_id="code")
    def code(
        project_id: ID,
        binding_id: ID,
        evidence_id: Annotated[str, Query(pattern=r"^code:[0-9a-f]{64}$")],
        start_line: Annotated[str, Query(pattern=r"^[1-9][0-9]{0,6}$")] = "1",
    ):
        return read(
            "code",
            project_id=project_id,
            binding_id=binding_id,
            evidence_id=evidence_id,
            start_line=start_line,
        )

    @app.get(
        "/api/v1/tasks", response_model=dto.Response[dto.Tasks], operation_id="tasks"
    )
    def tasks(project_id: ID):
        return read("tasks", project_id=project_id)

    @app.get("/api/v1/task", response_model=dto.Response[dto.Task], operation_id="task")
    def task(project_id: ID, run_id: ID):
        return read("task", project_id=project_id, run_id=run_id)

    @app.get(
        "/api/v1/review", response_model=dto.Response[dto.Review], operation_id="review"
    )
    def review(project_id: ID, run_id: ID):
        return read("review", project_id=project_id, run_id=run_id)

    @app.get(
        "/api/v1/settings",
        response_model=dto.Response[dto.Settings],
        operation_id="settings",
    )
    def settings():
        return read("settings")

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    def schema():
        return app.openapi()

    app.include_router(routes(service, manage_local=manage_local))
    app.include_router(import_routes(service, manage_local=manage_local))
    app.include_router(scan_routes(service, manage_local=manage_local))
    app.include_router(task_routes(service, manage_local=manage_local))

    @app.get("/{path:path}", include_in_schema=False)
    async def fallback(request: Request, path: str):
        route = "/" + path
        if route in LEGACY:
            method, required, optional = LEGACY[route]
            pairs = list(request.query_params.multi_items())
            params = dict(pairs)
            if (
                len(params) != len(pairs)
                or not set(required).issubset(params)
                or not set(params).issubset((*required, *optional))
                or any(len(v) > 2000 for v in params.values())
            ):
                raise ValueError("invalid query")
            result = await run_in_threadpool(read, method, **params)
            return JSONResponse(result["data"])
        assets, _ = load_assets()
        if not request.url.query and route in assets:
            body, mime = assets[route]
            return Response(body, media_type=mime)
        if UI_ROUTE.fullmatch(route):
            return Response(assets["/"][0], media_type="text/html")
        raise HTTPException(404)

    # FastAPI otherwise silently ignores unknown / repeated query parameters.
    @app.middleware("http")
    async def strict_queries(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/v1/"):
            legacy = LEGACY.get(path.replace("/api/v1/", "/api/", 1))
            allowed = (
                (*legacy[1], *legacy[2])
                if legacy
                else {
                    "/api/v1/github": ("project_id", "kind", "cursor", "number"),
                    "/api/v1/operations": ("project_id", "before"),
                    "/api/v1/operation": ("operation_id",),
                    "/api/v1/import": ("import_id",),
                    "/api/v1/task-preview": ("project_id", "run_id", "budget"),
                    "/api/v1/handoffs": ("project_id", "run_id"),
                    "/api/v1/handoff": ("project_id", "run_id", "handoff_id"),
                    "/api/v1/scan-plan": ("project_id", "binding_id", "rebuild"),
                }.get(path, ())
            )
            pairs = list(request.query_params.multi_items())
            if len(dict(pairs)) != len(pairs) or any(
                k not in allowed for k, _ in pairs
            ):
                return error(400, "invalid_request", "请求包含未知或重复参数。")
        return await call_next(request)

    app.add_middleware(Boundary, session=session, manage_local=manage_local)
    return app


if __name__ == "__main__":
    print(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True)
    )
