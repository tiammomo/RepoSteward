from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

MAX_CONTEXT_BUNDLE_BYTES = 2_000_000

SCHEMA_RESOURCES = {
    "context-pack": "context-pack-v1.schema.json",
    "checkpoint": "checkpoint-v1.schema.json",
    "context-bundle": "context-bundle-v1.schema.json",
}


class ProtocolValidationError(ValueError):
    """A persisted or imported context document violates its public protocol."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: object) -> Any:
    """Normalize tuples and dataclasses already materialized as dictionaries."""
    return json.loads(json.dumps(value, ensure_ascii=False))


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict[str, Any]]:
    root = files("reposteward").joinpath("schemas")
    result: dict[str, dict[str, Any]] = {}
    for name, filename in SCHEMA_RESOURCES.items():
        value = json.loads(root.joinpath(filename).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError(f"packaged protocol schema is not an object: {filename}")
        Draft202012Validator.check_schema(value)
        result[name] = value
    return result


@lru_cache(maxsize=1)
def _registry() -> Registry:
    registry = Registry()
    for schema in _schemas().values():
        identifier = str(schema["$id"])
        registry = registry.with_resource(identifier, Resource.from_contents(schema))
    return registry


def schema_document(name: str) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(_schemas()[name]))
    except KeyError as exc:
        raise KeyError(f"unknown protocol schema: {name}") from exc


def validate_document(name: str, payload: object) -> None:
    try:
        schema = _schemas()[name]
    except KeyError as exc:
        raise KeyError(f"unknown protocol schema: {name}") from exc
    normalized = _json_value(payload)
    validator = Draft202012Validator(schema, registry=_registry())
    errors = sorted(
        validator.iter_errors(normalized), key=lambda error: error.json_path
    )
    if errors:
        first = errors[0]
        raise ProtocolValidationError(
            f"invalid {name} document at {first.json_path}: {first.message}"
        )


def validate_context_pack(payload: object) -> None:
    validate_document("context-pack", payload)
    _validate_context_source_digest(_json_value(payload))


def _validate_context_source_digest(normalized: dict[str, Any]) -> None:
    expected_source_digest = hashlib.sha256(
        _canonical_json(normalized["sources"]).encode()
    ).hexdigest()
    if normalized["source_digest"] != expected_source_digest:
        raise ProtocolValidationError(
            "context pack source digest does not match its sources"
        )


def validate_checkpoint(payload: object) -> None:
    validate_document("checkpoint", payload)


def validate_context_bundle(
    payload: object, *, require_checkpoint: bool = False
) -> None:
    validate_document("context-bundle", payload)
    normalized = _json_value(payload)
    pack = normalized["context_pack"]
    metadata = normalized["context_metadata"]
    work_item = normalized["work_item"]
    harness_run = normalized["harness_run"]
    checkpoint = normalized["checkpoint"]
    _validate_context_source_digest(pack)

    unsigned = {
        key: normalized[key]
        for key in (
            "bundle_schema_version",
            "work_item",
            "harness_run",
            "context_metadata",
            "context_pack",
            "checkpoint",
            "continuity",
        )
    }
    encoded = _canonical_json(unsigned)
    expected_digest = hashlib.sha256(encoded.encode()).hexdigest()
    if normalized["bundle_digest"] != expected_digest:
        raise ProtocolValidationError(
            "context bundle digest does not match its payload"
        )
    expected_tokens = (len(encoded) + 3) // 4
    if normalized["estimated_tokens"] != expected_tokens:
        raise ProtocolValidationError("context bundle token estimate is inconsistent")

    expected = {
        "work item": (work_item["id"], pack["work_item_id"]),
        "run": (harness_run["run_id"], pack["provenance"]["run_id"]),
        "context pack": (metadata["id"], pack["id"]),
        "schema version": (metadata["schema_version"], pack["schema_version"]),
        "source digest": (metadata["source_digest"], pack["source_digest"]),
        "base commit": (metadata["base_commit"], pack["project"]["base_commit"]),
        "repository": (
            work_item["repository"].casefold(),
            pack["project"]["repository"].casefold(),
        ),
        "work item kind": (work_item["kind"], pack["task"]["kind"]),
        "external id": (work_item["external_id"], pack["task"]["external_id"]),
    }
    mismatched = [name for name, values in expected.items() if values[0] != values[1]]
    if mismatched:
        raise ProtocolValidationError(
            "context bundle contains inconsistent identities: " + ", ".join(mismatched)
        )
    if checkpoint is None:
        if require_checkpoint:
            raise ProtocolValidationError(
                "an imported context bundle needs a checkpoint"
            )
        return
    checkpoint_expected = {
        "work item": (checkpoint["work_item_id"], work_item["id"]),
        "run": (checkpoint["run_id"], harness_run["run_id"]),
        "context pack": (checkpoint["context_pack_id"], pack["id"]),
        "schema version": (checkpoint["schema_version"], pack["schema_version"]),
    }
    checkpoint_mismatched = [
        name for name, values in checkpoint_expected.items() if values[0] != values[1]
    ]
    if checkpoint_mismatched:
        raise ProtocolValidationError(
            "checkpoint contains inconsistent identities: "
            + ", ".join(checkpoint_mismatched)
        )


def read_context_bundle(path: Path) -> dict[str, Any]:
    source = path.expanduser().resolve()
    size = source.stat().st_size
    if size > MAX_CONTEXT_BUNDLE_BYTES:
        raise ProtocolValidationError(
            f"context bundle is {size} bytes; limit is {MAX_CONTEXT_BUNDLE_BYTES}"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError(f"cannot read context bundle: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolValidationError("context bundle must be a JSON object")
    validate_context_bundle(value, require_checkpoint=True)
    return value
