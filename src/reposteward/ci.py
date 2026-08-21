from __future__ import annotations

import hashlib
import json
import re
from typing import Any

FAILED_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "stale", "timed_out"}
)
MAX_LOG_BYTES = 256 * 1024
MAX_EXCERPT_LINES = 12
MAX_EXCERPT_LINE_CHARS = 300
MAX_TEST_IDS = 20
MAX_COMPARISON_RUNS = 12
MAX_CURRENT_LOGS = 24
MAX_COMPARISON_LOGS = 24

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z|"
    r"\d{2}:\d{2}:\d{2})\s*"
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|sig|signature|key|secret|x-amz-signature)=)[^&\s]+"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_SIGNAL = re.compile(
    r"(?i)\b(error|failed?|failure|exception|traceback|panic|fatal|timeout|"
    r"timed out|assert(?:ion)?|segmentation fault|no space left)\b"
)
_TEST_ID = re.compile(
    r"(?i)(?:FAILED|FAIL|ERROR)\s+([A-Za-z0-9_./\\:[\]-]+(?:\([^)]*\))?)"
)
_INFRASTRUCTURE = re.compile(
    r"(?i)(runner (?:has )?lost communication with the server|"
    r"hosted runner (?:is not responding|failed to start)|"
    r"github actions (?:has )?encountered an internal error|"
    r"runner was terminated by the operating system)"
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def redact_ci_log(value: str) -> str:
    """Remove common credentials before a log fragment enters an output object."""
    value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = _KNOWN_TOKEN.sub("[REDACTED]", value)
    value = _JWT.sub("[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
    return _SECRET_VALUE.sub(r"\1\2[REDACTED]", value)


def _normalize_line(value: str) -> str:
    value = _ANSI.sub("", value)
    value = _TIMESTAMP.sub("", value)
    value = re.sub(r"(?i)[A-Z]:\\a\\[^\\\s]+\\[^\\\s]+", "<workspace>", value)
    value = re.sub(r"/home/runner/work/[^/\s]+/[^/\s]+", "<workspace>", value)
    value = re.sub(r"(?i)[A-Z]:\\Users\\[^\\\s]+", "<home>", value)
    value = re.sub(r"/(?:home|Users)/[^/\s]+", "<home>", value)
    value = re.sub(r"\b[0-9a-f]{12,64}\b", "<hex>", value, flags=re.IGNORECASE)
    value = re.sub(r":\d+(?::\d+)?\b", ":<line>", value)
    value = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b", "<duration>", value)
    return " ".join(value.casefold().split())


def platform_key(job: dict[str, Any]) -> str:
    labels = sorted(
        {
            _normalize_line(redact_ci_log(str(value).strip()))[:200]
            for value in (job.get("labels") or ())
            if str(value).strip() and str(value).strip().casefold() != "self-hosted"
        }
    )
    if labels:
        return "|".join(labels)
    group = _normalize_line(
        redact_ci_log(str(job.get("runner_group_name") or "").strip())
    )[:200]
    return group or "unknown"


def fingerprint_failure(
    job: dict[str, Any], *, log: str, log_bytes: int, log_truncated: bool
) -> dict[str, Any]:
    """Build a bounded, secret-redacted fingerprint from one failed job log."""
    redacted = redact_ci_log(log)
    signal_lines: list[str] = []
    test_ids: set[str] = set()
    infrastructure_signals: set[str] = set()
    for raw_line in redacted.splitlines():
        line = _ANSI.sub("", raw_line).strip()
        if not line:
            continue
        for match in _TEST_ID.findall(line):
            if len(test_ids) >= MAX_TEST_IDS:
                break
            test_ids.add(_normalize_line(match))
        infrastructure_signals.update(
            match.casefold() for match in _INFRASTRUCTURE.findall(line)
        )
        if not _SIGNAL.search(line):
            continue
        normalized = _normalize_line(line)
        if (
            len(signal_lines) < MAX_EXCERPT_LINES
            and normalized
            and normalized not in signal_lines
        ):
            signal_lines.append(normalized[:MAX_EXCERPT_LINE_CHARS])
    failed_steps = sorted(
        {
            _normalize_line(redact_ci_log(str(value.get("name") or "").strip()))[
                :MAX_EXCERPT_LINE_CHARS
            ]
            for value in (job.get("steps") or ())
            if str(value.get("conclusion") or "").casefold() in FAILED_CONCLUSIONS
            and str(value.get("name") or "").strip()
        }
    )
    material = {
        "workflow": _normalize_line(
            redact_ci_log(str(job.get("workflow_name") or "").strip())
        )[:MAX_EXCERPT_LINE_CHARS],
        "job": _normalize_line(redact_ci_log(str(job.get("name") or "").strip()))[
            :MAX_EXCERPT_LINE_CHARS
        ],
        "platform": platform_key(job),
        "failed_steps": failed_steps,
        "test_ids": sorted(test_ids),
        "error_fragments": signal_lines,
    }
    fingerprint = _digest(material) if signal_lines or test_ids else ""
    return {
        "fingerprint": fingerprint,
        "fingerprint_material": material,
        "log_excerpt": signal_lines,
        "log_bytes_read": log_bytes,
        "log_truncated": log_truncated,
        "infrastructure_signals": sorted(infrastructure_signals),
    }


def compact_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": int(job.get("run_id") or 0),
        "run_attempt": int(job.get("run_attempt") or 0),
        "job_id": int(job.get("id") or 0),
        "check_run_id": int(job.get("check_run_id") or 0),
        "head_sha": str(job.get("head_sha") or ""),
        "workflow_name": redact_ci_log(str(job.get("workflow_name") or ""))[:300],
        "name": redact_ci_log(str(job.get("name") or ""))[:300],
        "platform": platform_key(job),
        "status": str(job.get("status") or ""),
        "conclusion": str(job.get("conclusion") or ""),
        "failed_steps": [
            redact_ci_log(str(value.get("name") or ""))[:300]
            for value in (job.get("steps") or ())
            if str(value.get("conclusion") or "").casefold() in FAILED_CONCLUSIONS
        ],
        "url": redact_ci_log(str(job.get("url") or ""))[:500],
    }


def compact_check(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(check.get("id") or 0),
        "name": redact_ci_log(str(check.get("name") or ""))[:300],
        "status": str(check.get("status") or "")[:100],
        "conclusion": str(check.get("conclusion") or "")[:100],
        "url": redact_ci_log(str(check.get("url") or ""))[:500],
        "app_slug": redact_ci_log(str(check.get("app_slug") or ""))[:200],
    }


def same_job(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("workflow_name") or "").casefold(),
        str(left.get("name") or "").casefold(),
        platform_key(left),
    ) == (
        str(right.get("workflow_name") or "").casefold(),
        str(right.get("name") or "").casefold(),
        platform_key(right),
    )


