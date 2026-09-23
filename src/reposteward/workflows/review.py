from __future__ import annotations

import json
from typing import Any

PACKET_SCHEMA_VERSION = 1
MAX_PACKET_CHARS = 12_000
MAX_SUMMARY_CHARS = 800
MAX_COMMAND_CHARS = 400
MAX_PATH_CHARS = 800
MAX_FILES = 24
MAX_COMMANDS = 12
MAX_RISKS = 8


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}… [omitted {omitted} chars]"


def _next_action(status: str, stage: str) -> str:
    if status == "ready":
        return "human_review"
    if status == "submitted":
        return "monitor_pull_request"
    if status == "failed":
        return "diagnose_failure"
    if status == "running":
        return f"wait_for_{stage or 'pipeline'}"
    return "inspect_run"


def _packet_chars(packet: dict[str, Any]) -> int:
    return len(json.dumps(packet, ensure_ascii=False, indent=2))


def _fit_packet(packet: dict[str, Any]) -> dict[str, Any]:
    packet["packet_truncated"] = False
    if _packet_chars(packet) <= MAX_PACKET_CHARS:
        return packet
    packet["packet_truncated"] = True
    reductions = (
        (packet["agent"], "risks", "risks_omitted", 0),
        (packet["change"], "files", "files_omitted", 5),
        (
            packet["verification"],
            "commands",
            "commands_omitted",
            1,
        ),
    )
    for section, values_key, omitted_key, minimum in reductions:
        values = section[values_key]
        while len(values) > minimum and _packet_chars(packet) > MAX_PACKET_CHARS:
            values.pop()
            section[omitted_key] += 1
        if _packet_chars(packet) <= MAX_PACKET_CHARS:
            return packet
    if _packet_chars(packet) <= MAX_PACKET_CHARS:
        return packet
    return {
        "schema_version": packet["schema_version"],
        "run_id": packet["run_id"],
        "repository": packet["repository"],
        "issue": packet["issue"],
        "status": packet["status"],
        "stage": packet["stage"],
        "commit_sha": packet["commit_sha"],
        "error": packet["error"],
        "next_action": packet["next_action"],
        "packet_truncated": True,
        "truncation_reason": "review packet exceeded its 12000 character budget",
    }


def compact_command(raw: dict[str, Any], index: int) -> dict[str, Any]:
    output = str(raw.get("output", ""))
    output_chars = raw.get("output_chars")
    if not isinstance(output_chars, int) or (output_chars == 0 and output):
        output_chars = len(output)
    return {
        "index": index,
        "command": _clip(raw.get("command"), MAX_COMMAND_CHARS),
        "status": "passed" if raw.get("exit_code") == 0 else "failed",
        "exit_code": raw.get("exit_code"),
        "duration_seconds": raw.get("duration_seconds"),
        "output_chars": output_chars,
        "output_bytes": raw.get("output_bytes"),
        "output_sha256": raw.get("output_sha256", ""),
        "output_truncated": bool(raw.get("output_truncated")),
        "log_path": _clip(raw.get("log_path"), MAX_PATH_CHARS),
        "log_truncated": bool(raw.get("log_truncated")),
    }


def compact_run(run: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded review packet without embedding command output."""
    details = run.get("details")
    if not isinstance(details, dict):
        details = {}
    agent = details.get("agent_result")
    if not isinstance(agent, dict):
        agent = {}
    metrics = details.get("agent_metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    verification = details.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    raw_commands = verification.get("commands")
    if not isinstance(raw_commands, (list, tuple)):
        raw_commands = ()
    harness = details.get("harness")
    if not isinstance(harness, dict):
        harness = {}

    commands: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_commands[:MAX_COMMANDS], start=1):
        if not isinstance(raw, dict):
            continue
        commands.append(compact_command(raw, index))

    raw_files = details.get("changed_files")
    if not isinstance(raw_files, (list, tuple)):
        raw_files = ()
    files = [_clip(value, MAX_PATH_CHARS) for value in raw_files[:MAX_FILES]]
    raw_risks = agent.get("risks")
    if not isinstance(raw_risks, (list, tuple)):
        raw_risks = ()

    status = str(run.get("status", ""))
    stage = str(run.get("stage", ""))
    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "run_id": _clip(run.get("id"), 200),
        "repository": _clip(run.get("repository"), 300),
        "issue": run.get("issue_number"),
        "status": status,
        "stage": stage,
        "updated_at": _clip(run.get("updated_at"), 100),
        "worktree": _clip(
            details.get("worktree") or run.get("worktree", ""), MAX_PATH_CHARS
        ),
        "base_branch": _clip(details.get("base_branch"), 300),
        "branch": _clip(details.get("branch"), 300),
        "commit_sha": _clip(details.get("commit_sha"), 100),
        "change": {
            "files": files,
            "files_omitted": max(0, len(raw_files) - len(files)),
            "added_lines": details.get("added_lines"),
            "deleted_lines": details.get("deleted_lines"),
        },
        "agent": {
            "summary": _clip(agent.get("summary"), MAX_SUMMARY_CHARS),
            "pr_title": _clip(agent.get("pr_title"), MAX_SUMMARY_CHARS),
            "risks": [
                _clip(value, MAX_SUMMARY_CHARS) for value in raw_risks[:MAX_RISKS]
            ],
            "risks_omitted": max(0, len(raw_risks) - MAX_RISKS),
        },
        "harness": {
            "name": _clip(harness.get("name"), 100),
            "model": _clip(harness.get("model"), 200),
            "native_session_id": _clip(harness.get("native_session_id"), 200),
            "context_pack_id": _clip(harness.get("context_pack_id"), 200),
        },
        "usage": {
            key: metrics.get(key)
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "prompt_chars",
                "event_bytes",
                "stderr_bytes",
                "event_count",
                "tool_call_count",
                "duration_seconds",
                "log_path",
                "warnings",
            )
            if key in metrics
        },
        "verification": {
            "passed": verification.get("passed"),
            "reason": _clip(verification.get("reason"), MAX_SUMMARY_CHARS),
            "commands": commands,
            "commands_omitted": max(0, len(raw_commands) - len(commands)),
        },
        "error": _clip(details.get("error"), MAX_SUMMARY_CHARS),
        "next_action": _next_action(status, stage),
    }
    warnings = packet["usage"].get("warnings")
    if isinstance(warnings, (list, tuple)):
        packet["usage"]["warnings"] = [
            _clip(value, MAX_SUMMARY_CHARS) for value in warnings[:8]
        ]
    if "log_path" in packet["usage"]:
        packet["usage"]["log_path"] = _clip(packet["usage"]["log_path"], MAX_PATH_CHARS)
    return _fit_packet(packet)
