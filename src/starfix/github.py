from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .config import GitHubConfig
from .models import Issue, RepositoryInfo


class GitHubError(RuntimeError):
    """A GitHub API request failed."""


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    state: str
    draft: bool


@dataclass(frozen=True, slots=True)
class CompetingWork:
    kind: str
    actor: str
    url: str
    detail: str


def resolve_authentication(
    config: GitHubConfig, *, required: bool = False
) -> tuple[str, str]:
    for name in config.token_env:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    command = config.gh_auth_command
    if command and shutil.which(command[0]):
        try:
            result = subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "gh OAuth"
    if required:
        names = " or ".join(config.token_env)
        raise GitHubError(
            f"GitHub API authentication required; set {names} or authenticate "
            "GitHub CLI with 'gh auth login'"
        )
    return "", "missing"


def resolve_token(config: GitHubConfig, *, required: bool = False) -> str:
    return resolve_authentication(config, required=required)[0]


class GitHubClient:
    def __init__(self, config: GitHubConfig, token: str = "") -> None:
        self.config = config
        self.token = token or resolve_token(config)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        data: dict[str, Any] | None = None,
        expected: Iterable[int] = (200,),
    ) -> tuple[Any, urllib.response.addinfourl]:
        url = f"{self.config.api_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "starfix/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=30)
            payload_bytes = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            message = error_body
            try:
                message = json.loads(error_body).get("message", error_body)
            except json.JSONDecodeError:
                pass
            raise GitHubError(
                f"GitHub {method} {path} failed ({exc.code}): {message}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"GitHub {method} {path} failed: {exc.reason}") from exc
        if response.status not in set(expected):
            raise GitHubError(
                f"GitHub {method} {path} returned unexpected status {response.status}"
            )
        if not payload_bytes:
            return None, response
        try:
            return json.loads(payload_bytes), response
        except json.JSONDecodeError as exc:
            raise GitHubError(f"GitHub {method} {path} returned invalid JSON") from exc

    def authenticated_login(self) -> str:
        if not self.token:
            raise GitHubError("cannot check login without a GitHub token")
        payload, _ = self._request("GET", "/user")
        return str(payload["login"])

    def repository(self, full_name: str) -> RepositoryInfo:
        payload, _ = self._request("GET", f"/repos/{full_name}")
        license_info = payload.get("license") or {}
        return RepositoryInfo(
            full_name=str(payload["full_name"]),
            default_branch=str(payload["default_branch"]),
            stars=int(payload["stargazers_count"]),
            forks=int(payload["forks_count"]),
            open_issues=int(payload["open_issues_count"]),
            pushed_at=str(payload["pushed_at"]),
            archived=bool(payload["archived"]),
            is_fork=bool(payload["fork"]),
            license_spdx=(str(license_info["spdx_id"]) if license_info else None),
        )

    def issues(
        self, full_name: str, *, per_page: int = 100, max_pages: int = 2
    ) -> list[Issue]:
        result: list[Issue] = []
        for page in range(1, max_pages + 1):
            payload, _ = self._request(
                "GET",
                f"/repos/{full_name}/issues",
                query={
                    "state": "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                raise GitHubError(f"issue response for {full_name} was not a list")
            for item in payload:
                if "pull_request" in item:
                    continue
                result.append(self._parse_issue(full_name, item))
            if len(payload) < per_page:
                break
        return result

    def issue(self, full_name: str, number: int) -> Issue:
        payload, _ = self._request("GET", f"/repos/{full_name}/issues/{number}")
        if "pull_request" in payload:
            raise GitHubError(f"{full_name}#{number} is a pull request, not an issue")
        return self._parse_issue(full_name, payload)

    @staticmethod
    def _parse_issue(full_name: str, item: dict[str, Any]) -> Issue:
        return Issue(
            repository=full_name,
            number=int(item["number"]),
            node_id=int(item["id"]),
            title=str(item["title"]),
            body=str(item.get("body") or ""),
            url=str(item["html_url"]),
            labels=tuple(str(label["name"]) for label in item.get("labels", ())),
            comments=int(item.get("comments", 0)),
            created_at=str(item["created_at"]),
            updated_at=str(item["updated_at"]),
            author_login=str(item["user"]["login"]),
            author_association=str(item.get("author_association") or "NONE"),
            state=str(item.get("state") or "open"),
            assignees=tuple(
                str(assignee["login"]) for assignee in item.get("assignees", ())
            ),
            locked=bool(item.get("locked", False)),
        )

    def has_maintainer_approval(
        self,
        full_name: str,
        number: int,
        command: str,
        allowed_associations: tuple[str, ...],
    ) -> bool:
        payload, _ = self._request(
            "GET",
            f"/repos/{full_name}/issues/{number}/comments",
            query={"per_page": 100},
        )
        associations = {value.upper() for value in allowed_associations}
        command_pattern = re.escape(command)
        start = re.compile(
            rf"^(?:\s*@[-\w]+\s+)*{command_pattern}(?:\s|$)", re.IGNORECASE
        )
        end = re.compile(rf"(?:^|\s){command_pattern}\s*$", re.IGNORECASE)
        for comment in payload:
            if str(comment.get("author_association", "")).upper() not in associations:
                continue
            body = str(comment.get("body") or "").strip()
            if start.search(body) or end.search(body):
                return True
        return False

    def competing_work(
        self,
        full_name: str,
        number: int,
        *,
        own_login: str,
        pull_request_references: dict[int, tuple[CompetingWork, ...]] | None = None,
    ) -> tuple[CompetingWork, ...]:
        conflicts: list[CompetingWork] = []
        comments, _ = self._request(
            "GET",
            f"/repos/{full_name}/issues/{number}/comments",
            query={"per_page": 100},
        )
        claim = re.compile(
            r"(?:^|\n)\s*/claim\s*(?:$|\n)|"
            r"\b(?:i(?:'d|'ll|'m| am| can| will| would)|working on|picking up|"
            r"taking)\b.{0,100}\b(?:take|work|fix|implement|prepare|send|open|pr)\b|"
            r"我.{0,40}(?:正在|会|准备).{0,40}(?:修复|实现|提交|提\s*pr)",
            re.IGNORECASE | re.DOTALL,
        )
        for comment in comments:
            actor = str((comment.get("user") or {}).get("login") or "")
            if not actor or actor.casefold() == own_login.casefold():
                continue
            body = str(comment.get("body") or "").strip()
            if claim.search(body):
                conflicts.append(
                    CompetingWork(
                        kind="claim_comment",
                        actor=actor,
                        url=str(comment.get("html_url") or ""),
                        detail=body[:240],
                    )
                )

        references = pull_request_references
        if references is None:
            references = self.open_pull_request_references(
                full_name, own_login=own_login
            )
        conflicts.extend(references.get(number, ()))
        return tuple(conflicts)

    def open_pull_request_references(
        self, full_name: str, *, own_login: str
    ) -> dict[int, tuple[CompetingWork, ...]]:
        references: dict[int, list[CompetingWork]] = {}
        seen_pulls: set[int] = set()
        issue_reference = re.compile(r"(?<![\w/])#(\d+)(?!\d)")
        for page in range(1, 3):
            pulls, _ = self._request(
                "GET",
                f"/repos/{full_name}/pulls",
                query={"state": "open", "per_page": 100, "page": page},
            )
            for pull in pulls:
                pull_number = int(pull["number"])
                if pull_number in seen_pulls:
                    continue
                seen_pulls.add(pull_number)
                body = str(pull.get("body") or "")
                actor = str((pull.get("user") or {}).get("login") or "")
                head_owner = str(
                    (
                        ((pull.get("head") or {}).get("repo") or {}).get("owner") or {}
                    ).get("login")
                    or ""
                )
                if own_login.casefold() in {actor.casefold(), head_owner.casefold()}:
                    continue
                conflict = CompetingWork(
                    kind="open_pull_request",
                    actor=actor,
                    url=str(pull.get("html_url") or ""),
                    detail=f"#{pull_number}: {pull.get('title', '')}",
                )
                for match in issue_reference.finditer(body):
                    number = int(match.group(1))
                    references.setdefault(number, []).append(conflict)
            if len(pulls) < 100:
                break
        return {key: tuple(value) for key, value in references.items()}

    def ensure_fork(self, upstream: str, owner: str, *, timeout: int = 120) -> str:
        fork_name = upstream.split("/", 1)[1]
        fork = f"{owner}/{fork_name}"
        try:
            payload, _ = self._request("GET", f"/repos/{fork}")
            parent = (payload.get("parent") or {}).get("full_name", "")
            source = (payload.get("source") or {}).get("full_name", "")
            if not payload.get("fork") or upstream.lower() not in {
                str(parent).lower(),
                str(source).lower(),
            }:
                raise GitHubError(
                    f"{fork} already exists but is not a fork of {upstream}"
                )
            return fork
        except GitHubError as exc:
            if "failed (404)" not in str(exc):
                raise

        self._request("POST", f"/repos/{upstream}/forks", data={}, expected=(202,))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                self._request("GET", f"/repos/{fork}")
                return fork
            except GitHubError as exc:
                if "failed (404)" not in str(exc):
                    raise
        raise GitHubError(f"timed out waiting for fork {fork}")

    def existing_pull_request(
        self, upstream: str, *, owner: str, branch: str
    ) -> PullRequest | None:
        payload, _ = self._request(
            "GET",
            f"/repos/{upstream}/pulls",
            query={"state": "open", "head": f"{owner}:{branch}", "per_page": 10},
        )
        if not payload:
            return None
        item = payload[0]
        return PullRequest(
            number=int(item["number"]),
            url=str(item["html_url"]),
            state=str(item["state"]),
            draft=bool(item.get("draft", False)),
        )

    def create_pull_request(
        self,
        upstream: str,
        *,
        owner: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        existing = self.existing_pull_request(upstream, owner=owner, branch=branch)
        if existing:
            return existing
        payload, _ = self._request(
            "POST",
            f"/repos/{upstream}/pulls",
            data={
                "title": title,
                "head": f"{owner}:{branch}",
                "base": base,
                "body": body,
                "draft": draft,
                "maintainer_can_modify": True,
            },
            expected=(201,),
        )
        return PullRequest(
            number=int(payload["number"]),
            url=str(payload["html_url"]),
            state=str(payload["state"]),
            draft=bool(payload.get("draft", False)),
        )
