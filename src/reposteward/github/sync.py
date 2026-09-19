"""Explicit GitHub reads and immutable observations, separate from merge audits."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from reposteward.github.client import GitHubClient, GitHubReadError
from reposteward.storage.local_queue import digest, encoded
from reposteward.storage.store import Store, utc_now


def account_key(config) -> str:
    return digest(
        {
            "api": config.github.api_url.rstrip("/").casefold(),
            "login": config.github.login.casefold(),
        }
    )


def observations(store: Store, account: str, project: str) -> dict:
    with store._connection() as db:
        rows = db.execute(
            """SELECT s.*,o.body,o.body_digest,o.complete,o.fetched_at
            FROM github_sources s LEFT JOIN github_observations o ON o.id=s.observation_id
            WHERE s.account_digest=? AND s.project_id=? ORDER BY
            CASE WHEN s.source IN ('pulls','activity','issues') THEN 0 ELSE 1 END,
            s.last_success DESC,s.source LIMIT 500""",
            (account, project),
        ).fetchall()
    result = {}
    for row in rows:
        value = dict(row)
        body = json.loads(value.pop("body")) if row["body"] else None
        if body is not None and digest(body) != value["body_digest"]:
            raise ValueError("GitHub observation changed")
        try:
            age = max(
                0,
                int(
                    (
                        datetime.now(UTC)
                        - datetime.fromisoformat(value["last_success"])
                    ).total_seconds()
                ),
            )
        except (ValueError, TypeError):
            age = None
        result[value["source"]] = {
            "age_seconds": age,
            "stale": age is None or age > 900,
            **value,
            "body": body,
            "status": "failed"
            if value["error_code"]
            else "cached"
            if body is not None
            else "unknown",
        }
    return result


def pull_item(raw: dict) -> dict:
    number = raw.get("number")
    if (
        type(number) is not int
        or number <= 0
        or raw.get("state") not in {"open", "closed"}
    ):
        raise GitHubReadError("invalid_response")
    state = (
        "merged" if raw.get("merged_at") or raw.get("merged") is True else raw["state"]
    )
    head = raw.get("head") or {}
    return {
        "number": number,
        "title": str(raw.get("title", ""))[:500],
        "state": state,
        "draft": bool(raw.get("draft")),
        "updated_at": str(raw.get("updated_at", ""))[:40],
        "merged_at": str(raw.get("merged_at") or "")[:40],
        "author": str((raw.get("user") or {}).get("login", ""))[:100],
        "head_sha": str(head.get("sha", ""))[:64],
    }


class GitHubSync:
    """Each response is fenced by the operation lease before local persistence."""

    def __init__(
        self,
        store: Store,
        client: GitHubClient,
        *,
        account: str,
        project: dict,
        operation: dict,
        guard: Callable[[], None],
    ):
        self.store, self.client = store, client
        self.account, self.project, self.operation = account, project, operation
        self.guard = guard
        self.prefix = "/repos/" + project["repository"]
        self.calls = 0

    def read(self, path: str, query: dict | None = None):
        if self.calls >= 120:
            raise GitHubReadError("request_budget")
        key = digest({"path": path, "query": query or {}})
        with self.store._connection() as db:
            cached = db.execute(
                "SELECT * FROM github_request_cache WHERE account_digest=? AND request_key=?",
                (self.account, key),
            ).fetchone()
        old = json.loads(cached["body"]) if cached else None
        if cached and digest(old) != cached["body_digest"]:
            raise ValueError("GitHub request cache changed")
        self.guard()
        self.calls += 1
        reply = self.client.conditional_get(
            path, query=query, etag=cached["etag"] if cached else ""
        )
        self.guard()
        if reply.not_modified:
            if cached is None:
                raise GitHubReadError("invalid_response")
            body, has_more = old, bool(cached["has_more"])
        else:
            body, has_more = reply.body, reply.has_more
        raw = encoded(body)
        if len(raw.encode()) > 2_000_000:
            raise GitHubReadError("response_limit")
        with self.store.atomic(), self.store._connection() as db:
            self.guard()
            db.execute(
                """INSERT INTO github_request_cache VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(account_digest,request_key) DO UPDATE SET etag=excluded.etag,
                body=excluded.body,body_digest=excluded.body_digest,has_more=excluded.has_more,checked_at=excluded.checked_at""",
                (
                    self.account,
                    key,
                    reply.etag,
                    raw,
                    digest(body),
                    int(has_more),
                    utc_now(),
                ),
            )
        return body, has_more

    def source(self, name: str, fetch: Callable[[], tuple[dict, bool]]) -> bool:
        attempted = utc_now()
        try:
            try:
                body, complete = fetch()
            except (KeyError, TypeError, AttributeError) as exc:
                raise GitHubReadError("invalid_response") from exc
            if len(encoded(body).encode()) > 2_000_000:
                raise GitHubReadError("response_limit")
            with self.store.atomic(), self.store._connection() as db:
                self.guard()
                identifier, now = uuid.uuid4().hex, utc_now()
                db.execute(
                    "INSERT INTO github_observations VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        self.account,
                        self.project["id"],
                        name,
                        encoded(body),
                        digest(body),
                        int(complete),
                        now,
                        self.operation["id"],
                    ),
                )
                db.execute(
                    """INSERT INTO github_sources VALUES (?,?,?,?,?,?,'','')
                    ON CONFLICT(account_digest,project_id,source) DO UPDATE SET
                    observation_id=excluded.observation_id,last_attempt=excluded.last_attempt,
                    last_success=excluded.last_success,error_code='',retry_at=''""",
                    (
                        self.account,
                        self.project["id"],
                        name,
                        identifier,
                        attempted,
                        now,
                    ),
                )
                self._result(
                    db,
                    name,
                    {
                        "status": "completed",
                        "complete": complete,
                        "observation_id": identifier,
                    },
                )
            return True
        except GitHubReadError as exc:
            with self.store.atomic(), self.store._connection() as db:
                self.guard()
                db.execute(
                    """INSERT INTO github_sources(account_digest,project_id,source,last_attempt,error_code,retry_at)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(account_digest,project_id,source) DO UPDATE SET
                    last_attempt=excluded.last_attempt,error_code=excluded.error_code,retry_at=excluded.retry_at""",
                    (
                        self.account,
                        self.project["id"],
                        name,
                        attempted,
                        exc.code,
                        exc.retry_at,
                    ),
                )
                self._result(
                    db,
                    name,
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "retry_at": exc.retry_at,
                    },
                )
                if exc.retry_at:
                    db.execute(
                        "INSERT INTO github_account_limits VALUES (?,?,?) ON CONFLICT(account_digest) DO UPDATE SET retry_at=excluded.retry_at,reason=excluded.reason",
                        (self.account, exc.retry_at, exc.code),
                    )
            if exc.retry_at:
                raise
            return False

    def _result(self, db, stage: str, value: dict):
        db.execute(
            "INSERT INTO local_operation_results VALUES (?,?,?,?,?,?)",
            (
                self.operation["id"],
                self.operation["lease_generation"],
                stage,
                encoded(value),
                digest(value),
                utc_now(),
            ),
        )

    def window(self, kind: str) -> tuple[dict, bool]:
        rows, more = [], False
        for page in (1, 2):
            raw, more = self.read(
                self.prefix + ("/issues" if kind == "issues" else "/pulls"),
                {
                    "state": "all" if kind == "activity" else "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 50,
                    "page": page,
                },
            )
            if (
                not isinstance(raw, list)
                or len(raw) > 50
                or any(not isinstance(row, dict) for row in raw)
            ):
                raise GitHubReadError("invalid_response")
            for row in raw:
                if kind == "issues" and "pull_request" in row:
                    continue
                item = pull_item(row)
                if kind == "issues":
                    item["labels"] = [
                        str(label.get("name", ""))[:100]
                        for label in row.get("labels", [])[:20]
                        if isinstance(label, dict)
                    ]
                rows.append(item)
            if not more:
                break
        unique = {row["number"]: row for row in rows}
        return {
            "items": list(unique.values()),
            "has_more": more,
            "total": None if more else len(unique),
            "window": "recently_updated" if kind == "activity" else "open",
            "pages": page,
        }, not more

    def known(self) -> tuple[list[int], int]:
        with self.store._connection() as db:
            numbers = {
                int(row[0])
                for row in db.execute(
                    "SELECT DISTINCT pull_number FROM github_pr_events WHERE repository=? ORDER BY sequence DESC LIMIT 201",
                    (self.project["repository"].casefold(),),
                )
                if row[0] > 0
            }
            for row in db.execute(
                "SELECT details FROM runs WHERE repository=? ORDER BY updated_at DESC LIMIT 500",
                (self.project["repository"].casefold(),),
            ):
                details = json.loads(row[0])
                match = re.search(
                    r"/pull/([1-9][0-9]*)$", str(details.get("pr_url", ""))
                )
                if match:
                    numbers.add(int(match[1]))
        for name, source in observations(
            self.store, self.account, self.project["id"]
        ).items():
            if name.startswith("pull:"):
                numbers.add(int(name[5:]))
            if name in {"pulls", "activity"} and source["body"]:
                numbers.update(row["number"] for row in source["body"]["items"])
        ordered = sorted(numbers, reverse=True)
        return ordered[:50], max(0, len(ordered) - 50)

    def detail(self, number: int) -> tuple[dict, bool]:
        raw, _ = self.read(f"{self.prefix}/pulls/{number}")
        if not isinstance(raw, dict):
            raise GitHubReadError("invalid_response")
        item = pull_item(raw)
        if item["number"] != number:
            raise GitHubReadError("invalid_response")
        item["body"] = str(raw.get("body") or "")[:4000]
        return item, True

    def checks(self, number: int) -> tuple[dict, bool]:
        sources = observations(self.store, self.account, self.project["id"])
        pull = sources.get(f"pull:{number}", {}).get("body")
        sha = (pull or {}).get("head_sha", "")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise GitHubReadError("head_unknown")
        checks, more_checks = self.read(
            f"{self.prefix}/commits/{sha}/check-runs", {"per_page": 100}
        )
        status, more_status = self.read(
            f"{self.prefix}/commits/{sha}/status", {"per_page": 100}
        )
        reviews, more_reviews = self.read(
            f"{self.prefix}/pulls/{number}/reviews", {"per_page": 100}
        )
        if (
            not isinstance(checks, dict)
            or not isinstance(status, dict)
            or not isinstance(reviews, list)
        ):
            raise GitHubReadError("invalid_response")
        runs = [
            {
                "name": str(row.get("name", ""))[:200],
                "status": str(row.get("status", ""))[:40],
                "conclusion": str(row.get("conclusion") or "")[:40],
            }
            for row in checks.get("check_runs", [])[:100]
        ]
        review_rows = [
            {
                "author": str((row.get("user") or {}).get("login", ""))[:100],
                "state": str(row.get("state", ""))[:40],
                "submitted_at": str(row.get("submitted_at", ""))[:40],
                "commit_id": str(row.get("commit_id", ""))[:64],
            }
            for row in reviews[:100]
        ]
        return {
            "head_sha": sha,
            "checks": runs,
            "status": str(status.get("state", "unknown"))[:40],
            "reviews": review_rows,
        }, not (more_checks or more_status or more_reviews)

    def execute(self) -> dict:
        login, _ = self.read("/user")
        if (
            not isinstance(login, dict)
            or str(login.get("login", "")).casefold()
            != self.client.config.login.casefold()
        ):
            raise GitHubReadError("account_changed")
        success = []
        for kind in ("pulls", "activity", "issues"):
            success.append(self.source(kind, lambda kind=kind: self.window(kind)))
        known, omitted = self.known()
        for number in known:
            success.append(
                self.source(f"pull:{number}", lambda number=number: self.detail(number))
            )
        # Detail state gets priority over potentially expensive CI/review reads.
        sources = observations(self.store, self.account, self.project["id"])
        open_numbers = [
            n
            for n in known
            if (sources.get(f"pull:{n}", {}).get("body") or {}).get("state") == "open"
        ]
        for number in open_numbers[:15]:
            success.append(
                self.source(
                    f"checks:{number}", lambda number=number: self.checks(number)
                )
            )
        return {
            "status": "completed" if all(success) else "partial",
            "sources_ok": sum(success),
            "sources_failed": len(success) - sum(success),
            "known_omitted": omitted,
            "checks_omitted": max(0, len(open_numbers) - 15),
            "requests": self.calls,
            "confirmed_at": datetime.now(UTC).isoformat(),
        }
