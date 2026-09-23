from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MAX_ISSUE_TITLE_CHARS = 200
MAX_ISSUE_BODY_CHARS = 30_000
MAX_DETAILS_CHARS = 12_000
SENSITIVE_DETAIL = re.compile(
    r"(?i)(?:github_token|gh_token|openai_api_key|anthropic_api_key|"
    r"aws_secret_access_key|npm_token|pypi_token)\s*[:=]\s*\S+|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
SECURITY_REPORT = re.compile(
    r"(?i)(?:\bCVE-\d{4}-\d+\b|\b(?:security vulnerability|"
    r"remote code execution|arbitrary code execution|privilege escalation|"
    r"authentication bypass|authorization bypass|credential leak|secret leak|"
    r"token leak|command injection|sql injection|cross-site scripting|xss|csrf|"
    r"path traversal|sandbox escape|insecure deserialization)\b|"
    r"安全漏洞|凭据泄露|密钥泄露|令牌泄露|身份验证绕过|鉴权绕过|权限绕过|"
    r"命令注入|SQL\s*注入|跨站脚本|路径穿越|沙箱逃逸|任意代码执行|远程代码执行|提权)"
)
PROPOSAL_MARKER = re.compile(
    r"^<!-- reposteward-proposal:v1 repository=(?P<repository>[A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+) draft_id=(?P<draft_id>[a-f0-9]{32}) -->\n?",
    re.MULTILINE,
)


def read_details(path: Path | None) -> str:
    if path is None:
        return ""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"issue details file does not exist: {resolved}")
    sensitive_parts = {".env", "credential", "credentials", "secret", "secrets"}
    if any(part.casefold() in sensitive_parts for part in resolved.parts):
        raise ValueError("refusing to read issue details from a sensitive path")
    value = resolved.read_text(encoding="utf-8")
    if len(value) > MAX_DETAILS_CHARS:
        raise ValueError(f"issue details file exceeds {MAX_DETAILS_CHARS} characters")
    if SENSITIVE_DETAIL.search(value):
        raise ValueError("issue details appear to contain a credential")
    return value.strip()


def render_issue_body(
    *,
    summary: str,
    actual: str,
    expected: str,
    reproduction: str = "",
    environment: str = "",
    acceptance: tuple[str, ...] = (),
    details: str = "",
    language: str = "en",
) -> str:
    values = {
        "summary": summary.strip(),
        "actual": actual.strip(),
        "expected": expected.strip(),
        "reproduction": reproduction.strip(),
        "environment": environment.strip(),
        "details": details.strip(),
    }
    for required in ("summary", "actual", "expected"):
        if not values[required]:
            raise ValueError(f"issue {required} must not be empty")
    if language not in {"en", "zh"}:
        raise ValueError("issue language must be 'en' or 'zh'")

    headings = (
        {
            "summary": "问题概述",
            "actual": "实际结果",
            "expected": "期望结果",
            "reproduction": "复现步骤",
            "environment": "环境信息",
            "acceptance": "验收条件",
            "details": "补充信息",
        }
        if language == "zh"
        else {
            "summary": "Summary",
            "actual": "Actual behavior",
            "expected": "Expected behavior",
            "reproduction": "Steps to reproduce",
            "environment": "Environment",
            "acceptance": "Acceptance criteria",
            "details": "Additional context",
        }
    )
    sections = [
        f"## {headings['summary']}\n\n{values['summary']}",
        f"## {headings['actual']}\n\n{values['actual']}",
        f"## {headings['expected']}\n\n{values['expected']}",
    ]
    if values["reproduction"]:
        sections.append(f"## {headings['reproduction']}\n\n{values['reproduction']}")
    if values["environment"]:
        sections.append(f"## {headings['environment']}\n\n{values['environment']}")
    criteria = tuple(value.strip() for value in acceptance if value.strip())
    if criteria:
        checklist = "\n".join(f"- [ ] {value}" for value in criteria)
        sections.append(f"## {headings['acceptance']}\n\n{checklist}")
    if values["details"]:
        sections.append(f"## {headings['details']}\n\n{values['details']}")
    body = "\n\n".join(sections).strip() + "\n"
    if len(body) > MAX_ISSUE_BODY_CHARS:
        raise ValueError(f"issue body exceeds {MAX_ISSUE_BODY_CHARS} characters")
    if SENSITIVE_DETAIL.search(body):
        raise ValueError("issue body appears to contain a credential")
    return body


def validate_issue_title(title: str) -> str:
    value = " ".join(title.split())
    if not value:
        raise ValueError("issue title must not be empty")
    if len(value) > MAX_ISSUE_TITLE_CHARS:
        raise ValueError(f"issue title exceeds {MAX_ISSUE_TITLE_CHARS} characters")
    if SENSITIVE_DETAIL.search(value):
        raise ValueError("issue title appears to contain a credential")
    return value


def issue_security_signals(title: str, body: str) -> tuple[str, ...]:
    """Return fail-closed signals that require a private reporting channel."""
    combined = f"{title}\n{body}"
    signals: list[str] = []
    if SENSITIVE_DETAIL.search(combined):
        signals.append("credential-like content")
    if SECURITY_REPORT.search(combined):
        signals.append("potential security vulnerability")
    return tuple(signals)


def attach_proposal_marker(body: str, *, repository: str, draft_id: str) -> str:
    marker = (
        f"<!-- reposteward-proposal:v1 repository={repository.lower()} "
        f"draft_id={draft_id} -->"
    )
    return f"{marker}\n{body.lstrip()}"


def proposal_body(body: str, *, repository: str) -> str:
    """Strip and validate optional RepoSteward routing metadata."""
    match = PROPOSAL_MARKER.search(body)
    if match and match.group("repository").casefold() != repository.casefold():
        raise ValueError(
            "online proposal targets a different repository than the review command"
        )
    clean = PROPOSAL_MARKER.sub("", body, count=1).strip() + "\n"
    if not clean.strip():
        raise ValueError("online proposal body must not be empty")
    if len(clean) > MAX_ISSUE_BODY_CHARS:
        raise ValueError(f"issue body exceeds {MAX_ISSUE_BODY_CHARS} characters")
    if SENSITIVE_DETAIL.search(clean):
        raise ValueError("issue body appears to contain a credential")
    return clean


def issue_review_digest(
    *,
    project_item_id: str,
    project_id: str,
    updated_at: str,
    repository: str,
    title: str,
    body: str,
    creator: str,
    similar_issues: list[dict[str, Any]],
) -> str:
    normalized_similar = sorted(
        (
            {
                "number": int(value["number"]),
                "title": str(value["title"]),
                "state": str(value["state"]),
                "url": str(value["url"]),
            }
            for value in similar_issues
        ),
        key=lambda value: (value["number"], value["url"]),
    )
    payload = {
        "version": 1,
        "project_item_id": project_item_id,
        "project_id": project_id,
        "updated_at": updated_at,
        "repository": repository.casefold(),
        "title": title,
        "body": body,
        "creator": creator.casefold(),
        "similar_issues": normalized_similar,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
