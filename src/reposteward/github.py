from __future__ import annotations

import hashlib
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

MAX_REST_PAGES = 1_000


class GitHubError(RuntimeError):
    """A GitHub API request failed."""


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    state: str
    draft: bool
    head_owner: str = ""
    head_branch: str = ""
    head_sha: str = ""
    base_branch: str = ""


@dataclass(frozen=True, slots=True)
class ProjectIssueProposal:
    item_id: str
    database_id: int
    project_id: str
    project_number: int
    project_url: str
    updated_at: str
    creator: str
    content_type: str
    title: str
    body: str
    issue_number: int = 0
    issue_url: str = ""
    repository: str = ""


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
            "User-Agent": "reposteward/0.1",
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

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload, _ = self._request(
            "POST", "/graphql", data={"query": query, "variables": variables}
        )
        if not isinstance(payload, dict):
            raise GitHubError("GitHub GraphQL response was not an object")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            messages = "; ".join(str(value.get("message", value)) for value in errors)
            raise GitHubError(f"GitHub GraphQL failed: {messages}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubError("GitHub GraphQL response did not contain data")
        return data

    @staticmethod
    def _response_has_next_page(response: object) -> bool:
        headers = getattr(response, "headers", None)
        if headers is None or not hasattr(headers, "get"):
            return False
        link = str(headers.get("Link", ""))
        return any('rel="next"' in value for value in link.split(",") if value.strip())

    def _paginated_rest_values(
        self, path: str, *, container: str = ""
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            payload, response = self._request(
                "GET", path, query={"per_page": 100, "page": page}
            )
            values = (
                payload.get(container)
                if container and isinstance(payload, dict)
                else payload
            )
            if not isinstance(values, list) or not all(
                isinstance(value, dict) for value in values
            ):
                label = container or "response"
                raise GitHubError(f"GitHub paginated {label} was not a list")
            result.extend(values)
            if not self._response_has_next_page(response):
                return result
            if page >= MAX_REST_PAGES:
                raise GitHubError(
                    f"GitHub pagination exceeded {MAX_REST_PAGES} pages for {path}"
                )
            page += 1

    def project_v2(self, owner: str, number: int, *, owner_type: str) -> dict[str, Any]:
        field = "organization" if owner_type == "organization" else "user"
        data = self._graphql(
            f"""
            query($owner: String!, $number: Int!) {{
              {field}(login: $owner) {{
                projectV2(number: $number) {{ id number url closed }}
              }}
            }}
            """,
            {"owner": owner, "number": number},
        )
        container = data.get(field)
        project = container.get("projectV2") if isinstance(container, dict) else None
        if not isinstance(project, dict):
            raise GitHubError(
                f"GitHub Project not found: {owner_type} {owner}#{number}"
            )
        if bool(project.get("closed")):
            raise GitHubError(
                f"GitHub Project is closed: {owner_type} {owner}#{number}"
            )
        return {
            "id": str(project["id"]),
            "number": int(project["number"]),
            "url": str(project["url"]),
        }

    @staticmethod
    def _project_item_fields() -> str:
        return """
          id
          fullDatabaseId
          updatedAt
          creator { login }
          project { id number url }
          content {
            __typename
            ... on DraftIssue { title body }
            ... on Issue {
              number
              url
              title
              body
              repository { nameWithOwner }
            }
          }
        """

    @staticmethod
    def _parse_project_issue_proposal(value: dict[str, Any]) -> ProjectIssueProposal:
        project = value.get("project")
        content = value.get("content")
        creator = value.get("creator")
        if not isinstance(project, dict) or not isinstance(content, dict):
            raise GitHubError("GitHub Project item is missing project or content")
        content_type = str(content.get("__typename") or "")
        if content_type not in {"DraftIssue", "Issue"}:
            raise GitHubError(
                f"GitHub Project item must contain a draft or issue, got {content_type!r}"
            )
        repository = content.get("repository")
        return ProjectIssueProposal(
            item_id=str(value["id"]),
            database_id=int(value.get("fullDatabaseId") or 0),
            project_id=str(project["id"]),
            project_number=int(project["number"]),
            project_url=str(project["url"]),
            updated_at=str(value["updatedAt"]),
            creator=(
                str(creator.get("login") or "") if isinstance(creator, dict) else ""
            ),
            content_type=content_type,
            title=str(content.get("title") or ""),
            body=str(content.get("body") or ""),
            issue_number=int(content.get("number") or 0),
            issue_url=str(content.get("url") or ""),
            repository=(
                str(repository.get("nameWithOwner") or "")
                if isinstance(repository, dict)
                else ""
            ),
        )

    def add_project_issue_proposal(
        self, *, project_id: str, title: str, body: str, client_mutation_id: str
    ) -> ProjectIssueProposal:
        fields = self._project_item_fields()
        data = self._graphql(
            f"""
            mutation($projectId: ID!, $title: String!, $body: String!,
                     $clientMutationId: String!) {{
              addProjectV2DraftIssue(input: {{
                projectId: $projectId,
                title: $title,
                body: $body,
                clientMutationId: $clientMutationId
              }}) {{
                projectItem {{ {fields} }}
              }}
            }}
            """,
            {
                "projectId": project_id,
                "title": title,
                "body": body,
                "clientMutationId": client_mutation_id,
            },
        )
        result = data.get("addProjectV2DraftIssue")
        item = result.get("projectItem") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            raise GitHubError("GitHub did not return the created Project draft item")
        return self._parse_project_issue_proposal(item)

    def project_issue_proposal(self, item_id: str) -> ProjectIssueProposal:
        fields = self._project_item_fields()
        data = self._graphql(
            f"""
            query($itemId: ID!) {{
              node(id: $itemId) {{
                ... on ProjectV2Item {{ {fields} }}
              }}
            }}
            """,
            {"itemId": item_id},
        )
        item = data.get("node")
        if not isinstance(item, dict) or not item.get("id"):
            raise GitHubError(f"GitHub Project item not found: {item_id}")
        return self._parse_project_issue_proposal(item)

    def project_issue_proposal_by_database_id(
        self,
        *,
        project_id: str,
        database_id: int,
        max_items: int = 1000,
    ) -> ProjectIssueProposal:
        """Resolve the numeric itemId exposed by GitHub Project browser URLs."""
        if database_id < 1:
            raise GitHubError("GitHub Project item database ID must be positive")
        max_items = min(max(max_items, 1), 1000)
        fields = self._project_item_fields()
        cursor: str | None = None
        scanned = 0
        max_pages = (max_items + 99) // 100
        pages = 0
        while scanned < max_items and pages < max_pages:
            pages += 1
            page_size = min(100, max_items - scanned)
            data = self._graphql(
                f"""
                query($projectId: ID!, $cursor: String, $first: Int!) {{
                  node(id: $projectId) {{
                    ... on ProjectV2 {{
                      items(
                        first: $first,
                        after: $cursor,
                        archivedStates: [NOT_ARCHIVED]
                      ) {{
                        nodes {{ {fields} }}
                        pageInfo {{ hasNextPage endCursor }}
                      }}
                    }}
                  }}
                }}
                """,
                {
                    "projectId": project_id,
                    "cursor": cursor,
                    "first": page_size,
                },
            )
            project = data.get("node")
            connection = project.get("items") if isinstance(project, dict) else None
            if not isinstance(connection, dict):
                raise GitHubError(f"GitHub Project not found: {project_id}")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubError("GitHub Project items response was not a list")
            if not nodes:
                break
            scanned += len(nodes)
            for value in nodes:
                if (
                    isinstance(value, dict)
                    and int(value.get("fullDatabaseId") or 0) == database_id
                ):
                    return self._parse_project_issue_proposal(value)
            page_info = connection.get("pageInfo")
            has_next = (
                bool(page_info.get("hasNextPage"))
                if isinstance(page_info, dict)
                else False
            )
            if not has_next:
                break
            cursor_value = page_info.get("endCursor")
            if not isinstance(cursor_value, str) or not cursor_value:
                raise GitHubError("GitHub Project pagination cursor is missing")
            cursor = cursor_value
        raise GitHubError(
            f"active GitHub Project item {database_id} was not found in the first "
            f"{max_items} items; use its GraphQL node ID or archive old proposals"
        )

    def convert_project_issue_proposal(
        self, *, item_id: str, repository: str
    ) -> ProjectIssueProposal:
        owner, name = repository.split("/", 1)
        repository_data = self._graphql(
            """
            query($owner: String!, $name: String!) {
              repository(owner: $owner, name: $name) { id }
            }
            """,
            {"owner": owner, "name": name},
        ).get("repository")
        if not isinstance(repository_data, dict):
            raise GitHubError(f"repository not found: {repository}")
        fields = self._project_item_fields()
        data = self._graphql(
            f"""
            mutation($itemId: ID!, $repositoryId: ID!) {{
              convertProjectV2DraftIssueItemToIssue(input: {{
                itemId: $itemId,
                repositoryId: $repositoryId
              }}) {{
                item {{ {fields} }}
              }}
            }}
            """,
            {"itemId": item_id, "repositoryId": str(repository_data["id"])},
        )
        result = data.get("convertProjectV2DraftIssueItemToIssue")
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            raise GitHubError("GitHub did not return the converted Issue")
        return self._parse_project_issue_proposal(item)

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
            can_push=bool((payload.get("permissions") or {}).get("push", False)),
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

    def similar_issues(
        self, full_name: str, title: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        terms = re.findall(r"[\w.-]{2,}", title, flags=re.UNICODE)[:12]
        if not terms:
            return []
        payload, _ = self._request(
            "GET",
            "/search/issues",
            query={
                "q": f"repo:{full_name} is:issue {' '.join(terms)}",
                "per_page": min(max(limit, 1), 20),
            },
        )
        items = payload.get("items", ()) if isinstance(payload, dict) else ()
        return [
            {
                "number": int(value["number"]),
                "title": str(value.get("title") or "")[:200],
                "state": str(value.get("state") or ""),
                "url": str(value.get("html_url") or ""),
            }
            for value in items[:limit]
        ]

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
        return self._parse_pull_request(payload[0])

    @staticmethod
    def _parse_pull_request(item: dict[str, Any]) -> PullRequest:
        head = item.get("head") or {}
        base = item.get("base") or {}
        head_owner = ((head.get("repo") or {}).get("owner") or {}).get("login") or ""
        return PullRequest(
            number=int(item["number"]),
            url=str(item["html_url"]),
            state=str(item["state"]),
            draft=bool(item.get("draft", False)),
            head_owner=str(head_owner),
            head_branch=str(head.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            base_branch=str(base.get("ref") or ""),
        )

    def pull_request(self, upstream: str, number: int) -> PullRequest:
        payload, _ = self._request("GET", f"/repos/{upstream}/pulls/{number}")
        return self._parse_pull_request(payload)

    def pull_request_activity(self, upstream: str, number: int) -> dict[str, Any]:
        pull, _ = self._request("GET", f"/repos/{upstream}/pulls/{number}")
        comments = self._paginated_rest_values(
            f"/repos/{upstream}/issues/{number}/comments"
        )
        reviews = self._paginated_rest_values(
            f"/repos/{upstream}/pulls/{number}/reviews"
        )
        review_comments = self._paginated_rest_values(
            f"/repos/{upstream}/pulls/{number}/comments"
        )
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        check_runs = self._paginated_rest_values(
            f"/repos/{upstream}/commits/{head_sha}/check-runs",
            container="check_runs",
        )
        return {
            "pull_request": {
                "number": int(pull["number"]),
                "url": str(pull["html_url"]),
                "state": str(pull["state"]),
                "draft": bool(pull.get("draft", False)),
                "updated_at": str(pull.get("updated_at") or ""),
                "head_sha": head_sha,
                "base_branch": str((pull.get("base") or {}).get("ref") or ""),
                "base_sha": str((pull.get("base") or {}).get("sha") or ""),
                "mergeable": pull.get("mergeable"),
                "mergeable_state": str(pull.get("mergeable_state") or ""),
                "merged": bool(pull.get("merged_at")),
            },
            "comments": [
                {
                    "id": int(value["id"]),
                    "author": str((value.get("user") or {}).get("login") or ""),
                    "association": str(value.get("author_association") or ""),
                    "created_at": str(value.get("created_at") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                    "url": str(value.get("html_url") or ""),
                    "body": str(value.get("body") or ""),
                }
                for value in comments
            ],
            "reviews": [
                {
                    "id": int(value["id"]),
                    "author": str((value.get("user") or {}).get("login") or ""),
                    "association": str(value.get("author_association") or ""),
                    "state": str(value.get("state") or ""),
                    "submitted_at": str(value.get("submitted_at") or ""),
                    "url": str(value.get("html_url") or ""),
                    "body": str(value.get("body") or ""),
                }
                for value in reviews
            ],
            "review_comments": [
                {
                    "id": int(value["id"]),
                    "author": str((value.get("user") or {}).get("login") or ""),
                    "association": str(value.get("author_association") or ""),
                    "created_at": str(value.get("created_at") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                    "url": str(value.get("html_url") or ""),
                    "path": str(value.get("path") or ""),
                    "line": value.get("line") or value.get("original_line"),
                    "body": str(value.get("body") or ""),
                }
                for value in review_comments
            ],
            "checks": [
                {
                    "id": int(value["id"]),
                    "name": str(value.get("name") or ""),
                    "status": str(value.get("status") or ""),
                    "conclusion": str(value.get("conclusion") or ""),
                    "url": str(value.get("details_url") or ""),
                }
                for value in check_runs
            ],
        }

    def pull_request_merge_snapshot(self, upstream: str, number: int) -> dict[str, Any]:
        """Read a complete PR decision snapshot without making GitHub writes."""
        owner, name = upstream.split("/", 1)
        cursors: dict[str, str | None] = {
            "files": None,
            "threads": None,
            "checks": None,
        }
        values: dict[str, list[dict[str, Any]]] = {
            "files": [],
            "threads": [],
            "checks": [],
        }
        totals = {"files": 0, "threads": 0, "checks": 0}
        pull: dict[str, Any] | None = None
        for _page in range(MAX_REST_PAGES):
            data = self._graphql(
                """
                query(
                  $owner: String!, $name: String!, $number: Int!,
                  $files: String, $threads: String, $checks: String
                ) {
                  repository(owner: $owner, name: $name) {
                    pullRequest(number: $number) {
                      number state isDraft mergeable reviewDecision
                      headRefOid baseRefOid additions deletions changedFiles
                      mergeCommit { oid }
                      files(first: 100, after: $files) {
                        totalCount
                        nodes { path }
                        pageInfo { hasNextPage endCursor }
                      }
                      reviewThreads(first: 100, after: $threads) {
                        totalCount
                        nodes { id isResolved }
                        pageInfo { hasNextPage endCursor }
                      }
                      commits(last: 1) {
                        nodes {
                          commit {
                            statusCheckRollup {
                              contexts(first: 100, after: $checks) {
                                totalCount
                                nodes {
                                  __typename
                                  ... on CheckRun {
                                    name status conclusion
                                    isRequired(pullRequestNumber: $number)
                                  }
                                  ... on StatusContext {
                                    context state
                                    isRequired(pullRequestNumber: $number)
                                  }
                                }
                                pageInfo { hasNextPage endCursor }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
                """,
                {"owner": owner, "name": name, "number": number, **cursors},
            )
            repository = data.get("repository")
            current = (
                repository.get("pullRequest") if isinstance(repository, dict) else None
            )
            if not isinstance(current, dict):
                raise GitHubError(f"pull request not found: {upstream}#{number}")
            pull = current
            commits = current.get("commits")
            commit_nodes = commits.get("nodes") if isinstance(commits, dict) else []
            latest = (
                commit_nodes[0]
                if isinstance(commit_nodes, list) and commit_nodes
                else {}
            )
            commit = latest.get("commit") if isinstance(latest, dict) else {}
            rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else {}
            connections = {
                "files": current.get("files"),
                "threads": current.get("reviewThreads"),
                "checks": (
                    rollup.get("contexts") if isinstance(rollup, dict) else None
                ),
            }
            pending = False
            for key, connection in connections.items():
                if connection is None and key == "checks":
                    continue
                if not isinstance(connection, dict):
                    raise GitHubError(f"GitHub merge snapshot omitted {key}")
                nodes = connection.get("nodes")
                page_info = connection.get("pageInfo")
                total_count = connection.get("totalCount")
                if (
                    not isinstance(nodes, list)
                    or not isinstance(page_info, dict)
                    or not isinstance(total_count, int)
                ):
                    raise GitHubError(f"GitHub merge snapshot returned invalid {key}")
                totals[key] = total_count
                values[key].extend(value for value in nodes if isinstance(value, dict))
                if bool(page_info.get("hasNextPage")):
                    cursor = str(page_info.get("endCursor") or "")
                    if not cursor or cursor == cursors[key]:
                        raise GitHubError(f"GitHub {key} pagination did not advance")
                    cursors[key] = cursor
                    pending = True
                else:
                    cursors[key] = str(page_info.get("endCursor") or "") or None
            if not pending:
                break
        else:
            raise GitHubError("GitHub merge snapshot pagination exceeded its limit")

        assert pull is not None
        incomplete = [key for key in values if len(values[key]) != totals[key]]
        if incomplete:
            raise GitHubError(
                "GitHub merge snapshot is incomplete: " + ", ".join(incomplete)
            )
        checks = []
        for value in values["checks"]:
            kind = str(value.get("__typename") or "")
            if kind == "CheckRun":
                checks.append(
                    {
                        "name": str(value.get("name") or ""),
                        "status": str(value.get("status") or ""),
                        "conclusion": str(value.get("conclusion") or ""),
                        "required": bool(value.get("isRequired")),
                    }
                )
            elif kind == "StatusContext":
                state = str(value.get("state") or "").casefold()
                checks.append(
                    {
                        "name": str(value.get("context") or ""),
                        "status": "completed" if state != "pending" else "pending",
                        "conclusion": "success" if state == "success" else state,
                        "required": bool(value.get("isRequired")),
                    }
                )
        conversation_state = sorted(
            (
                {
                    "id": str(value.get("id") or ""),
                    "resolved": bool(value.get("isResolved")),
                }
                for value in values["threads"]
            ),
            key=lambda value: value["id"],
        )
        return {
            "repository": upstream.casefold(),
            "pull_number": int(pull["number"]),
            "head_sha": str(pull.get("headRefOid") or ""),
            "base_sha": str(pull.get("baseRefOid") or ""),
            "state": str(pull.get("state") or ""),
            "draft": bool(pull.get("isDraft")),
            "mergeable": str(pull.get("mergeable") or ""),
            "review_decision": str(pull.get("reviewDecision") or ""),
            "unresolved_conversations": sum(
                not bool(value.get("isResolved")) for value in values["threads"]
            ),
            "conversation_digest": _canonical_digest(conversation_state),
            "files": sorted(
                {
                    str(value.get("path") or "")
                    for value in values["files"]
                    if value.get("path")
                }
            ),
            "additions": int(pull.get("additions") or 0),
            "deletions": int(pull.get("deletions") or 0),
            "checks": sorted(checks, key=lambda value: value["name"].casefold()),
            "files_complete": True,
            "conversations_complete": True,
            "checks_complete": True,
            "merge_commit_sha": str(((pull.get("mergeCommit") or {}).get("oid")) or ""),
        }

    def merge_pull_request(
        self,
        upstream: str,
        number: int,
        *,
        head_sha: str,
        method: str,
    ) -> dict[str, Any]:
        """Merge one exact PR head and return GitHub's normalized result."""
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError(f"unsupported merge method: {method!r}")
        payload, _ = self._request(
            "PUT",
            f"/repos/{upstream}/pulls/{number}/merge",
            data={"sha": head_sha, "merge_method": method},
        )
        if not isinstance(payload, dict):
            raise GitHubError("GitHub merge response was not an object")
        return {
            "merged": bool(payload.get("merged")),
            "sha": str(payload.get("sha") or ""),
            "message": str(payload.get("message") or ""),
        }

    def reopen_pull_request(
        self,
        upstream: str,
        number: int,
        *,
        owner: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequest:
        current = self.pull_request(upstream, number)
        if current.state not in {"closed", "open"}:
            raise GitHubError(
                f"pull request {upstream}#{number} cannot be reopened from {current.state!r}"
            )
        expected = (owner.casefold(), branch, base)
        actual = (
            current.head_owner.casefold(),
            current.head_branch,
            current.base_branch,
        )
        if actual != expected:
            raise GitHubError(
                f"pull request {upstream}#{number} does not match "
                f"{owner}:{branch} -> {base}"
            )
        payload, _ = self._request(
            "PATCH",
            f"/repos/{upstream}/pulls/{number}",
            data={"title": title, "body": body, "state": "open"},
        )
        return self._parse_pull_request(payload)

    def close_pull_request(self, upstream: str, number: int) -> PullRequest:
        payload, _ = self._request(
            "PATCH",
            f"/repos/{upstream}/pulls/{number}",
            data={"state": "closed"},
        )
        return self._parse_pull_request(payload)

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
        return self._parse_pull_request(payload)
