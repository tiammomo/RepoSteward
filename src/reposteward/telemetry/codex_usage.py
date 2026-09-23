"""Bounded, prompt-free projections of explicitly selected local Codex turns.

Rollouts are an internal client format, not a provider billing API. Keep this
compatibility adapter separate from the stable RepoSteward usage schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

ADAPTER_VERSION = 1
SUPPORTED_CLI_VERSIONS = {"0.153.0"}
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 1_000_000
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class UsageSourceError(ValueError):
    """The source cannot safely establish the selected turn's counters."""


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise UsageSourceError("invalid usage identity or model")
    return value


def _timestamp(value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.isoformat()
    except ValueError as exc:
        raise UsageSourceError("invalid usage timestamp") from exc


def _counters(value: Any) -> dict[str, int | None]:
    if not isinstance(value, dict):
        raise UsageSourceError("unsupported Codex cumulative usage shape")
    result = {}
    for key in TOKEN_FIELDS:
        count = value.get(key)
        if count is not None and (
            type(count) is not int or not 0 <= count <= 2**63 - 1
        ):
            raise UsageSourceError("invalid Codex token counter")
        result[key] = count
    _consistent(result)
    return result


def _consistent(counts: dict[str, int | None]) -> None:
    for subset, total in (
        ("cached_input_tokens", "input_tokens"),
        ("cache_write_input_tokens", "input_tokens"),
        ("reasoning_output_tokens", "output_tokens"),
    ):
        if (
            counts[subset] is not None
            and counts[total] is not None
            and counts[subset] > counts[total]
        ):
            raise UsageSourceError("inconsistent Codex token subsets")
    if (
        all(
            counts[k] is not None
            for k in ("input_tokens", "output_tokens", "total_tokens")
        )
        and counts["input_tokens"] + counts["output_tokens"] != counts["total_tokens"]
    ):
        raise UsageSourceError("inconsistent Codex total tokens")


def _delta(end: dict, start: dict | None) -> dict[str, int | None]:
    result = {}
    for key in TOKEN_FIELDS:
        a, b = (start or {}).get(key), end.get(key)
        result[key] = b - a if a is not None and b is not None else None
        if result[key] is not None and result[key] < 0:
            raise UsageSourceError("Codex counters reset within the selected turn")
    _consistent(result)
    return result


def read_codex_turns(
    path: Path,
    turn_ids: set[str],
    *,
    previous_prefix: tuple[int, str] | None = None,
) -> dict[str, Any]:
    """Read one bounded snapshot; retain identities and counters, never messages.

    A previous full-line prefix must remain byte-identical. The final partial
    line is retried next time. A second prefix hash detects concurrent rewrites.
    """
    selected = {identifier(value) for value in turn_ids}
    if not selected or len(selected) > 100:
        raise UsageSourceError("select between 1 and 100 explicit Codex turn IDs")
    path = path.expanduser().resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        initial = os.fstat(source.fileno())
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > MAX_SOURCE_BYTES:
            raise UsageSourceError("Codex source must be a bounded regular file")
        if previous_prefix and initial.st_size < previous_prefix[0]:
            raise UsageSourceError(
                "Codex source was truncated; previous evidence retained"
            )
        digest = hashlib.sha256()
        consumed = 0
        partial = False
        prefix_checked = previous_prefix is None or previous_prefix[0] == 0
        meta = None
        current = ""
        visited: set[str] = set()
        last: dict | None = None
        turns: dict[str, dict] = {}
        for _ in range(MAX_RECORDS):
            remaining = initial.st_size - consumed
            if remaining <= 0:
                break
            line = source.readline(min(MAX_LINE_BYTES + 1, remaining))
            if len(line) > MAX_LINE_BYTES:
                raise UsageSourceError(
                    "Codex source line exceeds the metadata read limit"
                )
            if not line.endswith(b"\n"):
                partial = True
                break
            consumed += len(line)
            digest.update(line)
            if previous_prefix and consumed == previous_prefix[0]:
                if digest.hexdigest() != previous_prefix[1]:
                    raise UsageSourceError(
                        "Codex source was rewritten; previous evidence retained"
                    )
                prefix_checked = True
            try:
                record = json.loads(line)
            except (ValueError, RecursionError) as exc:
                raise UsageSourceError("invalid complete Codex JSONL record") from exc
            if not isinstance(record, dict):
                raise UsageSourceError("unsupported Codex record envelope")
            kind = record.get("type")
            if not isinstance(kind, str):
                raise UsageSourceError("invalid Codex record type")
            if kind not in {"session_meta", "turn_context", "event_msg"}:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise UsageSourceError("unsupported Codex metadata envelope")
            event = payload.get("type") if kind == "event_msg" else kind
            if not isinstance(event, str):
                raise UsageSourceError("invalid Codex metadata type")
            if kind == "session_meta":
                version = payload.get("cli_version")
                if (
                    not isinstance(version, str)
                    or version not in SUPPORTED_CLI_VERSIONS
                ):
                    raise UsageSourceError(
                        "unsupported Codex CLI version; collector adapter needs review"
                    )
                identity = {
                    "session_id": identifier(payload.get("id")),
                    "cli_version": version,
                }
                if meta is not None and meta != identity:
                    raise UsageSourceError("Codex session identity or version changed")
                # Resume appends session_meta again. Identical metadata must not
                # reset the active turn or its cumulative counter baseline.
                meta = identity
            elif event in {"turn_context", "task_started"}:
                if meta is None:
                    raise UsageSourceError("Codex session metadata is missing")
                turn_id = identifier(payload.get("turn_id"))
                if turn_id != current:
                    if turn_id in visited:
                        raise UsageSourceError("noncontiguous Codex turn identity")
                    visited.add(turn_id)
                    current = turn_id
                    if turn_id in selected:
                        turns[turn_id] = {
                            "baseline": last,
                            "latest": None,
                            "high_water": dict(last or {}),
                            "models": set(),
                            "workspaces": set(),
                            "started_at": _timestamp(record.get("timestamp")),
                            "observed_at": None,
                            "completion": "in_progress",
                        }
                if current in selected and kind == "turn_context":
                    state = turns[current]
                    state["models"].add(identifier(payload.get("model")))
                    cwd = payload.get("cwd")
                    if (
                        not isinstance(cwd, str)
                        or len(cwd) > 4096
                        or not Path(cwd).is_absolute()
                    ):
                        raise UsageSourceError("invalid Codex turn workspace")
                    state["workspaces"].add(cwd)
            elif event == "token_count":
                info = payload.get("info")
                if info is None:
                    continue  # Rate-limit-only notification, not a usage update.
                if not isinstance(info, dict) or "total_token_usage" not in info:
                    raise UsageSourceError("unsupported Codex usage info")
                counters = _counters(info["total_token_usage"])
                if current in selected:
                    state = turns[current]
                    # Check every update, so an intermediate reset cannot be hidden
                    # by a subsequent cumulative value above the original baseline.
                    _delta(counters, state["latest"] or state["baseline"])
                    for key, count in counters.items():
                        previous = state["high_water"].get(key)
                        if count is not None:
                            if previous is not None and count < previous:
                                raise UsageSourceError(
                                    "Codex counters reset within the selected turn"
                                )
                            state["high_water"][key] = count
                    state["latest"] = counters
                    state["observed_at"] = _timestamp(record.get("timestamp"))
                last = counters
            elif event in {"task_complete", "turn_aborted"}:
                turn_id = identifier(payload.get("turn_id"))
                if turn_id in selected:
                    if turn_id != current or turn_id not in turns:
                        raise UsageSourceError(
                            "Codex completion does not match the active turn"
                        )
                    turns[turn_id]["completion"] = (
                        "completed" if event == "task_complete" else "interrupted"
                    )
        else:
            raise UsageSourceError("Codex source record limit exceeded")
        if not prefix_checked:
            raise UsageSourceError(
                "Codex source prefix changed; previous evidence retained"
            )
        if meta is None or selected != turns.keys():
            raise UsageSourceError(
                "selected turn or session metadata not found in source"
            )
        source.seek(0)
        verification = hashlib.sha256()
        remaining = consumed
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise UsageSourceError("Codex source changed during collection")
            verification.update(chunk)
            remaining -= len(chunk)
        after = path.stat()
        if verification.digest() != digest.digest() or (
            initial.st_dev,
            initial.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise UsageSourceError("Codex source changed during collection")
    results = {}
    for turn_id, state in turns.items():
        if not state["workspaces"]:
            raise UsageSourceError("selected Codex turn has no workspace context")
        metrics = _delta(state["latest"] or {}, state["baseline"])
        reasons = []
        if state["baseline"] is None:
            reasons.append("baseline_unavailable")
        if state["latest"] is None:
            reasons.append("usage_unavailable")
        if any(value is None for value in metrics.values()):
            reasons.append("metrics_missing")
        models = state["models"]
        if len(models) != 1:
            reasons.append("model_ambiguous")
        results[turn_id] = {
            "turn_id": turn_id,
            "model": next(iter(models)) if len(models) == 1 else None,
            "workspaces": sorted(state["workspaces"]),
            "started_at": state["started_at"],
            "observed_at": state["observed_at"],
            "completion": state["completion"],
            "metrics": metrics,
            "unknown_reasons": reasons,
        }
    return {
        **meta,
        "adapter_version": ADAPTER_VERSION,
        "prefix_bytes": consumed,
        "prefix_sha256": digest.hexdigest(),
        "partial_tail": partial,
        "turns": results,
    }
