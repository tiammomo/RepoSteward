"""Thin, workspace-scoped MCP transport over the same local application services."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .config import AppConfig
from .external_tasks import ExternalTasks, TaskConflict
from .projects import ProjectError

MAX_REQUEST_BYTES = 120_000
MAX_RESPONSE_BYTES = 300_000
RUN = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
REVISION = {"type": "integer", "minimum": 0}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"}
TEXT = {"type": "string", "maxLength": 2000}
TEXT_LIST = {"type": "array", "maxItems": 128, "items": TEXT}


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


SCHEMAS = {
    "project": _object({}),
    "context": _object(
        {
            "run_id": RUN,
            "budget": {"type": "integer", "minimum": 512, "maximum": 64000},
            "live": {"type": "boolean"},
            "scope_paths": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string", "maxLength": 512},
            },
        }
    ),
    "evidence": {
        "type": "object",
        "oneOf": [
            _object(
                {
                    "run_id": RUN,
                    "action": {"const": "list"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ("run_id", "action"),
            ),
            _object(
                {
                    "run_id": RUN,
                    "action": {"const": "get"},
                    "evidence_id": {"type": "string", "maxLength": 180},
                    "offset": REVISION,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 16000},
                },
                ("run_id", "action", "evidence_id"),
            ),
        ],
    },
    "understanding": {
        "type": "object",
        "oneOf": [
            _object(
                {
                    "action": {"enum": ["guide", "query"]},
                    "mode": {"enum": ["maintainer", "contributor"]},
                    "focus": TEXT,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ("action",),
            ),
            _object(
                {
                    "action": {"const": "evidence"},
                    "evidence_id": {"type": "string", "pattern": "^code:[0-9a-f]{64}$"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                ("action", "evidence_id"),
            ),
        ],
    },
    "checkpoint": _object(
        {
            "run_id": RUN,
            "expected_revision": REVISION,
            "expected_snapshot": DIGEST,
            "idempotency_key": KEY,
            "payload": _object(
                {
                    **{
                        field: TEXT_LIST
                        for field in (
                            "completed",
                            "remaining",
                            "blockers",
                            "tests_observed",
                            "risks",
                        )
                    },
                    "decisions": {
                        "type": "array",
                        "maxItems": 128,
                        "items": _object(
                            {
                                "statement": TEXT,
                                "rationale": TEXT,
                                "evidence": TEXT_LIST,
                            },
                            ("statement",),
                        ),
                    },
                    "next_action": TEXT,
                    "notes": TEXT,
                },
                ("next_action",),
            ),
        },
        (
            "run_id",
            "expected_revision",
            "expected_snapshot",
            "idempotency_key",
            "payload",
        ),
    ),
    "verification": {
        "type": "object",
        "oneOf": [
            _object(
                {"run_id": RUN, "action": {"const": "profiles"}}, ("run_id", "action")
            ),
            _object(
                {
                    "run_id": RUN,
                    "action": {"const": "inspect"},
                    "evidence_id": {
                        "type": "string",
                        "pattern": "^verification:[0-9a-f]{32}$",
                    },
                    "live": {"type": "boolean"},
                },
                ("run_id", "action", "evidence_id"),
            ),
            _object(
                {
                    "run_id": RUN,
                    "action": {"const": "request"},
                    "profile": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
                    "expected_revision": REVISION,
                    "expected_snapshot": DIGEST,
                    "idempotency_key": KEY,
                },
                (
                    "run_id",
                    "action",
                    "profile",
                    "expected_revision",
                    "expected_snapshot",
                    "idempotency_key",
                ),
            ),
        ],
    },
}
DESCRIPTIONS = {
    "understanding": "Read project overview, question-focused implementation/test reading routes and cited source lines. Static facts and repository declarations are distinct. Requires an explicit CLI scan; never scans, executes project code or writes from MCP.",
    "project": "Inspect the one workspace explicitly bound to this local server.",
    "context": "Read the current task contract, open work, decisions and verification references within a budget.",
    "evidence": "List task evidence or retrieve bounded text by its scoped evidence ID.",
    "checkpoint": "Save unverified Agent claims with exact revision and snapshot checks; grants no publication authority.",
    "verification": "Inspect evidence or request a user-configured verification profile for an exact task snapshot.",
}


class ScopedBridge:
    def __init__(self, config: AppConfig, workspace: Path):
        self.config = config
        self.tasks = ExternalTasks(config)
        self.bound = self.tasks.registry.inspect(workspace)
        self.root = Path(self.bound["binding"]["root"])
        self.tasks._policy(self.bound["project"]["repository"])

    def _scope(self, run_id: str | None = None) -> dict:
        current = self.tasks.registry.inspect(self.root)
        if any(
            current[section][field] != self.bound[section][field]
            for section, field in (
                ("project", "id"),
                ("binding", "id"),
                ("binding", "fingerprint"),
            )
        ):
            raise ProjectError(
                "server workspace binding changed; restart after explicit reconfiguration"
            )
        self.tasks._policy(current["project"]["repository"])
        if run_id:
            record = self.tasks._record(self.tasks._store(), run_id)
            if (
                record["project_id"] != current["project"]["id"]
                or record["binding_id"] != current["binding"]["id"]
            ):
                raise ProjectError("task is outside this server's workspace scope")
        return current

    def call(
        self, name: str, arguments: dict[str, Any], *, cancel_event: Event | None = None
    ) -> dict:
        if name not in SCHEMAS:
            raise ValueError("unknown RepoSteward tool")
        if len(json.dumps(arguments, ensure_ascii=False).encode()) > MAX_REQUEST_BYTES:
            raise ValueError("tool input exceeds the request limit")
        try:
            Draft202012Validator(SCHEMAS[name]).validate(arguments)
        except ValidationError as exc:
            # Do not echo arbitrary rejected input back into another context.
            raise ValueError("tool arguments do not match the declared schema") from exc
        linked = self._scope(arguments.get("run_id"))
        if name == "project":
            result = {
                "project": linked["project"],
                "binding_id": linked["binding"]["id"],
                "workspace": linked["workspace"],
                "public_write": False,
            }
        elif name == "context":
            if arguments.get("run_id"):
                result = self.tasks.context(
                    arguments["run_id"],
                    budget=arguments.get("budget", 24000),
                    live=arguments.get("live", True),
                    scope_paths=tuple(arguments.get("scope_paths", [])),
                )
            else:
                result = self.tasks.current(
                    self.root,
                    budget=arguments.get("budget", 24000),
                    scope_paths=tuple(arguments.get("scope_paths", [])),
                )
        elif name == "understanding":
            from .understanding import Understanding

            service = Understanding(self.config.state_dir / "understanding")
            if arguments["action"] == "evidence":
                result = service.evidence(
                    self.root,
                    arguments["evidence_id"],
                    start_line=arguments.get("start_line", 1),
                    limit=arguments.get("limit", 80),
                )
            else:
                result = service.guide(
                    self.root,
                    mode=arguments.get("mode", "maintainer"),
                    focus=arguments.get("focus", ""),
                    limit=arguments.get("limit", 12),
                )
        elif name == "checkpoint":
            result = self.tasks.checkpoint(**arguments)
        else:
            from .external_verification import ExternalVerification

            verification = ExternalVerification(self.config)
            run_id = arguments["run_id"]
            if name == "evidence":
                if arguments["action"] == "get":
                    result = verification.evidence(
                        run_id,
                        arguments["evidence_id"],
                        offset=arguments.get("offset", 0),
                        limit=arguments.get("limit", 8000),
                    )
                else:
                    task = self.tasks.inspect(run_id)
                    result = {
                        **verification.list(run_id, limit=arguments.get("limit", 3)),
                        "sources": task["source_evidence"],
                        "checkpoint": f"checkpoint:{run_id}:{task['revision']}",
                    }
            elif arguments["action"] == "profiles":
                result = {
                    "run_id": run_id,
                    "profiles": verification.profiles(run_id),
                    "public_write": False,
                }
            elif arguments["action"] == "inspect":
                result = verification.inspect(
                    run_id,
                    arguments["evidence_id"].split(":")[1],
                    live=arguments.get("live", True),
                )
            else:
                result = verification.request(
                    **{
                        key: value
                        for key, value in arguments.items()
                        if key != "action"
                    },
                    cancel_event=cancel_event,
                )
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError(
                "tool result exceeds response limit; reduce the requested budget or limit"
            )
        return result


def error_result(exc: BaseException) -> dict:
    code = (
        "conflict"
        if isinstance(exc, TaskConflict)
        else "scope_or_policy"
        if isinstance(exc, ProjectError)
        else "not_found"
        if isinstance(exc, KeyError)
        else "invalid_request"
        if isinstance(exc, ValueError)
        else "unavailable"
    )
    return {
        "error": {"code": code, "message": str(exc)[:1000], "cli_exit_code": 2},
        "public_write": False,
    }


def create_server(config: AppConfig, workspace: Path):
    try:
        import anyio
        from mcp import types
        from mcp.server.lowlevel import Server
    except ImportError as exc:
        raise RuntimeError(
            "MCP support requires reposteward[mcp]; use the file/CLI handoff until installed"
        ) from exc
    bridge = ScopedBridge(config, workspace)
    limiter = anyio.CapacityLimiter(4)

    async def list_tools(_context, params):
        if params is not None and params.cursor:
            raise ValueError("this tool list has no next page")
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=DESCRIPTIONS[name],
                    input_schema=schema,
                    annotations=types.ToolAnnotations(
                        read_only_hint=name
                        in {"project", "context", "evidence", "understanding"},
                        destructive_hint=False,
                        open_world_hint=False,
                    ),
                )
                for name, schema in SCHEMAS.items()
            ]
        )

    async def call_tool(_context, params):
        cancelled, started, finished = Event(), Event(), Event()

        def work():
            started.set()
            try:
                return bridge.call(
                    params.name, params.arguments or {}, cancel_event=cancelled
                )
            finally:
                finished.set()

        try:
            result = await anyio.to_thread.run_sync(
                work, abandon_on_cancel=True, limiter=limiter
            )
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            if started.is_set():
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(finished.wait, 20)
            raise
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            error = error_result(exc)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(error, ensure_ascii=False)
                    )
                ],
                is_error=True,
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(result, ensure_ascii=False)
                )
            ],
            structured_content=result,
        )

    server = Server(
        "reposteward",
        version="0.1.0",
        instructions="Local assistance for one linked workspace. Agent claims remain unverified; this server grants no public-write authority.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    # This local bridge never exports tracing or prompt data.
    server.middleware.clear()
    return server


class _BoundedInput:
    def __init__(self, source):
        self.source = source

    def __aiter__(self):
        return self

    async def __anext__(self):
        import anyio

        line = await anyio.to_thread.run_sync(
            self.source.readline, MAX_REQUEST_BYTES + 1, abandon_on_cancel=True
        )
        if not line:
            raise StopAsyncIteration
        if len(line) > MAX_REQUEST_BYTES:
            raise ValueError("MCP frame exceeds the request limit; connection closed")
        return line.decode("utf-8", errors="strict")


def serve(config: AppConfig, workspace: Path) -> None:
    import sys

    server = create_server(config, workspace)
    import anyio
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server(stdin=_BoundedInput(sys.stdin.buffer)) as (
            incoming,
            outgoing,
        ):
            await server.run(incoming, outgoing, server.create_initialization_options())

    anyio.run(run)
