"""Project guidance with explicit review basis, scoped sources and invalidation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import AppConfig
from .context import repository_policy_digest
from .external_tasks import ExternalTasks, TaskConflict
from .external_verification import ExternalVerification
from .policy import PolicyError
from .projects import canonical_digest
from .store import utc_now


def _paths(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 12:
        raise ValueError("knowledge needs between 1 and 12 relative scope paths")
    paths = []
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) > 512
            or not value
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("invalid knowledge scope path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("knowledge scope must stay inside the project")
        paths.append(path.as_posix())
    return tuple(sorted(set(paths)))


def _matches(name: str, prefix: str) -> bool:
    return prefix == "." or name == prefix or name.startswith(prefix + "/")


def _dependencies(snapshot: dict, paths: tuple[str, ...]) -> str:
    return canonical_digest(
        [
            entry
            for entry in snapshot["files"]
            if any(_matches(entry["path"], path) for path in paths)
        ]
    )


class ProjectKnowledge:
    def __init__(self, config: AppConfig):
        self.config = config
        self.tasks = ExternalTasks(config)
        self.verification = ExternalVerification(config)

    def _task(self, run_id: str, *, write: bool = False):
        store = self.tasks._store(write=write)
        record = self.tasks._record(store, run_id)
        current = self.tasks.inspect(run_id, live=True)
        if any(
            value != "workspace_changed_since_checkpoint"
            for value in current["validity"]
        ):
            raise TaskConflict(
                "task authority changed; refresh before managing project knowledge"
            )
        return store, record

    @staticmethod
    def _row(store, project_id: str, identifier: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid knowledge ID")
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM project_knowledge WHERE project_id=? AND id=?",
                (project_id, identifier),
            ).fetchone()
        if row is None:
            raise KeyError("knowledge not found in this project")
        value = dict(row)
        for field in ("scope_paths", "conditions", "evidence", "review"):
            value[field] = json.loads(value[field])
        return value

    def propose(self, run_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {
            "statement",
            "scope_paths",
            "conditions",
            "evidence_ids",
            "supersedes",
        }:
            raise ValueError("unsupported knowledge proposal fields")
        statement = payload.get("statement", "")
        if (
            not isinstance(statement, str)
            or not statement.strip()
            or len(statement) > 2000
        ):
            raise ValueError("knowledge statement must contain 1 to 2000 characters")
        paths = _paths(payload.get("scope_paths", []))
        conditions = payload.get("conditions", {})
        if (
            not isinstance(conditions, dict)
            or set(conditions) - {"branch", "verification_profile"}
            or any(
                not isinstance(value, str) or not value or len(value) > 256
                for value in conditions.values()
            )
        ):
            raise ValueError(
                "knowledge conditions support only branch and verification_profile"
            )
        evidence_ids = payload.get("evidence_ids", [])
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 8
            or any(
                not isinstance(value, str) or len(value) > 180 for value in evidence_ids
            )
        ):
            raise ValueError("knowledge requires 1 to 8 scoped evidence IDs")
        store, record = self._task(run_id, write=True)
        if payload.get("supersedes"):
            self._row(store, record["project_id"], payload["supersedes"])
        sources = []
        for identifier in sorted(set(evidence_ids)):
            if identifier.split(":")[0] not in {
                "source",
                "checkpoint",
                "verification",
                "log",
            }:
                raise ValueError(
                    "knowledge proposals require direct source, checkpoint or verification evidence"
                )
            evidence = self.verification.evidence(run_id, identifier, limit=1)
            if evidence["availability"] != "available":
                raise ValueError(
                    "knowledge evidence is unavailable or failed integrity checks"
                )
            sources.append(
                {
                    "run_id": run_id,
                    "evidence_id": identifier,
                    "digest": evidence["digest"],
                }
            )
        snapshot = self.tasks._snapshot(
            Path(record["workspace_root"]), store.run(run_id)["repository"]
        )
        material = {
            "project_id": record["project_id"],
            "statement": statement.strip(),
            "scope_paths": list(paths),
            "conditions": conditions,
            "evidence": sources,
            "dependencies_digest": _dependencies(snapshot, paths),
            "supersedes": payload.get("supersedes", ""),
        }
        identifier = canonical_digest(material)[:32]
        now = utc_now()
        with store._connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO project_knowledge
                (id,project_id,origin_run_id,status,statement,scope_paths,conditions,evidence,dependencies_digest,supersedes,created_at,updated_at)
                VALUES (?,?,?,'candidate',?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    record["project_id"],
                    run_id,
                    material["statement"],
                    json.dumps(paths),
                    json.dumps(conditions),
                    json.dumps(sources),
                    material["dependencies_digest"],
                    material["supersedes"],
                    now,
                    now,
                ),
            )
        return {
            **self._row(store, record["project_id"], identifier),
            "public_write": False,
        }

    def _validity(self, entry: dict, snapshot: dict, repository: str) -> list[str]:
        reasons = []
        if (
            _dependencies(snapshot, tuple(entry["scope_paths"]))
            != entry["dependencies_digest"]
        ):
            reasons.append("scoped_code_changed")
        if entry["conditions"].get("branch", snapshot["branch"]) != snapshot["branch"]:
            reasons.append("branch_condition_not_met")
        profiles = {
            profile.name: profile
            for profile in self.config.verification_profiles
            if profile.repository == repository.casefold()
        }
        required = entry["conditions"].get("verification_profile")
        if required and required not in profiles:
            reasons.append("profile_condition_not_met")
        review = entry["review"]
        if review.get("policy_digest") and review[
            "policy_digest"
        ] != repository_policy_digest(self.tasks._policy(repository)):
            reasons.append("verification_policy_changed")
        if review.get("profile"):
            profile = profiles.get(review["profile"])
            if (
                profile is None
                or self.verification._profile_digest(profile)
                != review["profile_digest"]
            ):
                reasons.append("verification_profile_changed")
        for source in entry["evidence"]:
            try:
                evidence = self.verification.evidence(
                    source["run_id"], source["evidence_id"], limit=1
                )
                if (
                    evidence["availability"] != "available"
                    or evidence["digest"] != source["digest"]
                ):
                    reasons.append("source_evidence_changed_or_missing")
            except (OSError, ValueError, RuntimeError, KeyError):
                reasons.append("source_evidence_unavailable")
        if review.get("verification_id"):
            try:
                proof = self.verification.evidence(
                    review["run_id"], review["verification_id"], limit=1
                )
                if (
                    proof["availability"] != "available"
                    or proof["digest"] != review["verification_digest"]
                ):
                    reasons.append("review_evidence_changed_or_missing")
            except (OSError, ValueError, RuntimeError, KeyError):
                reasons.append("review_evidence_unavailable")
        return sorted(set(reasons))

    def promote(
        self,
        run_id: str,
        identifier: str,
        *,
        reviewed_by: str,
        basis: str,
        rationale: str,
        verification_id: str = "",
    ) -> dict:
        if (
            not reviewed_by
            or reviewed_by.casefold() != self.config.github.login.casefold()
        ):
            raise PolicyError(
                "knowledge reviewer must match the configured GitHub login"
            )
        if (
            basis not in {"human_confirmation", "verification_evidence"}
            or not rationale.strip()
            or len(rationale) > 2000
        ):
            raise ValueError(
                "knowledge review requires an explicit basis and bounded rationale"
            )
        store, record = self._task(run_id, write=True)
        repository = store.run(run_id)["repository"]
        with store.atomic():
            entry = self._row(store, record["project_id"], identifier)
            snapshot = self.tasks._snapshot(Path(record["workspace_root"]), repository)
            if self._validity(entry, snapshot, repository):
                raise TaskConflict(
                    "knowledge sources, conditions or scoped code changed; propose updated evidence"
                )
            if entry["status"] == "replaced":
                raise TaskConflict("replaced knowledge cannot be promoted again")
            review = {
                "reviewed_by": reviewed_by,
                "basis": basis,
                "rationale": rationale,
                "trust": "human_attested"
                if basis == "human_confirmation"
                else "reviewed_with_test_evidence",
            }
            if basis == "verification_evidence":
                proof = self.verification.inspect(
                    run_id, verification_id.removeprefix("verification:"), live=True
                )
                if (
                    proof["outcome"] != "passed"
                    or proof["current_applicability"] != "matches"
                ):
                    raise TaskConflict(
                        "knowledge promotion requires current passed verification evidence"
                    )
                evidence = self.verification.evidence(
                    run_id, proof["evidence_id"], limit=1
                )
                if evidence["availability"] != "available":
                    raise TaskConflict("review evidence is unavailable")
                review.update(
                    {
                        "run_id": run_id,
                        "verification_id": proof["evidence_id"],
                        "verification_digest": evidence["digest"],
                        "profile": proof["profile"],
                        "profile_digest": proof["profile_digest"],
                        "policy_digest": proof["policy_digest"],
                        "snapshot_digest": proof["snapshot"]["digest"],
                    }
                )
            elif verification_id:
                raise ValueError(
                    "human confirmation must not masquerade as verification evidence"
                )
            if entry["status"] == "reviewed":
                if entry["review"] != review:
                    raise TaskConflict(
                        "knowledge already has a different review; use a replacement"
                    )
                return {**entry, "idempotent": True, "public_write": False}
            with store._connection() as db:
                if entry["supersedes"]:
                    previous = self._row(
                        store, record["project_id"], entry["supersedes"]
                    )
                    if previous["successor"] and previous["successor"] != identifier:
                        raise TaskConflict("knowledge already has another replacement")
                    db.execute(
                        "UPDATE project_knowledge SET status='replaced',successor=?,updated_at=? WHERE id=?",
                        (identifier, utc_now(), previous["id"]),
                    )
                db.execute(
                    "UPDATE project_knowledge SET status='reviewed',review=?,updated_at=? WHERE id=?",
                    (json.dumps(review), utc_now(), identifier),
                )
        return {
            **self._row(store, record["project_id"], identifier),
            "idempotent": False,
            "public_write": False,
        }

    def inspect(self, run_id: str, identifier: str, *, live: bool = False) -> dict:
        store = self.tasks._store()
        record = self.tasks._record(store, run_id)
        entry = self._row(store, record["project_id"], identifier)
        reasons = []
        if live:
            store, record = self._task(run_id)
            repository = store.run(run_id)["repository"]
            snapshot = self.tasks._snapshot(Path(record["workspace_root"]), repository)
            reasons = self._validity(entry, snapshot, repository)
        return {
            **entry,
            "invalidations": reasons,
            "effective_status": "stale"
            if reasons and entry["status"] == "reviewed"
            else entry["status"],
            "freshness": "local_checked" if live else "not_checked",
            "public_write": False,
        }

    def list(
        self,
        run_id: str,
        *,
        scope_paths: tuple[str, ...] = (".",),
        limit: int = 5,
        include_inactive: bool = False,
    ) -> dict:
        paths = _paths(scope_paths)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("knowledge limit must be between 1 and 50")
        store, record = self._task(run_id)
        repository = store.run(run_id)["repository"]
        snapshot = self.tasks._snapshot(Path(record["workspace_root"]), repository)
        with store._connection() as db:
            rows = db.execute(
                "SELECT id FROM project_knowledge WHERE project_id=? ORDER BY updated_at DESC,id LIMIT 201",
                (record["project_id"],),
            ).fetchall()
        entries, suppressed = [], 0
        for row in rows[:200]:
            entry = self._row(store, record["project_id"], row["id"])
            if not any(
                _matches(left, right) or _matches(right, left)
                for left in paths
                for right in entry["scope_paths"]
            ):
                suppressed += 1
                continue
            reasons = self._validity(entry, snapshot, repository)
            entry["effective_status"] = (
                "stale"
                if reasons and entry["status"] == "reviewed"
                else entry["status"]
            )
            entry["invalidations"] = reasons
            if not include_inactive and (entry["status"] != "reviewed" or reasons):
                suppressed += 1
                continue
            entries.append(entry)
        return {
            "project_id": record["project_id"],
            "entries": entries[:limit],
            "omitted": max(0, len(entries) - limit),
            "suppressed": suppressed,
            "scan_incomplete": len(rows) > 200,
            "scope_paths": list(paths),
            "remote_freshness": "not_refreshed",
            "public_write": False,
        }
