"""Offline capability discovery; no configured state or authentication needed."""

from __future__ import annotations

import importlib.util

from .api_contract import CONTRACT_VERSION, tool_output_schema
from .mcp_bridge import SCHEMAS
from .projects import PROJECT_SCHEMA_VERSION
from .protocol import CURRENT_SCHEMA_VERSIONS
from .runtime import installation_info
from .store import SCHEMA_VERSION


def capabilities(command_paths: list[str]) -> dict:
    return {
        "schema_version": 1,
        "installation": installation_info(),
        "cli": {
            "commands": sorted(command_paths),
            "json_envelope_version": CONTRACT_VERSION,
            "json_envelope_flag": "--json-envelope",
            "exit_codes": {
                "0": "success",
                "1": "negative_diagnostic",
                "2": "error_or_ineligible",
            },
            "envelope_exclusions": ["help", "--version", "web", "mcp serve", "image"],
        },
        "schemas": {
            **CURRENT_SCHEMA_VERSIONS,
            "state": SCHEMA_VERSION,
            "projects": PROJECT_SCHEMA_VERSION,
        },
        "mcp": {
            "implemented": True,
            "dependency_available": importlib.util.find_spec("mcp") is not None,
            "transports": ["stdio"],
            "scope": "one_explicit_workspace",
            "tools": {
                name: {
                    "input_schema": schema,
                    "output_schema": tool_output_schema(name),
                }
                for name, schema in SCHEMAS.items()
            },
            "durable_async_tasks": False,
            "public_write_tools": False,
            "client_health": "not_probed",
        },
        "a2a": {"implemented": False},
        "public_write": False,
        "writes_state": False,
    }
