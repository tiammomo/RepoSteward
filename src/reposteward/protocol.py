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
    ("context-pack", 1): "context-pack-v1.schema.json",
    ("context-pack", 2): "context-pack-v2.schema.json",
    ("checkpoint", 1): "checkpoint-v1.schema.json",
    ("context-bundle", 1): "context-bundle-v1.schema.json",
    ("context-bundle", 2): "context-bundle-v2.schema.json",
}
CURRENT_SCHEMA_VERSIONS = {
    "context-pack": 2,
    "checkpoint": 1,
    "context-bundle": 2,
}
VERSION_FIELDS = {
    "context-pack": "schema_version",
    "checkpoint": "schema_version",
    "context-bundle": "bundle_schema_version",
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
def _schemas() -> dict[tuple[str, int], dict[str, Any]]:
    root = files("reposteward").joinpath("schemas")
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for key, filename in SCHEMA_RESOURCES.items():
        value = json.loads(root.joinpath(filename).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError(f"packaged protocol schema is not an object: {filename}")
        Draft202012Validator.check_schema(value)
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _registry() -> Registry:
    registry = Registry()
    for schema in _schemas().values():
        identifier = str(schema["$id"])
        registry = registry.with_resource(identifier, Resource.from_contents(schema))
    return registry


def schema_document(name: str, version: int | None = None) -> dict[str, Any]:
    selected = version if version is not None else CURRENT_SCHEMA_VERSIONS.get(name)
    try:
        return json.loads(json.dumps(_schemas()[(name, int(selected))]))
    except (TypeError, ValueError) as exc:
        raise KeyError(f"unknown protocol schema: {name}") from exc
    except KeyError as exc:
        suffix = f" v{selected}" if selected is not None else ""
        raise KeyError(f"unknown protocol schema: {name}{suffix}") from exc


def _document_version(name: str, normalized: object) -> int:
    try:
        field = VERSION_FIELDS[name]
    except KeyError as exc:
        raise KeyError(f"unknown protocol schema: {name}") from exc
    if not isinstance(normalized, dict):
        raise ProtocolValidationError(f"invalid {name} document: expected an object")
    version = normalized.get(field)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProtocolValidationError(
            f"invalid {name} document at $.{field}: expected an integer"
        )
    return version


def validate_document(name: str, payload: object) -> None:
    normalized = _json_value(payload)
    version = _document_version(name, normalized)
    try:
        schema = _schemas()[(name, version)]
    except KeyError as exc:
        if name not in VERSION_FIELDS:
            raise KeyError(f"unknown protocol schema: {name}") from exc
        raise ProtocolValidationError(
            f"unsupported {name} {VERSION_FIELDS[name]}: {version}"
        ) from exc
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
    normalized = _json_value(payload)
    _validate_context_source_digest(normalized)
    if normalized["schema_version"] == 2:
        _validate_skill_catalog_digest(normalized)


def _validate_context_source_digest(normalized: dict[str, Any]) -> None:
    expected_source_digest = hashlib.sha256(
        _canonical_json(normalized["sources"]).encode()
    ).hexdigest()
    if normalized["source_digest"] != expected_source_digest:
        raise ProtocolValidationError(
            "context pack source digest does not match its sources"
        )


def _validate_skill_catalog_digest(normalized: dict[str, Any]) -> None:
    catalog = normalized["skill_catalog"]
    material = {
        key: catalog[key]
        for key in ("schema_version", "entries", "truncated_count", "invalid_count")
    }
    expected = hashlib.sha256(_canonical_json(material).encode()).hexdigest()
    if catalog["digest"] != expected:
        raise ProtocolValidationError(
            "context pack skill catalog digest is inconsistent"
        )
    invalid_count = sum(entry["status"] == "invalid" for entry in catalog["entries"])
    if catalog["invalid_count"] != invalid_count:
        raise ProtocolValidationError(
            "context pack skill catalog invalid count is inconsistent"
        )
    catalog_sources = [
        source
        for source in normalized["sources"]
        if source["kind"] == "repository_skill_catalog"
    ]
    expected_sources = 1 if catalog["entries"] or catalog["truncated_count"] else 0
    if len(catalog_sources) != expected_sources or any(
        source["digest"] != catalog["digest"]
        or source["locator"] != ".agents/skills"
        or source["trust"] != "repository_untrusted"
        for source in catalog_sources
    ):
        raise ProtocolValidationError(
            "context pack skill catalog source binding is inconsistent"
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
    validate_context_pack(pack)

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
