from __future__ import annotations

import re
from pathlib import Path

MAX_ISSUE_TITLE_CHARS = 200
MAX_ISSUE_BODY_CHARS = 30_000
MAX_DETAILS_CHARS = 12_000
SENSITIVE_DETAIL = re.compile(
    r"(?i)(?:github_token|gh_token|openai_api_key|anthropic_api_key|"
    r"aws_secret_access_key|npm_token|pypi_token)\s*[:=]\s*\S+|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
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
