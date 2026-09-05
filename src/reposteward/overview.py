"""Local-first attention across linked projects, with explicit remote refresh."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .config import AppConfig
from .external_tasks import ExternalTasks
from .external_verification import ExternalVerification
from .github import GitHubClient
from .inbox import build_maintainer_inbox
from .portfolio_fetch import portfolio_from_pulls
from .projects import ProjectRegistry, canonical_digest
from .store import Store, utc_now


def _age(value: str, now: datetime) -> int | None:
    try:
        parsed = datetime.fromisoformat(value)
        return max(0, int((now - parsed).total_seconds()))
    except (ValueError, TypeError):
        return None


def _fact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _fact(item)
            for key, item in value.items()
            if key
            not in {"observed_at", "source_age_seconds", "fetched_at", "attempted_at"}
        }
    if isinstance(value, list):
        return [_fact(item) for item in value]
    return value


class ProjectOverview:
    def __init__(self, config: AppConfig, *, github: GitHubClient | None = None):
        self.config = config
        self.registry = ProjectRegistry(config.state_dir / "projects.sqlite3")
        self.tasks = ExternalTasks(config)
        self.github = github

    def _projects(self, limit: int) -> dict:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("project limit must be between 1 and 50")
        registry = self.registry.list(limit=500)
        api_host = urlsplit(self.config.github.api_url).hostname
        host = "github.com" if api_host == "api.github.com" else api_host
        enabled, excluded = [], 0
        for project in registry["projects"]:
            policy = self.config.repositories.get(project["repository"].casefold())
            if (
                policy is None
                or not policy.enabled
                or not project["workspace_count"]
                or project["host"] != host
            ):
                excluded += 1
                continue
            enabled.append(project)
        return {
            "projects": enabled[:limit],
            "omitted": max(0, len(enabled) - limit) + registry["omitted"],
            "excluded": excluded,
        }

    def refresh(self, *, project_limit: int = 10, item_limit: int = 10) -> dict:
        if type(item_limit) is not int or not 1 <= item_limit <= 50:
            raise ValueError("per-project item limit must be between 1 and 50")
        selected = self._projects(project_limit)
        if not selected["projects"]:
            return self.show(project_limit=project_limit, item_limit=item_limit)
        store = self.tasks._store(write=True)
        github = self.github
        for project in selected["projects"]:
            attempted = utc_now()
            try:
                github = github or GitHubClient(self.config.github)
                policy = self.config.repositories[project["repository"].casefold()]
                portfolio = portfolio_from_pulls(
                    github, policy, github.open_pull_requests(policy.name), max_pulls=50
                )
                encoded = json.dumps(portfolio)
                if len(encoded.encode()) > 2_000_000:
                    raise ValueError("portfolio exceeds cache size limit")
                with store._connection() as db:
                    db.execute(
                        """INSERT INTO project_inbox_cache VALUES (?,?,?,?,?,'')
                        ON CONFLICT(project_id) DO UPDATE SET repository=excluded.repository,
                        portfolio=excluded.portfolio,fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at,error=''""",
                        (
                            project["id"],
                            policy.name.casefold(),
                            encoded,
                            attempted,
                            attempted,
                        ),
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                with store._connection() as db:
                    db.execute(
                        """INSERT INTO project_inbox_cache(project_id,repository,attempted_at,error) VALUES (?,?,?,?)
                        ON CONFLICT(project_id) DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error""",
                        (
                            project["id"],
                            project["repository"],
                            attempted,
                            str(exc)[:500],
                        ),
                    )
        return self.show(project_limit=project_limit, item_limit=item_limit)

    def _external_items(
        self, store: Store, project: dict
    ) -> tuple[list[dict], int, int]:
        with store._connection() as db:
            rows = db.execute(
                """WITH ranked AS (
                SELECT e.*,h.work_item_id,r.issue_number,r.details,
                    row_number() OVER (PARTITION BY e.binding_id,h.work_item_id ORDER BY e.updated_at DESC,e.created_at DESC,e.rowid DESC) AS rank
                FROM external_task_runs e JOIN runs r ON r.id=e.run_id JOIN harness_runs h ON h.run_id=e.run_id
                WHERE e.project_id=? AND r.stage='external' AND r.status='running')
                SELECT * FROM ranked WHERE rank=1 ORDER BY updated_at DESC,run_id LIMIT 21""",
                (project["id"],),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) FROM external_task_runs e JOIN runs r ON r.id=e.run_id WHERE e.project_id=? AND r.stage='external' AND r.status='running'",
                (project["id"],),
            ).fetchone()[0]
            unique = db.execute(
                """SELECT count(*) FROM (
                SELECT e.binding_id,h.work_item_id FROM external_task_runs e JOIN runs r ON r.id=e.run_id
                JOIN harness_runs h ON h.run_id=e.run_id WHERE e.project_id=? AND r.stage='external' AND r.status='running'
                GROUP BY e.binding_id,h.work_item_id)""",
                (project["id"],),
            ).fetchone()[0]
        items = []
        for row in rows[:20]:
            item = {
                "id": "external:" + row["run_id"],
                "run_id": row["run_id"],
                "repository": project["repository"],
                "issue_number": row["issue_number"],
                "pull_number": 0,
                "source_updated_at": row["updated_at"],
                "trace_command": f"reposteward trace {project['repository']} {row['issue_number']}",
                "next_command": f"reposteward task inspect {row['run_id']} --live",
            }
            try:
                task = self.tasks.inspect(row["run_id"], live=True)
                checkpoint = task["checkpoint"] or {}
                item.update(
                    {
                        "revision": task["revision"],
                        "dirty": task["current_snapshot"]["dirty"],
                        "snapshot_digest": task["current_snapshot"]["digest"],
                        "open_items": len(checkpoint.get("remaining", [])),
                        "next_action": checkpoint.get("next_action", "")[:1000],
                        "next_action_omitted": max(
                            0, len(checkpoint.get("next_action", "")) - 1000
                        ),
                        "checkpoint_evidence_id": f"checkpoint:{row['run_id']}:{task['revision']}",
                        "validity": task["validity"],
                    }
                )
                if checkpoint.get("blockers"):
                    code, priority, summary = (
                        "external_blocked",
                        95,
                        "外部开发任务存在阻塞，查看检查点",
                    )
                elif any(
                    value != "workspace_changed_since_checkpoint"
                    for value in task["validity"]
                ):
                    code, priority, summary = (
                        "external_refresh_required",
                        85,
                        "任务绑定的基线或策略变化，需要刷新",
                    )
                else:
                    verification = ExternalVerification(self.config)
                    evidence = verification.list(row["run_id"], limit=1)["evidence"]
                    if not evidence:
                        code, priority, summary = (
                            "external_verification_required",
                            45,
                            "开发任务尚无独立验证证据",
                        )
                    else:
                        latest = verification.inspect(
                            row["run_id"],
                            evidence[0]["evidence_id"].split(":")[1],
                            live=True,
                        )
                        item["evidence_id"] = latest["evidence_id"]
                        item["verification_outcome"] = latest["outcome"]
                        if latest["outcome"] in {"unknown", "cancelled", "running"}:
                            code, priority, summary = (
                                "external_verification_unknown",
                                85,
                                "验证未完成或结果未知，需要检查证据",
                            )
                        elif latest["outcome"] == "failed":
                            code, priority, summary = (
                                "external_verification_failed",
                                100,
                                "开发快照验证失败，需要处理",
                            )
                        elif latest["current_applicability"] != "matches":
                            code, priority, summary = (
                                "external_verification_stale",
                                75,
                                "旧验证不再匹配当前开发快照",
                            )
                        else:
                            code, priority, summary = (
                                "external_review_required",
                                50,
                                "开发快照验证通过，待检查余项和干净提交的 adopt",
                            )
                item.update(
                    {
                        "reason_code": code,
                        "priority": priority,
                        "summary": summary,
                        "complete": True,
                    }
                )
            except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
                item.update(
                    {
                        "reason_code": "external_state_unknown",
                        "priority": 110,
                        "summary": f"本地任务事实无法确认：{str(exc)[:300]}",
                        "complete": False,
                    }
                )
            items.append(item)
        return items, max(0, total - unique), max(0, unique - 20)

    def _project(
        self, store: Store, project: dict, *, limit: int, now: datetime
    ) -> dict:
        with store._connection() as db:
            cache_row = db.execute(
                "SELECT * FROM project_inbox_cache WHERE project_id=?", (project["id"],)
            ).fetchone()
        cache = dict(cache_row) if cache_row else {}
        portfolio = json.loads(cache["portfolio"]) if cache.get("fetched_at") else None
        runs = store.latest_runs_for_repository(
            project["repository"], limit=500, include_external=False
        )
        proposals = store.staged_issue_proposals(project["repository"], limit=500)
        native = build_maintainer_inbox(
            project["repository"],
            proposals=proposals,
            runs=runs,
            portfolio=portfolio,
            merge_outcomes=store.latest_merge_outcomes(project["repository"]),
            observed_at=cache.get("attempted_at", ""),
            error=cache.get("error", ""),
            limit=500,
        )
        items = list(native["items"])
        for item in items:
            if item["run_id"]:
                item["trace_command"] = (
                    f"reposteward trace {project['repository']} {item['issue_number']}"
                )
        with store._connection() as db:
            pending = db.execute(
                """SELECT e.pull_number,count(*) AS count,
                max(e.ingested_at) AS updated_at,sum(CASE WHEN b.digest IS NULL THEN 1 ELSE 0 END) AS missing
                FROM feedback_items f JOIN github_pr_events e ON e.sequence=f.event_sequence
                LEFT JOIN content_blobs b ON b.digest=e.payload_digest
                WHERE e.repository=? AND f.status IN ('pending','deferred','addressed')
                GROUP BY e.pull_number ORDER BY e.pull_number LIMIT 501""",
                (project["repository"],),
            ).fetchall()
        for row in pending[:500]:
            items.append(
                {
                    "id": f"feedback:{project['id']}:{row['pull_number']}",
                    "repository": project["repository"],
                    "run_id": "",
                    "issue_number": 0,
                    "pull_number": row["pull_number"],
                    "priority": 105 if row["missing"] else 90,
                    "reason_code": "feedback_evidence_missing"
                    if row["missing"]
                    else "review_feedback_required",
                    "summary": f"PR #{row['pull_number']} 仍有 {row['count']} 条本地未处理反馈",
                    "pending_feedback": row["count"],
                    "unknown_payloads": row["missing"],
                    "source_updated_at": row["updated_at"],
                    "next_command": "reposteward overview refresh --project-limit 10",
                    "complete": not row["missing"],
                }
            )
        external, collapsed, external_omitted = self._external_items(store, project)
        unique = {}
        for item in [*items, *external]:
            key = (
                (item["pull_number"], item["reason_code"])
                if item["pull_number"]
                else item["id"]
            )
            if key in unique:
                previous = unique[key]
                unique[key] = {
                    **previous,
                    **item,
                    "run_id": previous.get("run_id") or item.get("run_id", ""),
                    "next_command": previous["next_command"],
                }
                collapsed += 1
            else:
                unique[key] = item
        ordered = sorted(
            unique.values(),
            key=lambda item: (-item["priority"], item["repository"], item["id"]),
        )
        for item in ordered:
            item["source_age_seconds"] = _age(item["source_updated_at"], now)
        incomplete = (
            native["omitted_count"]
            or len(runs) == 500
            or len(proposals) == 500
            or len(pending) > 500
            or external_omitted
        )
        return {
            "project_id": project["id"],
            "repository": project["repository"],
            "items": ordered[:limit],
            "omitted": max(0, len(ordered) - limit)
            + native["omitted_count"]
            + external_omitted,
            "scan_incomplete": bool(incomplete),
            "duplicates_collapsed": collapsed,
            "complete": native["complete"]
            and not incomplete
            and all(item.get("complete", True) for item in ordered),
            "sources": {
                "kind": "local_cache",
                "fetched_at": cache.get("fetched_at", ""),
                "attempted_at": cache.get("attempted_at", ""),
                "source_age_seconds": _age(cache.get("fetched_at", ""), now),
                "error": cache.get("error", ""),
                "status": "refresh_failed"
                if cache.get("error")
                else "cached"
                if portfolio is not None
                else "not_refreshed",
            },
        }

    def show(
        self,
        *,
        project_limit: int = 10,
        item_limit: int = 10,
        previous_digest: str = "",
    ) -> dict:
        if type(item_limit) is not int or not 1 <= item_limit <= 50:
            raise ValueError("per-project item limit must be between 1 and 50")
        if previous_digest and not re.fullmatch(r"[0-9a-f]{64}", previous_digest):
            raise ValueError("previous overview digest must be SHA-256")
        selected = self._projects(project_limit)
        now = datetime.now(UTC)
        projects = []
        for project in selected["projects"]:
            try:
                report = self._project(
                    self.tasks._store(), project, limit=item_limit, now=now
                )
            except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
                report = {
                    "project_id": project["id"],
                    "repository": project["repository"],
                    "complete": False,
                    "items": [
                        {
                            "id": f"project:{project['id']}:unknown",
                            "priority": 110,
                            "reason_code": "project_state_unknown",
                            "summary": str(exc)[:300],
                            "next_command": "reposteward overview refresh",
                        }
                    ],
                    "omitted": 0,
                    "scan_incomplete": True,
                    "duplicates_collapsed": 0,
                    "sources": {"status": "unknown"},
                }
            projects.append(report)
        facts = {
            "schema_version": 1,
            "projects": projects,
            "omitted_projects": selected["omitted"],
            "excluded_projects": selected["excluded"],
            "complete": not selected["omitted"]
            and all(project["complete"] for project in projects),
            "duplicates_collapsed": sum(
                project["duplicates_collapsed"] for project in projects
            ),
        }
        digest = canonical_digest(_fact(facts))
        return {
            **facts,
            "overview_digest": digest,
            "unchanged": digest == previous_digest if previous_digest else None,
            "observed_at": now.isoformat(),
            "repeat_policy": "same facts coalesce; new blockers and unresolved feedback remain visible",
            "harness_invoked": False,
            "workspace_modified": False,
            "public_write": False,
        }


def render_overview(result: dict) -> str:
    lines = [
        f"Projects: {len(result['projects'])}; omitted: {result['omitted_projects']}"
    ]
    for project in result["projects"]:
        lines.append(f"\n{project['repository']} ({project['sources']['status']})")
        for item in project["items"]:
            lines.append(
                f"P{item['priority']} {item['summary']}\n  {item['next_command']}"
            )
    return "\n".join(lines)
