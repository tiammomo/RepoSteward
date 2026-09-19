"""Reviewable project onboarding with durable inspection, intent and reconciliation."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from reposteward.github.client import GitHubReadError
from reposteward.projects.clone import clone_ssh, directory_identity, publish_directory
from reposteward.projects.identity import PURPOSES
from reposteward.projects.registry import (
    ProjectError,
    local_git,
    normalize_remote,
    workspace_metadata,
    workspace_state,
)
from reposteward.storage.local_queue import digest, encoded, enqueue, hex_id
from reposteward.storage.snapshots import workspace_snapshot
from reposteward.storage.store import utc_now


def source_input(kind: str, value: str, host: str) -> dict:
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ProjectError("invalid project source")
    if kind == "local_path":
        path = Path(value)
        if not path.is_absolute():
            raise ProjectError("select an absolute local directory")
        return {"kind": kind, "value": str(path)}
    if kind != "github_url":
        raise ProjectError("unsupported project source")
    if "://" in value:
        parsed = urlsplit(value)
        if (
            parsed.password
            or (
                parsed.username
                and not (parsed.scheme == "ssh" and parsed.username == "git")
            )
            or parsed.scheme not in {"https", "ssh"}
        ):
            raise ProjectError("use a GitHub URL without credentials")
    elif not value.startswith("git@"):
        raise ProjectError("use a GitHub HTTPS or SSH URL")
    remote = normalize_remote(value)
    if remote["host"] != host:
        raise ProjectError("project host differs from the configured GitHub account")
    return {"kind": kind, "value": "https://" + remote["identity"]}


def local_snapshot(path: str) -> dict:
    meta = workspace_metadata(Path(path))
    # Never store the unnormalized remote, which may contain user credentials.
    return {
        **meta,
        "state": workspace_state(Path(meta["root"])),
        "content_digest": workspace_snapshot(Path(meta["root"]))["digest"],
    }


def remote_facts(client, host: str, repository: str, guard) -> dict:
    path = "/repos/" + repository
    for hop in range(3):
        guard()
        try:
            raw = client.conditional_get(path).body
        except GitHubReadError as exc:
            guard()
            if exc.code != "repository_moved" or not exc.redirect_path or hop == 2:
                raise
            path = exc.redirect_path
        else:
            break
    guard()
    if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0:
        raise GitHubReadError("invalid_response")
    canonical = normalize_remote(
        "https://" + host + "/" + str(raw.get("full_name", ""))
    )
    parent = raw.get("parent") or {}
    upstream = None
    if raw.get("fork") and parent:
        upstream_meta = normalize_remote(
            "https://" + host + "/" + str(parent.get("full_name", ""))
        )
        upstream = {
            "remote_id": str(parent.get("id", "")),
            "repository": upstream_meta["repository"],
        }
    return {
        **canonical,
        "remote_id": str(raw["id"]),
        "fork": bool(raw.get("fork")),
        "upstream": upstream,
        "aliases": sorted({host + "/" + repository, canonical["identity"]}),
        "private": bool(raw.get("private")),
        "default_branch": str(raw.get("default_branch", ""))[:255],
    }


class ProjectImports:
    def __init__(self, operations):
        self.operations = operations
        self.registry = operations.workbench.registry
        self.account = operations.account
        api_host = urlsplit(operations.config.github.api_url).hostname
        self.host = "github.com" if api_host == "api.github.com" else api_host

    def draft(self, import_id: str) -> dict:
        if not hex_id(import_id):
            raise ValueError("invalid import id")
        store = self.operations.store()
        if store is None:
            raise KeyError("project import not found")
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM project_imports WHERE id=? AND account_digest=?",
                (import_id, self.account),
            ).fetchone()
        if row is None:
            raise KeyError("project import not found")
        source = json.loads(row["source"])
        if (
            digest(source) != row["source_digest"]
            or source_input(source["kind"], source["value"], self.host) != source
        ):
            raise ProjectError("import source changed")
        return {"id": import_id, "source": source, "created_at": row["created_at"]}

    def inspect(self, kind: str, value: str, key: str) -> dict:
        source = source_input(kind, value, self.host)
        # Explicit commands check both schemas before creating task state.
        self.registry.revision()
        store = self.operations.store(write=True)
        import_id = uuid.uuid5(uuid.NAMESPACE_URL, self.account + ":import:" + key).hex
        with store.atomic(), store._connection() as db:
            old = db.execute(
                "SELECT source_digest FROM project_imports WHERE id=?", (import_id,)
            ).fetchone()
            if old and old[0] != digest(source):
                raise ProjectError("idempotency_conflict")
            db.execute(
                "INSERT OR IGNORE INTO project_imports VALUES (?,?,?,?,?)",
                (import_id, self.account, encoded(source), digest(source), utc_now()),
            )
            task = enqueue(
                store,
                account=self.account,
                project=import_id,
                repository="",
                action="project.inspect",
                payload={"source": source},
                idempotency_key=key,
                actor=self.operations.config.github.login,
            )
        self.operations.wake.set()
        return self.operations.operation(task["id"])

    def step(self, store, plan_id: str, stage: str) -> dict | None:
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM project_import_steps WHERE plan_id=? AND stage=?",
                (plan_id, stage),
            ).fetchone()
        if row is None:
            return None
        body = json.loads(row["body"])
        if digest(body) != row["body_digest"]:
            raise ProjectError("import step changed")
        return body

    def record(self, store, plan_id: str, stage: str, body: dict, guard):
        with store.atomic(), store._connection() as db:
            guard()
            old = self.step(store, plan_id, stage)
            if old is not None and old != body:
                raise ProjectError("import stage conflicts with its durable result")
            db.execute(
                "INSERT OR IGNORE INTO project_import_steps VALUES (?,?,?,?,?)",
                (plan_id, stage, encoded(body), digest(body), utc_now()),
            )

    def show(self, import_id: str) -> dict:
        draft = self.draft(import_id)
        store = self.operations.store()
        with store._connection() as db:
            inspections = db.execute(
                "SELECT p.id FROM local_operation_plans p JOIN project_import_steps s ON s.plan_id=p.id AND s.stage='inspected' WHERE p.scope_key=? AND p.account_digest=? ORDER BY p.created_at DESC LIMIT 1",
                (import_id, self.account),
            ).fetchall()
            plans = db.execute(
                "SELECT id,body,body_digest FROM project_import_previews WHERE import_id=? AND account_digest=? ORDER BY created_at DESC LIMIT 1",
                (import_id, self.account),
            ).fetchall()
            tasks = db.execute(
                "SELECT id FROM queue_tasks WHERE operation_family='local' AND account_digest=? AND scope_kind='import' AND scope_key=? ORDER BY sequence DESC LIMIT 20",
                (self.account, import_id),
            ).fetchall()
        inspection = (
            self.step(store, inspections[0]["id"], "inspected") if inspections else None
        )
        preview = json.loads(plans[0]["body"]) if plans else None
        if preview and digest(preview) != plans[0]["body_digest"]:
            raise ProjectError("import plan changed")
        return {
            **draft,
            "inspection": inspection,
            "inspection_id": inspections[0]["id"] if inspections else "",
            "preview": preview,
            "preview_id": plans[0]["id"] if plans else "",
            "preview_digest": plans[0]["body_digest"] if plans else "",
            "operations": [self.operations.operation(row["id"]) for row in tasks],
            "public_write": False,
        }

    def plan(
        self,
        import_id: str,
        inspection_id: str,
        method: str,
        purpose: str,
        target: str,
        key: str,
    ) -> dict:
        request = digest(
            {
                "import_id": import_id,
                "inspection_id": inspection_id,
                "method": method,
                "purpose": purpose,
                "target": target,
            }
        )
        store = self.operations.store(write=True)
        with store._connection() as db:
            old = db.execute(
                "SELECT * FROM project_import_requests WHERE account_digest=? AND action='plan' AND key_digest=?",
                (self.account, digest(key)),
            ).fetchone()
            if old:
                if old["request_digest"] != request:
                    raise ProjectError("idempotency_conflict")
                saved = db.execute(
                    "SELECT * FROM project_import_previews WHERE id=?",
                    (old["resource_id"],),
                ).fetchone()
                body = json.loads(saved["body"])
                if digest(body) != saved["body_digest"]:
                    raise ProjectError("import plan changed")
                return {
                    "id": saved["id"],
                    "digest": saved["body_digest"],
                    "plan": body,
                    "public_write": False,
                }
        current = self.show(import_id)
        if current["inspection_id"] != inspection_id or not current["inspection"]:
            raise ProjectError("inspection changed; inspect the latest result")
        if method not in {"link", "clone", "watch"} or purpose not in PURPOSES:
            raise ProjectError("invalid import method or purpose")
        inspected = current["inspection"]
        local, parent = None, None
        if method == "link":
            if not target:
                target = (inspected.get("workspace") or {}).get("root", "")
            if not Path(target).is_absolute():
                raise ProjectError("link requires an absolute workspace path")
            local = local_snapshot(target)
            if local["identity"] not in inspected["remote"]["aliases"]:
                raise ProjectError(
                    "local origin does not match the identified repository"
                )
            target = local["root"]
        elif method == "clone":
            path = Path(target)
            if (
                not path.is_absolute()
                or path.name in {"", ".", ".."}
                or any(ord(c) < 32 for c in target)
            ):
                raise ProjectError("clone requires a new absolute directory")
            path = path.parent.resolve(strict=True) / path.name
            if path.exists() or path.is_symlink():
                raise ProjectError("clone target already exists")
            parent = directory_identity(path.parent)
            target = str(path)
        elif target:
            raise ProjectError("remote-only import does not use a local path")
        plan = {
            "import_id": import_id,
            "inspection_id": inspection_id,
            "remote": inspected["remote"],
            "method": method,
            "purpose": purpose,
            "target": target,
            "workspace": local,
            "parent": parent,
            "registry_revision": self.registry.revision(),
            "account_digest": self.account,
        }
        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, digest(plan)).hex
        store = self.operations.store(write=True)
        with store.atomic(), store._connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO project_import_requests VALUES (?,'plan',?,?,?)",
                (self.account, digest(key), request, plan_id),
            )
            concurrent = db.execute(
                "SELECT * FROM project_import_requests WHERE account_digest=? AND action='plan' AND key_digest=?",
                (self.account, digest(key)),
            ).fetchone()
            if concurrent["request_digest"] != request:
                raise ProjectError("idempotency_conflict")
            if concurrent["resource_id"] != plan_id:
                saved = db.execute(
                    "SELECT * FROM project_import_previews WHERE id=?",
                    (concurrent["resource_id"],),
                ).fetchone()
                plan = json.loads(saved["body"])
                if digest(plan) != saved["body_digest"]:
                    raise ProjectError("import plan changed")
                plan_id = saved["id"]
            db.execute(
                "INSERT OR IGNORE INTO project_import_previews VALUES (?,?,?,?,?,?)",
                (
                    plan_id,
                    import_id,
                    self.account,
                    encoded(plan),
                    digest(plan),
                    utc_now(),
                ),
            )
        return {
            "id": plan_id,
            "digest": digest(plan),
            "plan": plan,
            "public_write": False,
        }

    def apply(
        self, import_id: str, preview_id: str, expected_digest: str, key: str
    ) -> dict:
        self.draft(import_id)
        store = self.operations.store(write=True)
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM project_import_previews WHERE id=? AND import_id=? AND account_digest=?",
                (preview_id, import_id, self.account),
            ).fetchone()
        if row is None:
            raise KeyError("import plan not found")
        plan = json.loads(row["body"])
        if digest(plan) != row["body_digest"] or row["body_digest"] != expected_digest:
            raise ProjectError("import plan changed")
        request = digest(
            {
                "import_id": import_id,
                "preview_id": preview_id,
                "expected_digest": expected_digest,
            }
        )
        with store.atomic(), store._connection() as db:
            old = db.execute(
                "SELECT * FROM project_import_requests WHERE account_digest=? AND action='apply' AND key_digest=?",
                (self.account, digest(key)),
            ).fetchone()
            if old and old["request_digest"] != request:
                raise ProjectError("idempotency_conflict")
            task = enqueue(
                store,
                account=self.account,
                project=import_id,
                repository=plan["remote"]["repository"],
                action="project.apply",
                payload=plan,
                idempotency_key="apply:" + preview_id,
                actor=self.operations.config.github.login,
            )
            db.execute(
                "INSERT OR IGNORE INTO project_import_requests VALUES (?,'apply',?,?,?)",
                (self.account, digest(key), request, task["id"]),
            )
        self.operations.wake.set()
        return self.operations.operation(task["id"])

    def execute(self, store, task: dict, plan: dict, client, guard) -> dict:
        draft = self.draft(task["scope_key"])
        guard()
        user = client.conditional_get("/user").body
        guard()
        if (
            not isinstance(user, dict)
            or str(user.get("login", "")).casefold()
            != self.operations.config.github.login.casefold()
        ):
            raise GitHubReadError("account_mismatch")
        payload, plan_id = plan["payload"], plan["id"]
        if task["action"] == "project.inspect":
            if payload != {"source": draft["source"]}:
                raise ProjectError("inspection source changed")
            previous = self.step(store, plan_id, "inspected")
            if previous:
                return {"status": "completed", "import_id": draft["id"], **previous}
            source = draft["source"]
            local = (
                local_snapshot(source["value"])
                if source["kind"] == "local_path"
                else None
            )
            remote = local or normalize_remote(source["value"])
            if remote["host"] != self.host:
                raise ProjectError("local origin host differs from the current account")
            facts = remote_facts(client, self.host, remote["repository"], guard)
            if local and local_snapshot(local["root"]) != local:
                raise ProjectError("workspace changed during inspection")
            result = {"remote": facts, "workspace": local, "observed_at": utc_now()}
            self.record(store, plan_id, "inspected", result, guard)
            return {"status": "completed", "import_id": draft["id"], **result}
        if (
            payload["account_digest"] != self.account
            or payload["import_id"] != draft["id"]
        ):
            raise ProjectError("import plan scope changed")
        completed = self.step(store, plan_id, "completed")
        if completed:
            return {"status": "completed", **completed}
        if draft["source"]["kind"] == "local_path":
            inspected = self.step(store, payload["inspection_id"], "inspected")
            if not inspected or not inspected.get("workspace"):
                raise ProjectError("import source changed")
            expected_source = {
                key: value
                for key, value in inspected["workspace"].items()
                if key not in {"state", "content_digest"}
            }
            if workspace_metadata(Path(draft["source"]["value"])) != expected_source:
                raise ProjectError("workspace identity changed at the original source")
        facts = remote_facts(client, self.host, payload["remote"]["repository"], guard)
        if any(
            facts[k] != payload["remote"][k]
            for k in ("host", "remote_id", "identity", "fork", "upstream", "private")
        ):
            raise ProjectError(
                "remote identity changed; create a new inspection and plan"
            )
        intent = self.step(store, plan_id, "authorized")
        if not intent:
            if self.registry.revision() != payload["registry_revision"]:
                raise ProjectError("registry changed; create a new plan")
            self.record(
                store, plan_id, "authorized", {"payload_digest": digest(payload)}, guard
            )
        elif intent != {"payload_digest": digest(payload)}:
            raise ProjectError("import authorization changed")
        target = payload["target"]
        if payload["method"] == "clone":
            target = self.clone(store, plan_id, payload, guard)
        elif (
            payload["method"] == "link"
            and local_snapshot(target) != payload["workspace"]
        ):
            raise ProjectError("workspace changed; create a new plan")
        expected_metadata = None
        if payload["method"] != "watch":
            expected_metadata = workspace_metadata(Path(target))
            if payload["method"] == "link" and expected_metadata != {
                key: value
                for key, value in payload["workspace"].items()
                if key not in {"state", "content_digest"}
            }:
                raise ProjectError("workspace identity changed before registration")
            if expected_metadata["identity"] not in payload["remote"]["aliases"]:
                raise ProjectError("workspace identity changed before registration")
        guard()
        project = self.registry.import_remote(
            payload["remote"],
            purpose=payload["purpose"],
            plan_id=plan_id,
            payload_digest=digest(payload),
            expected_revision=payload["registry_revision"],
        )
        self.record(store, plan_id, "registered", {"project_id": project["id"]}, guard)
        binding_id = ""
        if payload["method"] != "watch":
            guard()
            linked = self.registry.link(
                Path(target), expected_metadata=expected_metadata
            )
            if linked["project"]["id"] != project["id"]:
                raise ProjectError("workspace linked to a different project")
            binding_id = linked["binding"]["id"]
        result = {
            "project_id": project["id"],
            "binding_id": binding_id,
            "method": payload["method"],
            "target": target,
            "import_id": draft["id"],
        }
        self.record(store, plan_id, "completed", result, guard)
        return {"status": "completed", **result}

    def clone(self, store, plan_id: str, payload: dict, guard) -> str:
        target = Path(payload["target"])
        if directory_identity(target.parent) != payload["parent"]:
            raise ProjectError("clone parent changed")
        published = self.step(store, plan_id, "published")
        prepared = self.step(store, plan_id, "clone_prepared")
        if published or (prepared and target.exists()):
            expected = published or prepared
            if (
                directory_identity(target) != expected["directory"]
                or local_git(target, "rev-parse", "HEAD") != expected["head"]
                or workspace_metadata(target)["identity"]
                != payload["remote"]["identity"]
                or workspace_snapshot(target)["digest"] != expected["snapshot_digest"]
            ):
                raise ProjectError("clone destination changed; inspect retained files")
            self.record(store, plan_id, "published", expected, guard)
            return str(target)
        if target.exists() or target.is_symlink():
            raise ProjectError("clone target already exists")
        staging = target.parent / (".reposteward-import-" + plan_id)
        owned = self.step(store, plan_id, "staging")
        if not owned:
            guard()
            staging.mkdir(mode=0o700)
            owned = directory_identity(staging)
            self.record(store, plan_id, "staging", owned, guard)
        if directory_identity(staging) != owned:
            raise ProjectError("clone staging changed")
        checkout = staging / "checkout"
        if not prepared:
            # A failed clone may leave partial files. Keep them and use a fresh
            # attempt path; never delete or resume unverified repository contents.
            checkout = staging / ("checkout-" + uuid.uuid4().hex)
            clone_ssh(
                payload["remote"]["host"],
                payload["remote"]["repository"],
                checkout,
                guard,
            )
            meta = workspace_metadata(checkout)
            if (
                meta["identity"] != payload["remote"]["identity"]
                or workspace_state(checkout)["dirty"]
            ):
                raise ProjectError("clone metadata differs from the plan")
            prepared = {
                "directory": directory_identity(checkout),
                "head": local_git(checkout, "rev-parse", "HEAD"),
                "staging_name": checkout.name,
                "snapshot_digest": workspace_snapshot(checkout)["digest"],
            }
            self.record(store, plan_id, "clone_prepared", prepared, guard)
        checkout = staging / prepared["staging_name"]
        if (
            directory_identity(checkout) != prepared["directory"]
            or workspace_snapshot(checkout)["digest"] != prepared["snapshot_digest"]
            or workspace_metadata(checkout)["identity"] != payload["remote"]["identity"]
        ):
            raise ProjectError("clone staging changed before publication")
        guard()
        publish_directory(checkout, target, payload["parent"])
        self.record(store, plan_id, "published", prepared, guard)
        return str(target)


def import_problem(exc: Exception) -> tuple[str, str]:
    message = str(exc).casefold()
    for words, code, text in (
        (
            ("idempotency",),
            "idempotency_conflict",
            "此请求编号已用于不同的选择，请重新提交。",
        ),
        (
            ("schema",),
            "migration_required",
            "本地登记库需要升级。请停止工作台，执行 state plan 并按摘要升级；升级会先备份。",
        ),
        (
            (
                "credentials",
                "remote identity",
                "unsupported url",
                "invalid project source",
            ),
            "invalid_source",
            "请使用不含账号密码、令牌或查询参数的 GitHub HTTPS / SSH 仓库地址。",
        ),
        (
            ("host differs", "host mismatch"),
            "host_mismatch",
            "仓库主机与当前 GitHub 账号配置不一致。",
        ),
        (
            ("already exists", "file exists"),
            "directory_conflict",
            "目标或暂存目录已经存在。已有文件已保留；请选择新的克隆目录，或关联已完成的仓库目录。",
        ),
        (
            ("ssh clone",),
            "ssh_unavailable",
            "SSH 克隆未完成，暂存文件已保留。请确认 SSH agent、主机信任和仓库访问后重试。",
        ),
        (
            ("atomic clone",),
            "clone_publication_failed",
            "当前系统无法安全发布克隆目录。暂存文件已保留，可核对后通过关联已有目录导入。",
        ),
        (
            (
                "workspace changed",
                "workspace identity",
                "clone destination changed",
                "staging changed",
                "parent changed",
            ),
            "workspace_changed",
            "目录身份或代码内容已变化。已有文件已保留，请重新识别并生成计划。",
        ),
        (
            (
                "registry changed",
                "different projects",
                "different remote",
                "aliases belong",
            ),
            "registry_changed",
            "项目登记或仓库身份已变化。请核对现有项目，重新识别并生成计划。",
        ),
        (
            ("origin", "local git", "absolute", "directory", "no such file"),
            "workspace_unavailable",
            "请核对绝对目录、Git 工作区和 origin，确认其对应已识别的仓库。",
        ),
        (
            ("limit",),
            "source_limit",
            "工作区超出本次读取范围，请缩小选择或使用 CLI 核对。",
        ),
        (("changed",), "plan_changed", "导入依据已变化，请重新识别并生成计划。"),
    ):
        if any(word in message for word in words):
            return code, text
    return (
        "project_import_failed",
        "项目导入未完成。已成功的登记与文件保留，请查看步骤并核对来源、路径后重试。",
    )
