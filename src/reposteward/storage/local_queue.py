"""Local task envelopes share native queue leases while preserving v1 digests."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reposteward.storage.store import Store

LOCAL_ACTIONS = frozenset({"github.sync"})


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def hex_id(value: object, length: int = 32) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value)
    )


def material(value: dict) -> dict:
    return {
        key: value[key]
        for key in (
            "operation_family",
            "payload_version",
            "scope_kind",
            "scope_key",
            "account_digest",
            "plan_id",
            "repository",
            "action",
            "parameters_digest",
            "idempotency_digest",
        )
    }


def decode(value: dict) -> dict:
    """A changed family, version, scope or plan never falls back to native v1."""
    if (
        value["operation_family"] != "local"
        or value["payload_version"] != 2
        or value["action"] not in LOCAL_ACTIONS
        or value["scope_kind"] != "project"
        or not hex_id(value["scope_key"])
        or not hex_id(value["plan_id"])
        or not hex_id(value["account_digest"], 64)
        or not re.fullmatch(r"[^/\s]+/[^/\s]+", value["repository"])
        or any(
            value.get(key)
            for key in (
                "work_item_id",
                "run_id",
                "depends_on_task_id",
                "issue_number",
                "pull_number",
            )
        )
        or value["state"]
        not in {"pending", "running", "completed", "failed", "cancelled"}
    ):
        raise ValueError("invalid local queue envelope")
    parameters = json.loads(value["parameters"])
    if (
        parameters != {"plan_id": value["plan_id"]}
        or digest(parameters) != value["parameters_digest"]
    ):
        raise ValueError("local queue parameters changed")
    if digest(material(value)) != value["dedupe_key"]:
        raise ValueError("local queue identity changed")
    return {
        **value,
        "parameters": parameters,
        "manual_required": bool(value["manual_required"]),
    }


def plan_for(db, task: dict) -> dict:
    """Resolve the complete plan and bind it to the validated queue identity."""
    row = db.execute(
        "SELECT * FROM local_operation_plans WHERE id=?", (task["plan_id"],)
    ).fetchone()
    if row is None:
        raise ValueError("local operation plan missing")
    plan = dict(row)
    expected = plan.pop("plan_digest")
    plan["payload"] = json.loads(plan["payload"])
    if (
        not isinstance(plan["payload"], dict)
        or plan["payload_version"] != 1
        or digest(plan) != expected
        or any(
            plan[key] != task[key]
            for key in (
                "action",
                "account_digest",
                "scope_kind",
                "scope_key",
                "repository",
            )
        )
    ):
        raise ValueError("local operation plan changed")
    request = {
        "account": plan["account_digest"],
        "project": plan["scope_key"],
        "repository": plan["repository"],
        "action": plan["action"],
        "payload": plan["payload"],
    }
    if digest(request) != plan["request_digest"]:
        raise ValueError("local request identity changed")
    return {**plan, "plan_digest": expected}


def enqueue(
    store: Store,
    *,
    account: str,
    project: str,
    repository: str,
    action: str,
    payload: dict[str, Any],
    idempotency_key: str,
    actor: str,
) -> dict:
    """Persist an immutable plan and queue task in the same transaction."""
    if not hex_id(account, 64) or not hex_id(project) or action not in LOCAL_ACTIONS:
        raise ValueError("invalid local operation scope")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise ValueError("invalid idempotency key")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ValueError("invalid repository identity")
    worker = store._queue_worker(actor)
    request = {
        "account": account,
        "project": project,
        "repository": repository,
        "action": action,
        "payload": payload,
    }
    request_digest = digest(request)
    key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    scope = (account, action, "project", project, key_digest)
    with store.atomic(), store._connection() as db:
        previous = db.execute(
            """SELECT * FROM local_operation_requests WHERE account_digest=? AND action=?
            AND scope_kind=? AND scope_key=? AND key_digest=?""",
            scope,
        ).fetchone()
        if previous:
            if previous["request_digest"] != request_digest:
                raise ValueError("idempotency_conflict")
            row = db.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (previous["task_id"],)
            ).fetchone()
            if row is None:
                raise ValueError("local operation reference missing")
            result = store._queue_task(row)
            plan = plan_for(db, result)
            if plan["request_digest"] != request_digest:
                raise ValueError("local operation request mapping changed")
            return {**result, "idempotent": True}
        row = db.execute(
            """SELECT q.* FROM queue_tasks q JOIN local_operation_plans p ON p.id=q.plan_id
            WHERE q.operation_family='local' AND q.account_digest=? AND q.action=?
            AND q.scope_kind='project' AND q.scope_key=? AND p.request_digest=?
            AND q.state IN ('pending','running') ORDER BY q.sequence LIMIT 1""",
            (account, action, project, request_digest),
        ).fetchone()
        coalesced = row is not None
        if not coalesced:
            _, now = store._queue_timestamp()
            plan_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
            plan = {
                "id": plan_id,
                "action": action,
                "account_digest": account,
                "scope_kind": "project",
                "scope_key": project,
                "repository": repository,
                "payload_version": 1,
                "payload": payload,
                "request_digest": request_digest,
                "created_at": now,
            }
            if len(encoded(payload).encode()) > 64_000:
                raise ValueError("operation plan exceeds size limit")
            db.execute(
                "INSERT INTO local_operation_plans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    action,
                    account,
                    "project",
                    project,
                    repository,
                    1,
                    encoded(payload),
                    request_digest,
                    digest(plan),
                    now,
                ),
            )
            parameters = {"plan_id": plan_id}
            identity = {
                "operation_family": "local",
                "payload_version": 2,
                "scope_kind": "project",
                "scope_key": project,
                "account_digest": account,
                "plan_id": plan_id,
                "repository": repository,
                "action": action,
                "parameters_digest": digest(parameters),
                "idempotency_digest": key_digest,
            }
            db.execute(
                """INSERT INTO queue_tasks(id,dedupe_key,repository,action,parameters,
                parameters_digest,idempotency_digest,state,max_attempts,available_at,created_at,updated_at,
                operation_family,payload_version,scope_kind,scope_key,account_digest,plan_id)
                VALUES (?,?,?,?,?,?,?,'pending',3,?,?,?,'local',2,'project',?,?,?)""",
                (
                    task_id,
                    digest(identity),
                    repository,
                    action,
                    encoded(parameters),
                    digest(parameters),
                    key_digest,
                    now,
                    now,
                    now,
                    project,
                    account,
                    plan_id,
                ),
            )
            store._append_queue_attempt(
                db,
                task_id=task_id,
                generation=0,
                worker=worker,
                event="enqueued",
                outcome="pending",
                payload={},
                created_at=now,
            )
            row = db.execute(
                "SELECT * FROM queue_tasks WHERE id=?", (task_id,)
            ).fetchone()
        result = store._queue_task(row)
        if plan_for(db, result)["request_digest"] != request_digest:
            raise ValueError("local operation coalescing identity changed")
        db.execute(
            "INSERT INTO local_operation_requests VALUES (?,?,?,?,?,?,?)",
            (*scope, request_digest, result["id"]),
        )
        return {**result, "idempotent": coalesced}
