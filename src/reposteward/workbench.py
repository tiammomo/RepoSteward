"""Read-only application queries for the local maintainer workbench."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from .config import AppConfig
from .external_tasks import ExternalTasks, TaskConflict
from .external_verification import ExternalVerification
from .lifecycle import build_lifecycle_trace
from .overview import ProjectOverview
from .projects import ProjectError, ProjectRegistry
from .runtime import local_diagnostics
from .store import Store
from .understanding import Understanding


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("invalid local identifier")
    return value


class Workbench:
    """Compose existing reads; never construct Pipeline or a writable Store."""

    def __init__(self, config: AppConfig):
        api = urlsplit(config.github.api_url)
        if api.username or api.password or api.query or api.fragment:
            raise ValueError("correct the API URL using doctor --local first")
        self.config = config
        self.registry = ProjectRegistry(config.state_dir / "projects.sqlite3")
        self.tasks_service = ExternalTasks(config)
        self.understanding = Understanding(config.state_dir / "understanding")
        self.config_files = [Path(path) for _, path in config.config_files]
        self.config_signatures = self._signatures()

    def _signatures(self) -> list:
        return [
            (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            for p in self.config_files
            for stat in (p.stat(),)
        ]

    def check_configuration(self) -> None:
        try:
            if self._signatures() == self.config_signatures:
                return
        except OSError:
            pass
        raise TaskConflict("configuration changed; restart the local workbench")

    def _command(self, *arguments: str) -> str:
        return shlex.join(
            ["reposteward", "--config", str(self.config.path), *arguments]
        )

    def _store(self) -> Store:
        return Store(self.config.state_dir / "reposteward.sqlite3", read_only=True)

    def _role(self, project: dict) -> dict:
        policy = self.config.repositories.get(project["repository"].casefold())
        api_host = urlsplit(self.config.github.api_url).hostname
        host = "github.com" if api_host == "api.github.com" else api_host
        enabled = bool(policy and policy.enabled and project["host"] == host)
        return {
            "mode": policy.mode if policy else "unconfigured",
            "enabled": enabled,
            "task_access": enabled,
        }

    def _project(self, project_id: str, *, tasks: bool = False) -> dict:
        identifier(project_id)
        with self.registry.connection() as db:
            row = (
                db.execute(
                    "SELECT * FROM projects WHERE id=?", (project_id,)
                ).fetchone()
                if db
                else None
            )
        if row is None:
            raise KeyError("registered project not found")
        project = dict(row)
        if tasks and not self._role(project)["task_access"]:
            raise ProjectError("project needs an enabled repository policy")
        return project

    def _binding(self, project_id: str, binding_id: str) -> dict:
        self._project(project_id)
        identifier(binding_id)
        with self.registry.connection() as db:
            row = db.execute(
                "SELECT root FROM workspace_bindings WHERE id=? AND project_id=? AND active=1",
                (binding_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError("active project workspace not found")
        linked = self.registry.inspect(Path(row["root"]))
        if (
            linked["project"]["id"] != project_id
            or linked["binding"]["id"] != binding_id
        ):
            raise ProjectError("workspace binding changed")
        return linked

    def projects(self) -> dict:
        listing = self.registry.list(limit=50)
        return {
            **listing,
            "projects": [{**p, "policy": self._role(p)} for p in listing["projects"]],
        }

    def overview(self) -> dict:
        return ProjectOverview(self.config).show(project_limit=20, item_limit=15)

    def workspace(self, project_id: str, binding_id: str, focus: str = "") -> dict:
        linked = self._binding(project_id, binding_id)
        root = Path(linked["binding"]["root"])
        role = self._role(linked["project"])
        guide = self.understanding.guide(
            root,
            mode="contributor" if role["mode"] == "contributor" else "maintainer",
            focus=focus,
        )
        if self._binding(project_id, binding_id)["binding"] != linked["binding"]:
            raise TaskConflict("workspace binding changed during the read")
        return {
            **linked,
            "policy": role,
            "guide": guide,
            "commands": {
                "scan": self._command("understand", "scan", str(root)),
                "mcp_clients": {
                    client: self._command(
                        "mcp", "config", str(root), "--client", client
                    )
                    for client in ("codex", "claude-code", "copilot-vscode")
                },
            },
            "command_shell": "POSIX",
        }

    def code(
        self,
        project_id: str,
        binding_id: str,
        evidence_id: str,
        start_line: str = "1",
    ) -> dict:
        if not re.fullmatch(r"code:[0-9a-f]{64}", evidence_id) or not re.fullmatch(
            r"[1-9][0-9]{0,6}", start_line
        ):
            raise ValueError("invalid code evidence request")
        linked = self._binding(project_id, binding_id)
        result = self.understanding.evidence(
            Path(linked["binding"]["root"]),
            evidence_id,
            start_line=int(start_line),
            limit=80,
        )
        if self._binding(project_id, binding_id)["binding"] != linked["binding"]:
            raise TaskConflict("workspace binding changed during the read")
        return result

    def tasks(self, project_id: str) -> dict:
        project = self._project(project_id, tasks=True)
        path = self.config.state_dir / "reposteward.sqlite3"
        if not path.exists():
            return {
                "tasks": [],
                "omitted": 0,
                "status": "missing",
                "public_write": False,
            }
        store = self._store()
        with store._connection() as db:
            total = db.execute(
                "SELECT count(*) FROM runs WHERE repository=?",
                (project["repository"].casefold(),),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT r.id,r.issue_number,r.stage,r.status,r.updated_at,
                    substr(w.title,1,300) AS title FROM runs r
                    LEFT JOIN harness_runs h ON h.run_id=r.id
                    LEFT JOIN work_items w ON w.id=h.work_item_id
                    WHERE r.repository=? ORDER BY r.updated_at DESC,r.id LIMIT 50""",
                (project["repository"].casefold(),),
            ).fetchall()
        return {
            "tasks": [dict(row) for row in rows],
            "omitted": max(0, total - len(rows)),
            "status": "available",
            "selection": "recent_attempts",
            "public_write": False,
        }

    def _run(self, project_id: str, run_id: str) -> tuple[Store, dict]:
        project = self._project(project_id, tasks=True)
        identifier(run_id)
        store = self._store()
        run = store.run(run_id)
        if (
            run is None
            or run["repository"].casefold() != project["repository"].casefold()
        ):
            raise KeyError("task is outside the selected project")
        if run["stage"] == "external":
            record = self.tasks_service._record(store, run_id)
            if record["project_id"] != project_id:
                raise ProjectError("external task project binding changed")
            linked = self._binding(project_id, record["binding_id"])
            if linked["binding"]["fingerprint"] != record["binding_fingerprint"]:
                raise ProjectError("external task workspace binding changed")
        return store, run

    def task(self, project_id: str, run_id: str) -> dict:
        store, run = self._run(project_id, run_id)
        external = run["stage"] == "external"
        context = (
            self.tasks_service.context(run_id, live=True)
            if external
            else store.context_bundle(run_id)
        )
        self._run(project_id, run_id)
        return {
            "run_id": run_id,
            "kind": "external" if external else "managed",
            "context": context,
            "context_status": "available" if context else "missing",
            "current_applicability": "see_context_validity"
            if external
            else "not_checked",
            "command": self._command(
                *[
                    "task" if external else "context",
                    "context" if external else "inspect",
                    run_id,
                ]
                + (["--live"] if external else [])
            ),
            "public_write": False,
        }

    def review(self, project_id: str, run_id: str) -> dict:
        _, run = self._run(project_id, run_id)
        trace = build_lifecycle_trace(
            self.config.state_dir,
            run["repository"],
            run["issue_number"],
            event_limit=60,
        )
        verification = None
        if run["stage"] == "external":
            service = ExternalVerification(self.config)
            listing = service.list(run_id, limit=3)
            verification = {
                **listing,
                "evidence": [
                    service.inspect(run_id, e["evidence_id"].split(":")[1], live=True)
                    for e in listing["evidence"]
                ],
            }
        self._run(project_id, run_id)
        return {
            "run_id": run_id,
            "trace": trace,
            "verification": verification,
            "decision_freshness": "historical_only",
            "publication_eligible": False,
            "public_write": False,
        }

    def settings(self) -> dict:
        report, _ = local_diagnostics(self.config)
        return report
