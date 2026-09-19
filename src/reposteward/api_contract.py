"""Transport-neutral, versioned error and response contracts."""

from __future__ import annotations

from .config import ConfigError
from .external_tasks import TaskConflict
from .policy import PolicyError
from .projects import ProjectError

CONTRACT_VERSION = 1


class ResultContractError(RuntimeError):
    """An adapter received a result incompatible with its advertised schema."""


def error_details(exc: BaseException) -> dict:
    # Exception text can contain credentials, remote content or rejected arguments.
    # Public errors deliberately use fixed text; no replay authority is inferred.
    if isinstance(exc, ResultContractError):
        code, message, action = (
            "result_contract_error",
            "The result does not match the advertised contract.",
            "Check the installed runtime and adapter versions before retrying.",
        )
    elif isinstance(exc, ConfigError):
        code, message, action = (
            "configuration_error",
            "The selected configuration is invalid or unavailable.",
            "Run doctor --local with the selected configuration.",
        )
    elif isinstance(exc, TaskConflict):
        code, message, action = (
            "conflict",
            "The request conflicts with current task state.",
            "Read current state and reconcile any prior attempt before a new request.",
        )
    elif isinstance(exc, (ProjectError, PolicyError)):
        code, message, action = (
            "scope_or_policy",
            "The operation is outside the configured scope or policy.",
            "Inspect the workspace binding and trusted repository policy.",
        )
    elif isinstance(exc, (KeyError, FileNotFoundError)):
        code, message, action = (
            "not_found",
            "The requested local object is unavailable.",
            "Check the object identity and selected workspace.",
        )
    elif isinstance(exc, ValueError):
        code, message, action = (
            "invalid_request",
            "The request does not match the supported input contract.",
            "Inspect capabilities and command help, then correct the request.",
        )
    else:
        code, message, action = (
            "unavailable",
            "The operation could not establish a result.",
            "Inspect recorded attempts and runtime health before considering a retry.",
        )
    return {
        "code": code,
        "message": message,
        "retryable": False,
        "next_action": action,
        "cli_exit_code": 2,
    }


def envelope(data: object = None, *, error: dict | None = None) -> dict:
    return {"schema_version": CONTRACT_VERSION, "data": data, "error": error}


ERROR_SCHEMA = {
    "type": "object",
    "required": ["code", "message", "retryable", "next_action", "cli_exit_code"],
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "next_action": {"type": "string"},
        "cli_exit_code": {"const": 2},
    },
    "additionalProperties": False,
}


def tool_output_schema(name: str) -> dict:
    """Guarantee stable routing fields, allowing additive application facts."""
    fields = {
        "run_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "revision": {"type": "integer", "minimum": 0},
        "public_write": {"const": False},
    }
    required = {
        "project": ["project", "binding_id", "workspace", "public_write"],
        "context": ["run_id", "revision", "open_work", "public_write"],
        "checkpoint": ["run_id", "revision", "public_write"],
        "evidence": [],
        "verification": ["run_id", "public_write"],
        "understanding": ["status", "public_write"],
    }[name]
    fields.update(
        {
            "project": {"type": "object"},
            "binding_id": {"type": "string"},
            "workspace": {"type": "object"},
            "open_work": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string"},
        }
    )
    success = {
        "required": required,
        "properties": fields,
        "not": {"required": ["error"]},
        "additionalProperties": True,
    }
    if name == "evidence":
        success["anyOf"] = [
            {"required": ["run_id", "evidence", "public_write"]},
            {"required": ["evidence_id", "availability"]},
        ]
        fields.update(
            {
                "evidence": {"type": "array"},
                "evidence_id": {"type": "string"},
                "availability": {"enum": ["available", "unknown"]},
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "oneOf": [
            success,
            {
                "required": ["error", "public_write"],
                "properties": {"error": ERROR_SCHEMA, "public_write": {"const": False}},
                "additionalProperties": False,
            },
        ],
    }
