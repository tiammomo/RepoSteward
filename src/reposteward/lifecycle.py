from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .store import SCHEMA_VERSION, StoreError

LIFECYCLE_SCHEMA_VERSION = 1
DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 500
MAX_PULL_NUMBERS = 200
MAX_FIELD_CHARS = 300
MAX_STORED_JSON_CHARS = 2_000_000
MAX_TEXT_CHARS = 12_000
MAX_TEXT_EVENTS = 50
MAX_JSON_CHARS = 1_000_000

SOURCE_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "work_item",
            "run",
            "context_pack",
            "context_import",
            "harness",
            "checkpoint",
            "harness_usage",
            "verification",
            "lease",
            "queue_task",
            "queue_attempt",
            "submission",
            "publication",
            "github_watermark",
            "github_pr",
            "owner_review",
            "merge_decision",
            "merge_execution",
            "dependency",
        )
    )
}
SOURCE_TRUST = {
    **{name: "local_control_plane" for name in SOURCE_ORDER},
    "checkpoint": "derived_review_required",
    "context_import": "imported_untrusted",
    "github_pr": "github_untrusted",
    "owner_review": "operator_attested",
    "merge_decision": "local_deterministic",
    "dependency": "operator_attested",
}
CHECKPOINT_ACTIONS = frozenset(
    {
        "human_review",
        "diagnose_failure",
        "run_coding_harness",
        "verify_adopted_change",
        "run_repair_harness",
        "monitor_pull_request",
        "reverify_changed_head",
        "complete",
        "inspect_closed_pull_request",
        "diagnose_failed_checks",
        "review_new_activity",
        "wait_for_checks",
        "wait_for_activity",
        "review_suggestions",
    }
)


def _checkpoint_action(value: object) -> str:
    return (
        value if isinstance(value, str) and value in CHECKPOINT_ACTIONS else "unknown"
    )


