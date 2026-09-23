"""Optional localhost HTTP+JSON adapter using stable A2A 1.0 models."""

from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import asynccontextmanager
from threading import Event, Thread
from urllib.parse import urlsplit

import anyio
from a2a.types import CancelTaskRequest, GetTaskRequest
from a2a.utils.error_handlers import build_rest_error_payload
from a2a.utils.errors import (
    A2AError,
    InternalError,
    InvalidParamsError,
    TaskNotFoundError,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from fastapi import FastAPI, Request
from google.protobuf.json_format import ParseDict, ParseError
from starlette.responses import JSONResponse

from reposteward.integrations.a2a.reports import ReportAgent
from reposteward.projects.registry import ProjectError
from reposteward.tasks.local_operations import OperationError


def create_app(service, *, origin: str, token: str, run_worker: bool = True):
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("A2A requires an exact localhost origin")
    if len(token) < 43:
        raise ValueError("a private bearer token is required")
    agent = ReportAgent(service, origin=origin, token=token)
    stopping = Event()

    def work():
        while not stopping.is_set():
            try:
                progressed = service.process_once(actions=("assistance.understanding",))
            except (RuntimeError, ValueError, OSError, sqlite3.Error):
                progressed = False
            if not progressed:
                stopping.wait(0.5)

    @asynccontextmanager
    async def lifespan(_app):
        thread = Thread(target=work, daemon=True)
        if run_worker:
            thread.start()
        try:
            yield
        finally:
            stopping.set()
            if run_worker:
                await anyio.to_thread.run_sync(thread.join, 5)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    limiter = anyio.CapacityLimiter(8)

    def response(value, status=200):
        return JSONResponse(
            value,
            status_code=status,
            media_type="application/a2a+json",
            headers={"A2A-Version": "1.0", "Cache-Control": "no-store"},
        )

    def error(exc):
        payload = build_rest_error_payload(exc)
        return response(payload, payload["error"]["code"])

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if (
            request.headers.get("host") != parsed.netloc
            or request.headers.get("origin", origin) != origin
        ):
            return response(
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "message": "Local origin required.",
                    }
                },
                403,
            )
        card = (
            request.url.path == "/.well-known/agent-card.json"
            and request.method == "GET"
        )
        if not card and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + token).encode(),
        ):
            result = response(
                {
                    "error": {
                        "code": 401,
                        "status": "UNAUTHENTICATED",
                        "message": "Bearer authentication required.",
                    }
                },
                401,
            )
            result.headers["WWW-Authenticate"] = "Bearer"
            return result
        if (
            not card
            and request.headers.get(
                "a2a-version", request.query_params.get("A2A-Version", "0.3")
            )
            != "1.0"
        ):
            return error(VersionNotSupportedError())
        if request.headers.get("a2a-extensions"):
            return error(UnsupportedOperationError())
        try:
            return await call_next(request)
        except A2AError as exc:
            return error(exc)
        except (KeyError, ProjectError):
            return error(TaskNotFoundError())
        except (ValueError, ParseError):
            return error(
                InvalidParamsError(
                    message="Invalid request or stale operation parameters."
                )
            )
        except (RuntimeError, OSError, sqlite3.Error, OperationError):
            return error(
                InternalError(
                    message="Unable to establish a scoped result. Inspect local operation records."
                )
            )

    async def invoke(function, *args):
        return await anyio.to_thread.run_sync(function, *args, limiter=limiter)

    async def body(request):
        if request.headers.get("content-type", "").split(";", 1)[0] not in {
            "application/json",
            "application/a2a+json",
        }:
            from a2a.utils.errors import ContentTypeNotSupportedError

            raise ContentTypeNotSupportedError()
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 64000:
                raise InvalidParamsError(message="Request exceeds the supported size.")
        result = json.loads(data or b"{}")
        if not isinstance(result, dict):
            raise InvalidParamsError()
        return result

    @app.get("/.well-known/agent-card.json")
    async def card():
        return response(agent.card())

    @app.post("/message:send")
    async def send(request: Request):
        task, immediate = await invoke(agent.send, await body(request))
        while not immediate and task["status"]["state"] in {
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
        }:
            if await request.is_disconnected():
                break  # The persisted job continues independently of this connection.
            await anyio.sleep(0.25)
            task = agent.task(await invoke(agent.operation, task["id"]))
        return response({"task": task})

    @app.get("/tasks")
    async def listing(request: Request):
        params = {k: v for k, v in request.query_params.items() if k != "A2A-Version"}
        return response(await invoke(agent.listing, params))

    @app.api_route("/tasks/{identifier}:subscribe", methods=["GET", "POST"])
    async def subscribe(identifier: str):
        return error(UnsupportedOperationError())

    @app.get("/tasks/{identifier}")
    async def get(identifier: str, request: Request):
        params = {k: v for k, v in request.query_params.items() if k != "A2A-Version"}
        value = ParseDict({**params, "id": identifier}, GetTaskRequest())
        if value.history_length < 0 or value.tenant:
            raise InvalidParamsError()
        return response(agent.task(await invoke(agent.operation, identifier)))

    @app.post("/tasks/{identifier}:cancel")
    async def cancel(identifier: str, request: Request):
        value = ParseDict(await body(request), CancelTaskRequest())
        if value.tenant or value.id not in {"", identifier}:
            raise InvalidParamsError()
        return response(await invoke(agent.cancel, identifier))

    @app.api_route("/{unsupported:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def unsupported(unsupported: str):
        if "pushNotificationConfigs" in unsupported:
            from a2a.utils.errors import PushNotificationNotSupportedError

            return error(PushNotificationNotSupportedError())
        if unsupported == "extendedAgentCard":
            from a2a.utils.errors import ExtendedAgentCardNotConfiguredError

            return error(ExtendedAgentCardNotConfiguredError())
        return error(UnsupportedOperationError())

    return app