def classify_failure(
    current: dict[str, Any],
    *,
    same_head: list[dict[str, Any]],
    pull_history: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    evidence_complete: bool,
) -> dict[str, Any]:
    """Classify one failure using only deterministic, reproducible evidence."""
    fingerprint = str(current.get("fingerprint") or "")
    current_job = current["job"]
    comparable_same_head = [
        value for value in same_head if same_job(current_job, value)
    ]
    comparable_history = [
        value for value in pull_history if same_job(current_job, value)
    ]
    comparable_baseline = [value for value in baseline if same_job(current_job, value)]
    same_head_success = [
        value
        for value in comparable_same_head
        if str(value.get("conclusion") or "").casefold() == "success"
    ]
    baseline_success = [
        value
        for value in comparable_baseline
        if str(value.get("conclusion") or "").casefold() == "success"
    ]
    baseline_failures = [
        value
        for value in comparable_baseline
        if str(value.get("fingerprint") or "") == fingerprint and fingerprint
    ]
    baseline_other_failures = [
        value
        for value in comparable_baseline
        if str(value.get("conclusion") or "").casefold() in FAILED_CONCLUSIONS
        and value not in baseline_failures
    ]
    history_matches = [
        value
        for value in comparable_history
        if str(value.get("fingerprint") or "") == fingerprint and fingerprint
    ]

    if current.get("infrastructure_signals"):
        classification = "infrastructure"
        confidence = "high"
        reason = "current log matched a deterministic infrastructure signature"
        compared = []
    elif same_head_success:
        classification = "flaky"
        confidence = "high"
        reason = "the same head, job, and platform also completed successfully"
        compared = same_head_success
    elif baseline_failures:
        classification = "inherited"
        confidence = "high"
        reason = "the current base has the same failure fingerprint"
        compared = baseline_failures
    elif (
        fingerprint
        and baseline_success
        and not baseline_other_failures
        and evidence_complete
    ):
        classification = "introduced"
        confidence = "medium"
        reason = "the current base passed the same job and platform"
        compared = baseline_success
    else:
        classification = "unknown"
        confidence = "none"
        reason = "available deterministic evidence is insufficient or contradictory"
        compared = []

    return {
        "classification": classification,
        "confidence": confidence,
        "reason": reason,
        "compared_run_ids": sorted(
            {int(value.get("run_id") or 0) for value in compared if value.get("run_id")}
        ),
        "same_pr_history_run_ids": sorted(
            {
                int(value.get("run_id") or 0)
                for value in history_matches
                if value.get("run_id")
            }
        ),
    }


def finalize_ci_analysis(material: dict[str, Any]) -> dict[str, Any]:
    return {**material, "analysis_digest": _digest(material)}
