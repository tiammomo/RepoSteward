from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import UTC, datetime

from .config import AppConfig, RepositoryPolicy
from .github import GitHubClient
from .models import Candidate, Issue, RepositoryInfo
from .store import Store, utc_now

SECURITY_TITLE = re.compile(
    r"(?:\bsecurity\b|\bvulnerabilit|\bcve-\d|\bghsa-|\bexfil(?:tration)?\b|"
    r"\bskillscan\b)",
    re.IGNORECASE,
)


def _normalized(values: tuple[str, ...]) -> set[str]:
    return {value.casefold().strip() for value in values}


def score_issue(
    issue: Issue,
    repository: RepositoryInfo,
    policy: RepositoryPolicy,
    config: AppConfig,
) -> Candidate:
    score = min(30.0, math.log10(max(repository.stars, 1)) * 6.0)
    reasons: list[str] = [f"active project with {repository.stars:,} stars"]
    blockers: list[str] = []
    labels = _normalized(issue.labels)
    preferred = _normalized(config.discovery.preferred_labels + policy.preferred_labels)
    blocked = _normalized(config.discovery.blocked_labels + policy.blocked_labels)

    weights = {
        "good first issue": 30.0,
        "help wanted": 24.0,
        "external": 18.0,
        "bug": 14.0,
        "documentation": 10.0,
        "docs": 10.0,
    }
    for label in sorted(labels & preferred):
        amount = weights.get(label, 8.0)
        score += amount
        reasons.append(f"preferred label {label!r} (+{amount:g})")

    blocked_matches = sorted(labels & blocked)
    if blocked_matches:
        blockers.append(f"blocked labels: {', '.join(blocked_matches)}")
    if repository.archived:
        blockers.append("repository is archived")
    if repository.is_fork:
        blockers.append("configured repository is itself a fork")
    if repository.stars < policy.min_stars:
        blockers.append(
            f"repository has {repository.stars} stars; policy requires {policy.min_stars}"
        )
    if issue.locked:
        blockers.append("issue is locked")
    assigned_logins = {value.casefold() for value in issue.assignees}
    if issue.assignees and config.github.login.casefold() not in assigned_logins:
        blockers.append(f"issue already assigned to {', '.join(issue.assignees)}")
    elif config.github.login.casefold() in assigned_logins:
        score += 12
        reasons.append(f"assigned to {config.github.login} (+12)")
    if SECURITY_TITLE.search(issue.title) or any(
        SECURITY_TITLE.search(label) for label in issue.labels
    ):
        blockers.append("possible security report must use the private disclosure path")

    body_length = len(issue.body.strip())
    if body_length < 80:
        score -= 12
        reasons.append("issue has little implementation detail (-12)")
    elif body_length > 400:
        score += 5
        reasons.append("issue includes substantial context (+5)")
    if re.search(
        r"repro|reproduce|expected|actual|test|traceback", issue.body, re.IGNORECASE
    ):
        score += 7
        reasons.append("issue appears reproducible (+7)")
    if issue.comments == 0:
        score += 4
        reasons.append("uncontested issue (+4)")
    elif issue.comments > 20:
        score -= 10
        reasons.append("long discussion suggests higher ambiguity (-10)")
    elif issue.comments > 8:
        score -= 4
        reasons.append("discussion suggests some ambiguity (-4)")

    age = datetime.now(UTC) - datetime.fromisoformat(issue.updated_at)
    if age.days > 180:
        score -= 12
        reasons.append("not updated in 180 days (-12)")
    elif age.days <= 14:
        score += 5
        reasons.append("recent maintainer activity (+5)")
    if issue.author_association in {"OWNER", "MEMBER", "COLLABORATOR"}:
        score += 8
        reasons.append("opened by a project maintainer (+8)")
    if "needs-triage" in labels or "needs review" in labels:
        score -= 5
        reasons.append("maintainer triage is still pending (-5)")

    return Candidate(
        issue=issue,
        repository=repository,
        score=round(score, 2),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        discovered_at=utc_now(),
    )


class DiscoveryService:
    def __init__(self, config: AppConfig, github: GitHubClient, store: Store) -> None:
        self.config = config
        self.github = github
        self.store = store

    def discover(self, only_repository: str = "") -> list[Candidate]:
        candidates: list[Candidate] = []
        wanted = only_repository.casefold()
        for key, policy in self.config.repositories.items():
            if not policy.enabled or (wanted and key != wanted):
                continue
            repository = self.github.repository(policy.name)
            repository_candidates: list[Candidate] = []
            for issue in self.github.issues(
                policy.name,
                per_page=self.config.discovery.issues_per_repo,
                max_pages=self.config.discovery.max_pages,
            ):
                candidate = score_issue(issue, repository, policy, self.config)
                repository_candidates.append(candidate)
            if policy.require_no_competing_work:
                references = self.github.open_pull_request_references(
                    policy.name, own_login=self.config.github.login
                )
                eligible = sorted(
                    (value for value in repository_candidates if not value.blockers),
                    key=lambda value: value.score,
                    reverse=True,
                )
                checked = {
                    value.issue.number
                    for value in eligible[
                        : self.config.discovery.competing_work_checks_per_repo
                    ]
                }
                enriched: list[Candidate] = []
                for candidate in repository_candidates:
                    if candidate.blockers:
                        enriched.append(candidate)
                        continue
                    if candidate.issue.number not in checked:
                        enriched.append(
                            replace(
                                candidate,
                                blockers=candidate.blockers
                                + ("competing-work check deferred by scan budget",),
                            )
                        )
                        continue
                    conflicts = self.github.competing_work(
                        policy.name,
                        candidate.issue.number,
                        own_login=self.config.github.login,
                        pull_request_references=references,
                    )
                    blockers = tuple(
                        f"{value.kind} by {value.actor}: {value.url}"
                        for value in conflicts
                    )
                    enriched.append(
                        replace(candidate, blockers=candidate.blockers + blockers)
                    )
                repository_candidates = enriched
            for candidate in repository_candidates:
                self.store.upsert_candidate(candidate)
                candidates.append(candidate)
        return sorted(candidates, key=lambda item: item.score, reverse=True)
