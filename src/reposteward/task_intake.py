"""Contribution gate facts shared by native and external development entry points."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .config import AppConfig, RepositoryPolicy
from .github import GitHubClient
from .models import Issue


def contribution_gate(
    config: AppConfig,
    github: GitHubClient,
    policy: RepositoryPolicy,
    issue_number: int,
    *,
    issue: Issue | None = None,
) -> dict[str, Any]:
    issue = issue or github.issue(policy.name, issue_number)
    assigned = config.github.login.casefold() in {
        login.casefold() for login in issue.assignees
    }
    approval = True
    if policy.maintainer_approval:
        approval = github.has_maintainer_approval(
            policy.name,
            issue_number,
            policy.maintainer_approval,
            policy.allowed_approver_associations,
        )
    competing = (
        github.competing_work(policy.name, issue_number, own_login=config.github.login)
        if policy.require_no_competing_work
        else ()
    )
    return {
        "repository": policy.name,
        "issue": issue_number,
        "state": issue.state,
        "assignees": list(issue.assignees),
        "assignment_required": policy.require_assignment_before_submit,
        "assigned_to_login": assigned,
        "approval_command": policy.maintainer_approval,
        "maintainer_approval": approval,
        "competing_work_required_absent": policy.require_no_competing_work,
        "competing_work": [asdict(value) for value in competing],
        "submission_ready": issue.state == "open"
        and (not policy.require_assignment_before_submit or assigned)
        and approval
        and not competing,
    }
