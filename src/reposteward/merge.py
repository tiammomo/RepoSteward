from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass

SUCCESSFUL_CHECK_CONCLUSIONS = {"neutral", "skipped", "success"}

# Repository configuration may add patterns, but it cannot remove these safeguards.
BUILTIN_RISK_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ci", (".github/workflows/**", ".circleci/**", "Jenkinsfile", ".gitlab-ci.yml")),
    (
        "identity_permissions_publication",
        (
            "**/auth/**",
            "**/credentials/**",
            "**/permissions/**",
            "**/publish*",
            "**/release*",
        ),
    ),
    (
        "runner_sandbox_network",
        (
            "Dockerfile*",
            "docker/**",
            "**/runner/**",
            "**/sandbox/**",
            "**/network/**",
        ),
    ),
    (
        "database_persistence",
        ("**/migrations/**", "migrations/**", "**/*.sql", "**/schema.*"),
    ),
    (
        "dependencies_release",
        (
            "pyproject.toml",
            "uv.lock",
            "requirements*.txt",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "Cargo.toml",
            "Cargo.lock",
            "go.mod",
            "go.sum",
        ),
    ),
    ("security", ("SECURITY*", "security/**", "**/security/**")),
)


@dataclass(frozen=True, slots=True)
class MergeCheck:
    name: str
    status: str
    conclusion: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class MergeSnapshot:
    repository: str
    pull_number: int
    head_sha: str
    base_sha: str
    policy_digest: str
    state: str
    draft: bool
    mergeable: str
    review_decision: str
    unresolved_conversations: int
    files: tuple[str, ...]
    additions: int
    deletions: int
    checks: tuple[MergeCheck, ...]
    files_complete: bool = True
    conversations_complete: bool = True
    checks_complete: bool = True


@dataclass(frozen=True, slots=True)
class MergeReason:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MergeDecision:
    eligible: bool
    reasons: tuple[MergeReason, ...]
    risk_categories: tuple[str, ...]
    risk_files: tuple[str, ...]
    snapshot_digest: str
    decision_digest: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _matches(path: str, pattern: str) -> bool:
    # fnmatch does not give ** special semantics, but matching both forms makes
    # root-level and nested paths explicit and predictable.
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )


def classify_merge_risk(
    files: tuple[str, ...], extra_patterns: tuple[str, ...] = ()
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    categories: set[str] = set()
    risk_files: set[str] = set()
    groups = BUILTIN_RISK_PATTERNS + (("repository_policy", extra_patterns),)
    for path in files:
        for category, patterns in groups:
            if any(_matches(path, pattern) for pattern in patterns):
                categories.add(category)
                risk_files.add(path)
    return tuple(sorted(categories)), tuple(sorted(risk_files))


def evaluate_merge(
    snapshot: MergeSnapshot,
    *,
    expected_head_sha: str,
    expected_base_sha: str,
    max_files_changed: int,
    max_diff_lines: int,
    extra_risk_patterns: tuple[str, ...] = (),
) -> MergeDecision:
    """Evaluate a snapshot without mutating GitHub, a workspace, or the harness."""
    reasons: list[MergeReason] = []

    def block(code: str, message: str) -> None:
        reasons.append(MergeReason(code, message))

    if snapshot.state.casefold() != "open":
        block("pull_not_open", "Pull request is not open.")
    if snapshot.draft:
        block("pull_is_draft", "Pull request is still a draft.")
    if snapshot.mergeable.casefold() != "mergeable":
        block(
            "pull_not_mergeable", "GitHub does not report the pull request mergeable."
        )
    if snapshot.head_sha != expected_head_sha:
        block("head_changed", "Pull request head differs from the verified commit.")
    if snapshot.base_sha != expected_base_sha:
        block("base_changed", "Base branch changed after verification.")
    if not snapshot.files_complete:
        block("files_incomplete", "Changed-file data is incomplete.")
    if not snapshot.conversations_complete:
        block("conversations_incomplete", "Review-conversation data is incomplete.")
    if not snapshot.checks_complete:
        block("checks_incomplete", "Check data is incomplete.")
    if snapshot.review_decision.casefold() != "approved":
        block("review_not_approved", "The current review decision is not approved.")
    if snapshot.unresolved_conversations:
        block(
            "unresolved_conversations",
            f"{snapshot.unresolved_conversations} review conversation(s) remain unresolved.",
        )
    for check in sorted(snapshot.checks, key=lambda value: value.name.casefold()):
        if not check.required:
            continue
        if check.status.casefold() != "completed":
            block("required_check_pending", f"Required check is pending: {check.name}.")
        elif check.conclusion.casefold() not in SUCCESSFUL_CHECK_CONCLUSIONS:
            block(
                "required_check_failed", f"Required check did not pass: {check.name}."
            )
    if len(snapshot.files) > max_files_changed:
        block("file_limit_exceeded", "The change exceeds the configured file limit.")
    if snapshot.additions + snapshot.deletions > max_diff_lines:
        block("diff_limit_exceeded", "The change exceeds the configured diff limit.")

    risk_categories, risk_files = classify_merge_risk(
        snapshot.files, extra_risk_patterns
    )
    if risk_files:
        block("high_risk_change", "High-risk paths require manual merge approval.")

    snapshot_payload = asdict(snapshot)
    snapshot_digest = _digest(snapshot_payload)
    decision_payload = {
        "eligible": not reasons,
        "reasons": [asdict(value) for value in reasons],
        "risk_categories": risk_categories,
        "risk_files": risk_files,
        "snapshot_digest": snapshot_digest,
    }
    return MergeDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        risk_categories=risk_categories,
        risk_files=risk_files,
        snapshot_digest=snapshot_digest,
        decision_digest=_digest(decision_payload),
    )
