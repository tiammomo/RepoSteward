"""CLI and ordinary MCP tool adapters for the shared operation service."""

from __future__ import annotations

import time
from pathlib import Path


def _object(action, properties=None, required=()):
    return {
        "type": "object",
        "properties": {"action": {"const": action}, **(properties or {})},
        "required": ["action", *required],
        "additionalProperties": False,
    }


ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
DIGEST = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"}
OPERATION_SCHEMA = {
    "type": "object",
    "oneOf": [
        _object(
            "start_verification",
            {
                "run_id": ID,
                "profile": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
                "expected_revision": {"type": "integer", "minimum": 0},
                "expected_snapshot": DIGEST,
                "idempotency_key": KEY,
            },
            (
                "run_id",
                "profile",
                "expected_revision",
                "expected_snapshot",
                "idempotency_key",
            ),
        ),
        _object(
            "start_understanding",
            {
                "mode": {"enum": ["maintainer", "contributor"]},
                "focus": {"type": "string", "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "idempotency_key": KEY,
            },
            ("idempotency_key",),
        ),
        _object("get", {"operation_id": ID}, ("operation_id",)),
        _object("list", {"before": {"type": "integer", "minimum": 0}}),
        _object(
            "wait",
            {
                "operation_id": ID,
                "timeout": {"type": "integer", "minimum": 0, "maximum": 30},
            },
            ("operation_id",),
        ),
        *[
            _object(
                action,
                {
                    "operation_id": ID,
                    "expected_revision": DIGEST,
                    "idempotency_key": KEY,
                },
                ("operation_id", "expected_revision", "idempotency_key"),
            )
            for action in ("cancel", "reconcile")
        ],
    ],
}


def call(bridge, arguments, *, cancel_event=None):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    from reposteward.tasks.assistance_operations import AssistanceOperations

    try:
        Draft202012Validator(OPERATION_SCHEMA).validate(arguments)
    except ValidationError as exc:
        raise ValueError("invalid operation arguments") from exc
    service = AssistanceOperations(bridge)
    args = dict(arguments)
    action = args.pop("action")
    identifier = args.pop("operation_id", "")
    if action.startswith("start_"):
        return service.start(action.removeprefix("start_"), **args)
    if action == "list":
        return service.listing(**args)
    if action == "wait":
        return service.wait(identifier, **args, cancel_event=cancel_event)
    return getattr(service, action)(identifier, **args)


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "operation", help="run durable workspace-scoped assistance jobs"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    commands = parser.add_subparsers(dest="operation_command", required=True)
    start = commands.add_parser("start")
    start.add_argument("kind", choices=("verification", "understanding"))
    start.add_argument("--idempotency-key", required=True)
    for name in ("run-id", "profile", "expected-snapshot", "focus"):
        start.add_argument("--" + name)
    start.add_argument("--expected-revision", type=int)
    start.add_argument("--mode", choices=("maintainer", "contributor"))
    start.add_argument("--limit", type=int)
    for name in ("get", "wait", "cancel", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("operation_id")
        if name == "wait":
            command.add_argument("--timeout", type=int, default=10)
        if name in {"cancel", "reconcile"}:
            command.add_argument("--expected-revision", required=True)
            command.add_argument("--idempotency-key", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--before", type=int, default=0)
    worker = commands.add_parser(
        "worker", help="explicitly execute queued jobs for this workspace"
    )
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--max-seconds", type=int, default=3600)


def cli(config, args):
    from reposteward.integrations.mcp import ScopedBridge
    from reposteward.tasks.assistance_operations import AssistanceOperations

    bridge = ScopedBridge(config, args.workspace)
    if args.operation_command == "worker":
        if not 1 <= args.max_seconds <= 86400:
            raise ValueError("worker duration must be between 1 and 86400 seconds")
        service = AssistanceOperations(bridge)
        deadline, count = time.monotonic() + args.max_seconds, 0
        while True:
            progressed = service.process_once()
            count += int(progressed)
            if args.once or time.monotonic() >= deadline:
                return {"schema_version": 1, "processed": count, "public_write": False}
            if not progressed:
                time.sleep(0.5)
    names = (
        "run_id",
        "profile",
        "expected_revision",
        "expected_snapshot",
        "mode",
        "focus",
        "limit",
        "idempotency_key",
        "operation_id",
        "timeout",
        "before",
    )
    arguments = {
        name: getattr(args, name)
        for name in names
        if getattr(args, name, None) is not None
    }
    arguments["action"] = (
        "start_" + args.kind
        if args.operation_command == "start"
        else args.operation_command
    )
    return call(bridge, arguments)