class LifecycleTraceError(StoreError):
    """Stored lifecycle facts cannot be read without guessing."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _read_object(value: object, *, source: str, record_id: str) -> dict[str, Any]:
    serialized = str(value)
    if len(serialized) > MAX_STORED_JSON_CHARS:
        raise LifecycleTraceError(
            f"{source} record {record_id} exceeds the lifecycle read boundary"
        )
    try:
        result = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleTraceError(
            f"{source} record {record_id} contains invalid JSON"
        ) from exc
    if not isinstance(result, dict):
        raise LifecycleTraceError(f"{source} record {record_id} is not a JSON object")
    return result


def _pull_number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    text = str(value or "")
    match = re.search(r"/pull/(\d+)(?:/|$)", text)
    return int(match.group(1)) if match else 0


def _next_action(runs: list[dict[str, Any]], work_item: dict[str, Any] | None) -> str:
    if not runs:
        return "prepare" if work_item is not None else "inspect_local_state"
    latest = max(
        runs,
        key=lambda row: (str(row["created_at"]), str(row["id"])),
    )
    status = str(latest["status"])
    stage = str(latest["stage"])
    if status == "ready":
        return "human_review"
    if status == "submitted":
        return "monitor_pull_request"
    if status == "failed":
        return "diagnose_failure"
    if status == "running":
        return f"wait_for_{stage or 'pipeline'}"
    return "inspect_run"


def _placeholders(values: list[object] | tuple[object, ...]) -> str:
    return ",".join("?" for _ in values)


def _bounded_rows(
    connection: sqlite3.Connection,
    *,
    select: str,
    count: str,
    parameters: tuple[object, ...],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    total_row = connection.execute(count, parameters).fetchone()
    assert total_row is not None
    total = int(total_row[0])
    rows = connection.execute(f"{select} LIMIT ?", (*parameters, limit)).fetchall()
    return [dict(row) for row in rows], total


def _database_path(state_dir: Path) -> Path:
    current = state_dir / "reposteward.sqlite3"
    legacy = state_dir / "starfix.sqlite3"
    if legacy.exists() and not current.exists():
        return legacy
    return current


def build_lifecycle_trace(
    state_dir: Path,
    repository: str,
    issue_number: int,
    *,
    event_limit: int = DEFAULT_EVENT_LIMIT,
) -> dict[str, Any]:
    """Build one deterministic lifecycle trace from a read-only Store snapshot."""
    normalized_repository = repository.casefold().strip()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", normalized_repository):
        raise ValueError("repository must use owner/name")
    if issue_number < 1:
        raise ValueError("issue_number must be positive")
    if event_limit < 1 or event_limit > MAX_EVENT_LIMIT:
        raise ValueError(f"event_limit must be between 1 and {MAX_EVENT_LIMIT}")

    database = _database_path(state_dir)
    if not database.is_file():
        raise KeyError(
            f"no local lifecycle facts for {normalized_repository}#{issue_number}"
        )
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")

    try:
        connection.execute("BEGIN")
        database_schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if database_schema_version != SCHEMA_VERSION:
            raise LifecycleTraceError(
                "lifecycle trace requires the current Store schema; run another "
                "RepoSteward command to migrate it first"
            )

        work_item_row = connection.execute(
            """
            SELECT id, repository, kind, external_id, status, created_at, updated_at
            FROM work_items
            WHERE repository=? AND kind='github_issue' AND external_id=?
            """,
            (normalized_repository, str(issue_number)),
        ).fetchone()
        work_item = dict(work_item_row) if work_item_row is not None else None
        work_item_id = str(work_item["id"]) if work_item is not None else ""

        runs, run_total = _bounded_rows(
            connection,
            select="""
                SELECT id, repository, issue_number, stage, status, details,
                       created_at, updated_at
                FROM runs WHERE repository=? AND issue_number=?
                ORDER BY created_at, rowid
            """,
            count="SELECT COUNT(*) FROM runs WHERE repository=? AND issue_number=?",
            parameters=(normalized_repository, issue_number),
            limit=event_limit,
        )
        latest_run_row = connection.execute(
            """
            SELECT id, stage, status, details, created_at
            FROM runs WHERE repository=? AND issue_number=?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (normalized_repository, issue_number),
        ).fetchone()
        latest_runs = [dict(latest_run_row)] if latest_run_row is not None else []
        current_details: dict[str, Any] = {}
        current_checkpoint: dict[str, Any] | None = None
        current_context: dict[str, Any] | None = None
        current_merge: dict[str, Any] | None = None
        if latest_runs:
            current_run_id = str(latest_runs[0]["id"])
            current_details = _read_object(
                latest_runs[0]["details"], source="run", record_id=current_run_id
            )
            for table, columns, order, label in (
                ("checkpoints", "id, payload", "sequence DESC", "checkpoint"),
                (
                    "context_packs",
                    "id, base_commit, payload",
                    "created_at DESC, rowid DESC",
                    "context",
                ),
                (
                    "merge_executions",
                    "id, stage, outcome, head_sha",
                    "created_at DESC, rowid DESC",
                    "merge",
                ),
            ):
                row = connection.execute(
                    f"SELECT {columns} FROM {table} WHERE run_id=? ORDER BY {order} LIMIT 1",
                    (current_run_id,),
                ).fetchone()
                if row is None:
                    continue
                material = dict(row)
                if "payload" in material:
                    material["payload"] = _read_object(
                        material["payload"], source=table, record_id=str(material["id"])
                    )
                if label == "checkpoint":
                    current_checkpoint = material
                elif label == "context":
                    current_context = material
                else:
                    current_merge = material
        if work_item is None and run_total == 0:
            raise KeyError(
                f"no local lifecycle facts for {normalized_repository}#{issue_number}"
            )
        run_ids = [str(row["id"]) for row in runs]
        run_details = {
            str(row["id"]): _read_object(
                row["details"], source="run", record_id=str(row["id"])
            )
            for row in runs
        }

        contexts: list[dict[str, Any]] = []
        context_total = 0
        harnesses: list[dict[str, Any]] = []
        harness_total = 0
        checkpoints: list[dict[str, Any]] = []
        checkpoint_total = 0
        imports: list[dict[str, Any]] = []
        import_total = 0
        usage_rows: list[dict[str, Any]] = []
        usage_total = 0
        context_run_total = 0
        checkpoint_run_total = 0
        usage_run_total = 0
        if work_item_id:
            contexts, context_total = _bounded_rows(
                connection,
                select="""
                    SELECT id, work_item_id, run_id, schema_version, source_digest,
                           base_commit, payload, created_at
                    FROM context_packs WHERE work_item_id=?
                    ORDER BY created_at, id
                """,
                count="SELECT COUNT(*) FROM context_packs WHERE work_item_id=?",
                parameters=(work_item_id,),
                limit=event_limit,
            )
            harnesses, harness_total = _bounded_rows(
                connection,
                select="""
                    SELECT run_id, work_item_id, context_pack_id, harness, model,
                           created_at
                    FROM harness_runs WHERE work_item_id=?
                    ORDER BY created_at, run_id
                """,
                count="SELECT COUNT(*) FROM harness_runs WHERE work_item_id=?",
                parameters=(work_item_id,),
                limit=event_limit,
            )
            checkpoints, checkpoint_total = _bounded_rows(
                connection,
                select="""
                    SELECT id, work_item_id, run_id, context_pack_id, sequence,
                           status, payload, created_at
                    FROM checkpoints WHERE work_item_id=?
                    ORDER BY created_at, run_id, sequence, id
                """,
                count="SELECT COUNT(*) FROM checkpoints WHERE work_item_id=?",
                parameters=(work_item_id,),
                limit=event_limit,
            )
            imports, import_total = _bounded_rows(
                connection,
                select="""
                    SELECT id, bundle_digest, work_item_id, source_run_id, imported_at
                    FROM context_imports WHERE work_item_id=?
                    ORDER BY imported_at, id
                """,
                count="SELECT COUNT(*) FROM context_imports WHERE work_item_id=?",
                parameters=(work_item_id,),
                limit=event_limit,
            )
            usage_rows, usage_total = _bounded_rows(
                connection,
                select="""
                    SELECT sequence, id, run_id, work_item_id, run_stage, harness,
                           event_digest, payload, created_at
                    FROM harness_usage_events WHERE work_item_id=?
                    ORDER BY sequence, id
                """,
                count=(
                    "SELECT COUNT(*) FROM harness_usage_events WHERE work_item_id=?"
                ),
                parameters=(work_item_id,),
                limit=event_limit,
            )
            relationship_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(DISTINCT run_id) FROM context_packs
                     WHERE work_item_id=?) AS context_runs,
                    (SELECT COUNT(DISTINCT run_id) FROM checkpoints
                     WHERE work_item_id=?) AS checkpoint_runs,
                    (SELECT COUNT(DISTINCT run_id) FROM harness_usage_events
                     WHERE work_item_id=?) AS usage_runs
                """,
                (work_item_id, work_item_id, work_item_id),
            ).fetchone()
            assert relationship_counts is not None
            context_run_total = int(relationship_counts["context_runs"])
            checkpoint_run_total = int(relationship_counts["checkpoint_runs"])
            usage_run_total = int(relationship_counts["usage_runs"])

        verification_total = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM runs
                WHERE repository=? AND issue_number=?
                  AND json_type(details, '$.verification')='object'
                """,
                (normalized_repository, issue_number),
            ).fetchone()[0]
        )

        context_payloads = {
            str(row["id"]): _read_object(
                row["payload"], source="context_pack", record_id=str(row["id"])
            )
            for row in contexts
        }
        checkpoint_payloads = {
            str(row["id"]): _read_object(
                row["payload"], source="checkpoint", record_id=str(row["id"])
            )
            for row in checkpoints
        }

        scope = f"issue:{normalized_repository}#{issue_number}"
        leases, lease_total = _bounded_rows(
            connection,
            select="""
                SELECT sequence, scope, owner, generation, action, expires_at,
                       created_at
                FROM run_lease_events WHERE scope=?
                ORDER BY sequence
            """,
            count="SELECT COUNT(*) FROM run_lease_events WHERE scope=?",
            parameters=(scope,),
            limit=event_limit,
        )
        publications, publication_total = _bounded_rows(
            connection,
            select="""
                SELECT sequence, id, attempt_id, step_id, run_id, actor, action,
                       stage, outcome, destination, branch, head_sha, base_branch,
                       expected_remote_sha, target_pull_number, created_at
                FROM publication_attempts
                WHERE repository=? AND issue_number=?
                ORDER BY sequence, id
            """,
            count="""
                SELECT COUNT(*) FROM publication_attempts
                WHERE repository=? AND issue_number=?
            """,
            parameters=(normalized_repository, issue_number),
            limit=event_limit,
        )
        submissions, submission_total = _bounded_rows(
            connection,
            select="""
                SELECT repository, issue_number, pr_url, created_at
                FROM submissions WHERE repository=? AND issue_number=?
                ORDER BY created_at, pr_url
            """,
            count="""
                SELECT COUNT(*) FROM submissions
                WHERE repository=? AND issue_number=?
            """,
            parameters=(normalized_repository, issue_number),
            limit=event_limit,
        )

        run_scope = "SELECT id FROM runs WHERE repository=? AND issue_number=?"
        queue_predicates = ["t.issue_number=?", f"t.run_id IN ({run_scope})"]
        queue_parameters: tuple[object, ...] = (
            normalized_repository,
            issue_number,
            normalized_repository,
            issue_number,
        )
        if work_item_id:
            queue_predicates.append("t.work_item_id=?")
            queue_parameters = (*queue_parameters, work_item_id)
        queue_where = "t.repository=? AND (" + " OR ".join(queue_predicates) + ")"
        queue_tasks, queue_task_total = _bounded_rows(
            connection,
            select=f"""
                SELECT sequence, id, action, work_item_id, run_id, issue_number,
                       pull_number, parameters_digest, idempotency_digest, priority,
                       state, max_attempts, attempt_count, manual_required,
                       last_error_code, created_at, updated_at
                FROM queue_tasks t WHERE {queue_where}
                ORDER BY sequence, id
            """,
            count=f"SELECT COUNT(*) FROM queue_tasks t WHERE {queue_where}",
            parameters=queue_parameters,
            limit=event_limit,
        )
        queue_attempts, queue_attempt_total = _bounded_rows(
            connection,
            select=f"""
                SELECT a.sequence, a.id, a.task_id, a.generation, a.worker,
                       a.event, a.outcome, a.payload_digest, a.created_at
                FROM queue_attempts a
                JOIN queue_tasks t ON t.id=a.task_id
                WHERE {queue_where}
                ORDER BY a.sequence, a.id
            """,
            count=f"""
                SELECT COUNT(*) FROM queue_attempts a
                JOIN queue_tasks t ON t.id=a.task_id
                WHERE {queue_where}
            """,
            parameters=queue_parameters,
            limit=event_limit,
        )

        pull_numbers: set[int] = set()
        for row in submissions:
            number = _pull_number(row["pr_url"])
            if number:
                pull_numbers.add(number)
        for row in publications:
            number = _pull_number(row["target_pull_number"])
            if number:
                pull_numbers.add(number)
        for row in queue_tasks:
            number = _pull_number(row["pull_number"])
            if number:
                pull_numbers.add(number)
        for details in run_details.values():
            number = _pull_number(details.get("pr_url"))
            guard = details.get("repair_guard")
            if not number and isinstance(guard, dict):
                number = _pull_number(guard.get("pull_number"))
            if number:
                pull_numbers.add(number)
        watermarks: list[dict[str, Any]] = []
        watermark_total = 0
        if run_total:
            parameters = (normalized_repository, issue_number)
            watermarks, watermark_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT run_id, pull_number, sequence, batch_digest, updated_at
                    FROM github_pr_watermarks WHERE run_id IN ({run_scope})
                    ORDER BY updated_at, run_id
                """,
                count=f"""
                    SELECT COUNT(*) FROM github_pr_watermarks
                    WHERE run_id IN ({run_scope})
                """,
                parameters=parameters,
                limit=event_limit,
            )
            pull_numbers.update(
                int(row["pull_number"])
                for row in watermarks
                if int(row["pull_number"]) > 0
            )

        all_pull_numbers = sorted(pull_numbers)
        selected_pull_numbers = all_pull_numbers[:MAX_PULL_NUMBERS]
        pull_numbers_omitted = len(all_pull_numbers) - len(selected_pull_numbers)

        github_events: list[dict[str, Any]] = []
        github_event_total = 0
        owner_reviews: list[dict[str, Any]] = []
        owner_review_total = 0
        merge_decisions: list[dict[str, Any]] = []
        merge_decision_total = 0
        merge_executions: list[dict[str, Any]] = []
        merge_execution_total = 0
        dependencies: list[dict[str, Any]] = []
        dependency_total = 0
        if selected_pull_numbers:
            pulls = _placeholders(selected_pull_numbers)
            pull_parameters = (normalized_repository, *selected_pull_numbers)
            github_events, github_event_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT sequence, pull_number, event_type, external_id,
                           version_digest, head_sha, source_trust, source_created_at,
                           source_updated_at, source_actor, source_state, ingested_at
                    FROM github_pr_events
                    WHERE repository=? AND pull_number IN ({pulls})
                    ORDER BY sequence
                """,
                count=f"""
                    SELECT COUNT(*) FROM github_pr_events
                    WHERE repository=? AND pull_number IN ({pulls})
                """,
                parameters=pull_parameters,
                limit=event_limit,
            )
            merge_decisions, merge_decision_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT id, pull_number, head_sha, base_sha, policy_digest,
                           snapshot_digest, eligible, decision_digest, created_at
                    FROM merge_decisions
                    WHERE repository=? AND pull_number IN ({pulls})
                    ORDER BY created_at, id
                """,
                count=f"""
                    SELECT COUNT(*) FROM merge_decisions
                    WHERE repository=? AND pull_number IN ({pulls})
                """,
                parameters=pull_parameters,
                limit=event_limit,
            )
            dependencies, dependency_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT sequence, id, pull_number, dependency_number, head_sha,
                           action, actor, source, event_digest, created_at
                    FROM portfolio_dependency_events
                    WHERE repository=? AND pull_number IN ({pulls})
                    ORDER BY sequence, id
                """,
                count=f"""
                    SELECT COUNT(*) FROM portfolio_dependency_events
                    WHERE repository=? AND pull_number IN ({pulls})
                """,
                parameters=pull_parameters,
                limit=event_limit,
            )

        relationship_clauses: list[str] = []
        relationship_parameters: list[object] = [normalized_repository]
        if run_ids:
            relationship_clauses.append(f"run_id IN ({_placeholders(run_ids)})")
            relationship_parameters.extend(run_ids)
        if selected_pull_numbers:
            relationship_clauses.append(
                f"pull_number IN ({_placeholders(selected_pull_numbers)})"
            )
            relationship_parameters.extend(selected_pull_numbers)
        if relationship_clauses:
            relationship_where = (
                "repository=? AND (" + " OR ".join(relationship_clauses) + ")"
            )
            parameters = tuple(relationship_parameters)
            merge_executions, merge_execution_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT id, attempt_id, run_id, decision_id, pull_number, actor,
                           merge_method, stage, outcome, reason, decision_digest,
                           head_sha, created_at
                    FROM merge_executions WHERE {relationship_where}
                    ORDER BY created_at, id
                """,
                count=f"SELECT COUNT(*) FROM merge_executions WHERE {relationship_where}",
                parameters=parameters,
                limit=event_limit,
            )
            owner_reviews, owner_review_total = _bounded_rows(
                connection,
                select=f"""
                    SELECT sequence, id, pull_number, run_id, actor, head_sha, base_sha,
                           policy_digest, review_facts_digest, attestation_digest,
                           created_at
                    FROM owner_review_attestations WHERE {relationship_where}
                    ORDER BY sequence, id
                """,
                count=f"""
                    SELECT COUNT(*) FROM owner_review_attestations
                    WHERE {relationship_where}
                """,
                parameters=parameters,
                limit=event_limit,
            )
    finally:
        connection.rollback()
        connection.close()

    clipped_fields = 0
    clipped_chars = 0

    def clip(value: object, limit: int = MAX_FIELD_CHARS) -> str:
        nonlocal clipped_fields, clipped_chars
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        clipped_fields += 1
        clipped_chars += len(text) - limit
        return text[:limit]

    def event(
        source: str,
        kind: str,
        occurred_at: object,
        stable_id: object,
        facts: dict[str, Any],
        *,
        sequence: int = 0,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "kind": kind,
            "occurred_at": clip(occurred_at, 100),
            "id": clip(stable_id, 200),
            "sequence": sequence,
            "facts": facts,
        }

    events: list[dict[str, Any]] = []
    if work_item is not None:
        events.append(
            event(
                "work_item",
                "work_item_created",
                work_item["created_at"],
                work_item["id"],
                {
                    "work_item_id": clip(work_item["id"], 200),
                    "kind": clip(work_item["kind"], 80),
                    "external_id": clip(work_item["external_id"], 100),
                    "status": clip(work_item["status"], 80),
                    "updated_at": clip(work_item["updated_at"], 100),
                },
            )
        )

    context_by_run: dict[str, dict[str, Any]] = {}
    for row in contexts:
        payload = context_payloads[str(row["id"])]
        sources = payload.get("sources")
        policy_digest = ""
        if isinstance(sources, list):
            for source in sources:
                if (
                    isinstance(source, dict)
                    and source.get("kind") == "repository_policy"
                ):
                    policy_digest = str(source.get("digest") or "")
                    break
        fact = {
            "run_id": clip(row["run_id"], 200),
            "context_pack_id": clip(row["id"], 200),
            "schema_version": int(row["schema_version"]),
            "source_digest": clip(row["source_digest"], 100),
            "base_sha": clip(row["base_commit"], 100),
            "policy_digest": clip(policy_digest, 100),
        }
        context_by_run.setdefault(str(row["run_id"]), fact)
        events.append(
            event(
                "context_pack",
                "context_bound",
                row["created_at"],
                row["id"],
                fact,
            )
        )

    latest_checkpoint_by_run: dict[str, dict[str, Any]] = {}
    for row in checkpoints:
        payload = checkpoint_payloads[str(row["id"])]
        facts = {
            "run_id": clip(row["run_id"], 200),
            "context_pack_id": clip(row["context_pack_id"], 200),
            "checkpoint_id": clip(row["id"], 200),
            "status": clip(row["status"], 80),
            "head_sha": clip(payload.get("head_commit"), 100),
            "next_action": _checkpoint_action(payload.get("next_action")),
            "completed_count": len(payload.get("completed", ()))
            if isinstance(payload.get("completed"), list)
            else 0,
            "remaining_count": len(payload.get("remaining", ()))
            if isinstance(payload.get("remaining"), list)
            else 0,
            "blocker_count": len(payload.get("blockers", ()))
            if isinstance(payload.get("blockers"), list)
            else 0,
            "evidence_count": len(payload.get("evidence", ()))
            if isinstance(payload.get("evidence"), list)
            else 0,
        }
        latest_checkpoint_by_run[str(row["run_id"])] = facts
        events.append(
            event(
                "checkpoint",
                "checkpoint_saved",
                row["created_at"],
                row["id"],
                facts,
                sequence=int(row["sequence"]),
            )
        )

    for row in runs:
        run_id = str(row["id"])
        details = run_details[run_id]
        context = context_by_run.get(run_id, {})
        checkpoint = latest_checkpoint_by_run.get(run_id, {})
        head_sha = details.get("commit_sha") or checkpoint.get("head_sha") or ""
        base_sha = details.get("base_commit") or context.get("base_sha") or ""
        events.append(
            event(
                "run",
                "run_state",
                row["updated_at"],
                run_id,
                {
                    "run_id": clip(run_id, 200),
                    "stage": clip(row["stage"], 80),
                    "status": clip(row["status"], 80),
                    "created_at": clip(row["created_at"], 100),
                    "head_sha": clip(head_sha, 100),
                    "base_sha": clip(base_sha, 100),
                    "policy_digest": clip(context.get("policy_digest"), 100),
                    "source_run_id": clip(details.get("source_run_id"), 200),
                },
            )
        )
        verification = details.get("verification")
        if isinstance(verification, dict):
            commands = verification.get("commands")
            command_rows = commands if isinstance(commands, list) else []
            events.append(
                event(
                    "verification",
                    "verification_summary",
                    row["updated_at"],
                    f"verification:{run_id}",
                    {
                        "run_id": clip(run_id, 200),
                        "passed": verification.get("passed")
                        if isinstance(verification.get("passed"), bool)
                        else None,
                        "command_count": len(command_rows),
                        "failed_command_count": sum(
                            isinstance(command, dict) and command.get("exit_code") != 0
                            for command in command_rows
                        ),
                        "truncated_output_count": sum(
                            isinstance(command, dict)
                            and bool(command.get("output_truncated"))
                            for command in command_rows
                        ),
                    },
                )
            )

    for row in imports:
        events.append(
            event(
                "context_import",
                "context_imported",
                row["imported_at"],
                row["id"],
                {
                    "work_item_id": clip(row["work_item_id"], 200),
                    "source_run_id": clip(row["source_run_id"], 200),
                    "bundle_digest": clip(row["bundle_digest"], 100),
                },
            )
        )

    for row in harnesses:
        events.append(
            event(
                "harness",
                "harness_bound",
                row["created_at"],
                row["run_id"],
                {
                    "run_id": clip(row["run_id"], 200),
                    "context_pack_id": clip(row["context_pack_id"], 200),
                    "harness": clip(row["harness"], 100),
                    "model": clip(row["model"], 200),
                },
            )
        )

    for row in usage_rows:
        payload = _read_object(
            row["payload"], source="harness_usage", record_id=str(row["id"])
        )
        if _digest(payload) != str(row["event_digest"]):
            raise LifecycleTraceError(
                f"harness_usage record {row['id']} failed its digest check"
            )
        metrics = payload.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        run_id = str(row["run_id"])
        numeric_metrics: dict[str, int | float | None] = {}
        for source_key, output_key in (
            ("event_count", "event_count"),
            ("tool_call_count", "tool_call_count"),
            ("duration_seconds", "duration_seconds"),
        ):
            value = metrics.get(source_key)
            numeric_metrics[output_key] = (
                value
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )
        events.append(
            event(
                "harness_usage",
                "usage_summary",
                row["created_at"],
                row["id"],
                {
                    "run_id": clip(run_id, 200),
                    "run_stage": clip(row["run_stage"], 80),
                    "harness": clip(row["harness"], 100),
                    **numeric_metrics,
                    "event_digest": clip(row["event_digest"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in leases:
        events.append(
            event(
                "lease",
                "lease_event",
                row["created_at"],
                f"lease:{row['sequence']}",
                {
                    "generation": int(row["generation"]),
                    "action": clip(row["action"], 80),
                    "expires_at": clip(row["expires_at"], 100),
                    "owner_digest": _digest(str(row["owner"]))[:16],
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in queue_tasks:
        events.append(
            event(
                "queue_task",
                "queue_state",
                row["updated_at"],
                row["id"],
                {
                    "task_id": clip(row["id"], 200),
                    "action": clip(row["action"], 80),
                    "work_item_id": clip(row["work_item_id"], 200),
                    "run_id": clip(row["run_id"], 200),
                    "issue_number": int(row["issue_number"]),
                    "pull_number": int(row["pull_number"]),
                    "state": clip(row["state"], 80),
                    "priority": int(row["priority"]),
                    "attempt_count": int(row["attempt_count"]),
                    "max_attempts": int(row["max_attempts"]),
                    "manual_required": bool(row["manual_required"]),
                    "error_code": clip(row["last_error_code"], 120),
                    "parameters_digest": clip(row["parameters_digest"], 100),
                    "idempotency_digest": clip(row["idempotency_digest"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in queue_attempts:
        events.append(
            event(
                "queue_attempt",
                "queue_attempt",
                row["created_at"],
                row["id"],
                {
                    "task_id": clip(row["task_id"], 200),
                    "generation": int(row["generation"]),
                    "event": clip(row["event"], 80),
                    "outcome": clip(row["outcome"], 80),
                    "worker_digest": _digest(str(row["worker"]))[:16],
                    "payload_digest": clip(row["payload_digest"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in submissions:
        events.append(
            event(
                "submission",
                "pull_recorded",
                row["created_at"],
                row["pr_url"],
                {
                    "pull_number": _pull_number(row["pr_url"]),
                    "pull_url": clip(row["pr_url"], 300),
                },
            )
        )

    for row in publications:
        events.append(
            event(
                "publication",
                "publication_attempt",
                row["created_at"],
                row["id"],
                {
                    "attempt_id": clip(row["attempt_id"], 200),
                    "step_id": clip(row["step_id"], 200),
                    "run_id": clip(row["run_id"], 200),
                    "actor": clip(row["actor"], 100),
                    "action": clip(row["action"], 80),
                    "stage": clip(row["stage"], 80),
                    "outcome": clip(row["outcome"], 80),
                    "destination": clip(row["destination"], 300),
                    "branch": clip(row["branch"], 300),
                    "head_sha": clip(row["head_sha"], 100),
                    "base_branch": clip(row["base_branch"], 300),
                    "expected_remote_sha": clip(row["expected_remote_sha"], 100),
                    "pull_number": int(row["target_pull_number"]),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in watermarks:
        events.append(
            event(
                "github_watermark",
                "github_activity_bound",
                row["updated_at"],
                row["run_id"],
                {
                    "run_id": clip(row["run_id"], 200),
                    "pull_number": int(row["pull_number"]),
                    "github_sequence": int(row["sequence"]),
                    "batch_digest": clip(row["batch_digest"], 100),
                },
            )
        )

    for row in github_events:
        events.append(
            event(
                "github_pr",
                "github_event",
                row["source_updated_at"]
                or row["source_created_at"]
                or row["ingested_at"],
                f"github:{row['sequence']}",
                {
                    "pull_number": int(row["pull_number"]),
                    "event_type": clip(row["event_type"], 80),
                    "external_id": clip(row["external_id"], 200),
                    "version_digest": clip(row["version_digest"], 100),
                    "head_sha": clip(row["head_sha"], 100),
                    "source_trust": clip(row["source_trust"], 80),
                    "actor": clip(row["source_actor"], 100),
                    "state": clip(row["source_state"], 80),
                    "ingested_at": clip(row["ingested_at"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in owner_reviews:
        events.append(
            event(
                "owner_review",
                "owner_review_attested",
                row["created_at"],
                row["id"],
                {
                    "run_id": clip(row["run_id"], 200),
                    "pull_number": int(row["pull_number"]),
                    "actor": clip(row["actor"], 100),
                    "head_sha": clip(row["head_sha"], 100),
                    "base_sha": clip(row["base_sha"], 100),
                    "policy_digest": clip(row["policy_digest"], 100),
                    "review_facts_digest": clip(row["review_facts_digest"], 100),
                    "attestation_digest": clip(row["attestation_digest"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    for row in merge_decisions:
        events.append(
            event(
                "merge_decision",
                "merge_evaluated",
                row["created_at"],
                row["id"],
                {
                    "pull_number": int(row["pull_number"]),
                    "head_sha": clip(row["head_sha"], 100),
                    "base_sha": clip(row["base_sha"], 100),
                    "policy_digest": clip(row["policy_digest"], 100),
                    "snapshot_digest": clip(row["snapshot_digest"], 100),
                    "eligible": bool(row["eligible"]),
                    "decision_digest": clip(row["decision_digest"], 100),
                },
            )
        )

    for row in merge_executions:
        events.append(
            event(
                "merge_execution",
                "merge_attempt",
                row["created_at"],
                row["id"],
                {
                    "attempt_id": clip(row["attempt_id"], 200),
                    "run_id": clip(row["run_id"], 200),
                    "decision_id": clip(row["decision_id"], 200),
                    "pull_number": int(row["pull_number"]),
                    "actor": clip(row["actor"], 100),
                    "merge_method": clip(row["merge_method"], 80),
                    "stage": clip(row["stage"], 80),
                    "outcome": clip(row["outcome"], 80),
                    "reason": clip(row["reason"], 120),
                    "decision_digest": clip(row["decision_digest"], 100),
                    "head_sha": clip(row["head_sha"], 100),
                },
            )
        )

    for row in dependencies:
        events.append(
            event(
                "dependency",
                "dependency_attested",
                row["created_at"],
                row["id"],
                {
                    "pull_number": int(row["pull_number"]),
                    "dependency_number": int(row["dependency_number"]),
                    "head_sha": clip(row["head_sha"], 100),
                    "action": clip(row["action"], 80),
                    "actor": clip(row["actor"], 100),
                    "source": clip(row["source"], 80),
                    "event_digest": clip(row["event_digest"], 100),
                },
                sequence=int(row["sequence"]),
            )
        )

    events.sort(
        key=lambda value: (
            value["occurred_at"],
            SOURCE_ORDER[value["source"]],
            value["sequence"],
            value["id"],
        )
    )
    events = events[:event_limit]
    represented = Counter(str(value["source"]) for value in events)

    source_totals = {
        "work_item": 1 if work_item is not None else 0,
        "run": run_total,
        "context_pack": context_total,
        "context_import": import_total,
        "harness": harness_total,
        "checkpoint": checkpoint_total,
        "harness_usage": usage_total,
        "verification": verification_total,
        "lease": lease_total,
        "queue_task": queue_task_total,
        "queue_attempt": queue_attempt_total,
        "submission": submission_total,
        "publication": publication_total,
        "github_watermark": watermark_total,
        "github_pr": github_event_total,
        "owner_review": owner_review_total,
        "merge_decision": merge_decision_total,
        "merge_execution": merge_execution_total,
        "dependency": dependency_total,
    }
    available_events = sum(source_totals.values())
    coverage: dict[str, tuple[str, str]] = {}
    for source, total in source_totals.items():
        if total == 0:
            coverage[source] = ("unknown", "no_recorded_facts")
        elif represented[source] < total:
            coverage[source] = ("incomplete", "bounded_output_omitted_records")
        else:
            coverage[source] = ("complete", "recorded_facts_complete")

    if run_total and context_run_total < run_total:
        coverage["context_pack"] = ("incomplete", "some_runs_have_no_context_pack")
    if run_total and checkpoint_run_total < run_total:
        coverage["checkpoint"] = ("incomplete", "some_runs_have_no_checkpoint")
    if harness_total and usage_run_total < harness_total:
        coverage["harness_usage"] = (
            "incomplete",
            "some_harness_runs_have_no_usage_ledger",
        )
    if run_total and verification_total < run_total:
        coverage["verification"] = (
            "incomplete",
            "some_runs_have_no_verification_summary",
        )
    if pull_numbers_omitted:
        for source in (
            "github_pr",
            "owner_review",
            "merge_decision",
            "merge_execution",
            "dependency",
        ):
            coverage[source] = ("incomplete", "bounded_pull_relationships")
    if (
        run_total > len(runs)
        or publication_total > len(publications)
        or queue_task_total > len(queue_tasks)
        or submission_total > len(submissions)
        or watermark_total > len(watermarks)
    ):
        for source in (
            "github_pr",
            "owner_review",
            "merge_decision",
            "merge_execution",
            "dependency",
        ):
            coverage[source] = ("incomplete", "parent_records_omitted")

    sources = [
        {
            "name": name,
            "trust": SOURCE_TRUST[name],
            "status": coverage[name][0],
            "reason": coverage[name][1],
            "records": represented[name],
            "available_records": source_totals[name],
            "omitted_records": max(0, source_totals[name] - represented[name]),
        }
        for name in SOURCE_ORDER
    ]
    work_item_summary = (
        {
            "id": clip(work_item["id"], 200),
            "kind": clip(work_item["kind"], 80),
            "external_id": clip(work_item["external_id"], 100),
            "status": clip(work_item["status"], 80),
        }
        if work_item is not None
        else None
    )
    current: dict[str, Any] | None = None
    next_action = _next_action(latest_runs, work_item)
    if latest_runs:
        latest = latest_runs[0]
        checkpoint = current_checkpoint["payload"] if current_checkpoint else {}
        context = current_context["payload"] if current_context else {}
        project = context.get("project")
        project = project if isinstance(project, dict) else {}
        verification = current_details.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        head_sha = str(
            current_details.get("commit_sha") or checkpoint.get("head_commit") or ""
        )
        current = {
            "run_id": clip(latest["id"], 200),
            "status": clip(latest["status"], 80),
            "stage": clip(latest["stage"], 80),
            "head_sha": clip(head_sha, 100),
            "base_sha": clip(
                current_details.get("base_commit")
                or (current_context or {}).get("base_commit"),
                100,
            ),
            "policy_digest": clip(project.get("policy_digest"), 100),
            "checkpoint_id": clip((current_checkpoint or {}).get("id"), 200),
            "verification_passed": verification.get("passed")
            if isinstance(verification.get("passed"), bool)
            else None,
            "merge_outcome": clip((current_merge or {}).get("outcome"), 80),
        }
        if (
            str(latest["status"]) == "submitted"
            and current_merge
            and head_sha
            and current_merge["head_sha"] == head_sha
        ):
            if current_merge["stage"] == "applying":
                next_action = "reconcile_merge"
            elif current_merge["stage"] == "completed" and current_merge["outcome"] in {
                "merged",
                "already_merged",
            }:
                next_action = "complete"
    trace = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "database_schema_version": database_schema_version,
        "repository": normalized_repository,
        "issue_number": issue_number,
        "work_item": work_item_summary,
        "complete": all(value["status"] == "complete" for value in sources),
        "next_action": next_action,
        "current": current,
        "sources": sources,
        "events": events,
        "stats": {
            "event_limit": event_limit,
            "events": len(events),
            "available_events": available_events,
            "events_omitted": max(0, available_events - len(events)),
            "runs": represented["run"],
            "available_runs": run_total,
            "runs_omitted": max(0, run_total - represented["run"]),
            "pull_numbers": len(selected_pull_numbers),
            "pull_numbers_omitted": pull_numbers_omitted,
            "fields_clipped": clipped_fields,
            "field_chars_omitted": clipped_chars,
        },
        "network_access": False,
        "harness_invoked": False,
        "workspace_modified": False,
        "store_modified": False,
        "public_write": False,
    }
    if len(_canonical_json(trace)) > MAX_JSON_CHARS:
        raise LifecycleTraceError(
            "bounded lifecycle trace exceeded its maximum serialized size"
        )
    return {**trace, "trace_digest": _digest(trace)}


def render_lifecycle_text(trace: dict[str, Any]) -> str:
    """Render a compact bounded view from an already-built trace."""
    stats = trace["stats"]
    lines = [
        f"Lifecycle: {trace['repository']}#{trace['issue_number']}",
        f"Digest: {trace['trace_digest']}",
        f"Complete: {'yes' if trace['complete'] else 'no'}",
        f"Next action: {trace['next_action']}",
        (
            f"Events: {stats['events']}/{stats['available_events']} "
            f"(omitted {stats['events_omitted']})"
        ),
    ]
    current = trace.get("current")
    if isinstance(current, dict):
        lines.append(
            f"Current: run={current['run_id']} status={current['status']} stage={current['stage']}"
        )
        lines.append(
            f"HEAD: {current['head_sha'] or 'unknown'}; base={current['base_sha'] or 'unknown'}"
        )
    lines.append("Sources:")
    for source in trace["sources"]:
        lines.append(
            f"- {source['name']}: {source['status']} "
            f"({source['records']}/{source['available_records']}; "
            f"{source['reason']})"
        )
    lines.append("Events:")
    text_events = trace["events"][:MAX_TEXT_EVENTS]
    for value in text_events:
        scalar_facts = [
            f"{key}={fact}"
            for key, fact in value["facts"].items()
            if fact is not None and fact != "" and not isinstance(fact, (dict, list))
        ][:6]
        suffix = f" {' '.join(scalar_facts)}" if scalar_facts else ""
        lines.append(
            f"- {value['occurred_at']} {value['source']}/{value['kind']}{suffix}"
        )
    text_omitted = len(trace["events"]) - len(text_events)
    if text_omitted:
        lines.append(f"- ... {text_omitted} additional events omitted from text")
    result = "\n".join(lines)
    if len(result) <= MAX_TEXT_CHARS:
        return result
    suffix = "\n... text output clipped to 12000 characters"
    return result[: MAX_TEXT_CHARS - len(suffix)] + suffix
