"""Scoped evidence for exact external-development snapshots; no publication authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from typing import Any

from .config import AppConfig, VerificationProfile
from .external_tasks import ExternalTasks, TaskConflict
from .models import AgentResult
from .projects import canonical_digest
from .snapshots import snapshot_summary, verify_snapshot_copy
from .store import Store, utc_now
from .verifier import DockerVerifier, VerificationCancelled, VerificationError


class ExternalVerification:
    def __init__(self, config: AppConfig, *, verifier: DockerVerifier | None = None):
        self.config = config
        self.tasks = ExternalTasks(config)
        self.verifier = verifier or DockerVerifier(config)

    def profiles(self, run_id: str) -> list[dict[str, Any]]:
        store = self.tasks._store()
        self.tasks._record(store, run_id)
        repository = store.run(run_id)["repository"]
        return [
            {
                "name": profile.name,
                "commands": list(profile.commands),
                "bootstrap_commands": list(profile.bootstrap_commands),
            }
            for profile in self.config.verification_profiles
            if profile.repository == repository.casefold()
        ]

    def _profile(self, repository: str, name: str) -> VerificationProfile:
        for profile in self.config.verification_profiles:
            if (profile.repository, profile.name) == (repository.casefold(), name):
                return profile
        raise VerificationError(
            "profile is not configured for this repository in user-owned configuration"
        )

    def _profile_digest(self, profile: VerificationProfile) -> str:
        return canonical_digest(
            {
                "profile": asdict(profile),
                "runner": asdict(self.config.runner),
                "trusted_sensitive_paths": self.config.safety.tracked_sensitive_paths_for(
                    profile.repository
                ),
            }
        )

    def _directory(self, identifier: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid verification evidence ID")
        return self.config.state_dir / "external-verification" / identifier

    def _active(self, identifier: str) -> bool:
        try:
            descriptor = os.open(
                self._directory(identifier) / "lease", os.O_RDONLY | os.O_NOFOLLOW
            )
        except FileNotFoundError:
            return False
        with os.fdopen(descriptor, "r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def _row(self, store: Store, run_id: str, identifier: str) -> dict[str, Any]:
        self.tasks._record(store, run_id)
        self._directory(identifier)
        with store._connection() as db:
            row = db.execute(
                "SELECT * FROM external_verifications WHERE id=? AND run_id=?",
                (identifier, run_id),
            ).fetchone()
        if row is None:
            raise KeyError("verification evidence not found in this task")
        return {
            **dict(row),
            "snapshot": json.loads(row["snapshot"]),
            "result": json.loads(row["result"]),
        }

    def inspect(
        self, run_id: str, identifier: str, *, live: bool = False
    ) -> dict[str, Any]:
        store = self.tasks._store()
        row = self._row(store, run_id, identifier)
        outcome = row["outcome"]
        reason = row["reason"]
        if outcome == "running" and not self._active(identifier):
            outcome, reason = (
                "unknown",
                "execution interrupted without a terminal record",
            )
        validity = []
        if live:
            current = self.tasks.inspect(run_id, live=True)
            validity = [
                value
                for value in current["validity"]
                if value != "workspace_changed_since_checkpoint"
            ]
            if current["revision"] != row["revision"]:
                validity.append("checkpoint_revision_changed")
            if current["current_snapshot"]["digest"] != row["snapshot"]["digest"]:
                validity.append("workspace_changed_since_verification")
            try:
                profile = self._profile(store.run(run_id)["repository"], row["profile"])
                if self._profile_digest(profile) != row["profile_digest"]:
                    validity.append("verification_profile_changed")
            except VerificationError:
                validity.append("verification_profile_unavailable")
        return {
            "evidence_id": f"verification:{identifier}",
            "run_id": run_id,
            "project_id": row["project_id"],
            "revision": row["revision"],
            "profile": row["profile"],
            "profile_digest": row["profile_digest"],
            "policy_digest": row["policy_digest"],
            "base_commit": row["base_commit"],
            "snapshot": snapshot_summary(row["snapshot"]),
            "outcome": outcome,
            "reason": reason,
            "result": row["result"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "validity": validity,
            "current_applicability": "not_checked"
            if not live
            else "matches"
            if outcome == "passed" and not validity
            else "not_verified",
            "evidence_kind": "development_snapshot",
            "publication_eligible": False,
            "public_write": False,
        }

    def list(self, run_id: str, *, limit: int = 20) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("evidence limit must be between 1 and 100")
        store = self.tasks._store()
        self.tasks._record(store, run_id)
        with store._connection() as db:
            rows = db.execute(
                "SELECT id FROM external_verifications WHERE run_id=? ORDER BY created_at DESC,id LIMIT ?",
                (run_id, limit),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) FROM external_verifications WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        return {
            "run_id": run_id,
            "evidence": [self.inspect(run_id, row["id"]) for row in rows],
            "omitted": max(0, total - limit),
            "public_write": False,
        }

    def request(
        self,
        run_id: str,
        *,
        profile: str,
        expected_revision: int,
        expected_snapshot: str,
        idempotency_key: str,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot)
        ):
            raise ValueError("verification needs an exact revision and snapshot digest")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("invalid verification idempotency key")
        store = self.tasks._store(write=True)
        record = self.tasks._record(store, run_id)
        run = store.run(run_id)
        selected = self._profile(run["repository"], profile)
        profile_digest = self._profile_digest(selected)
        request_digest = canonical_digest(
            {
                "run_id": run_id,
                "profile_digest": profile_digest,
                "revision": expected_revision,
                "snapshot": expected_snapshot,
            }
        )
        identifier = canonical_digest(
            {"run_id": run_id, "idempotency_key": idempotency_key}
        )[:32]
        with store._connection() as db:
            existing = db.execute(
                "SELECT request_digest FROM external_verifications WHERE id=?",
                (identifier,),
            ).fetchone()
        if existing:
            if existing["request_digest"] != request_digest:
                raise TaskConflict(
                    "verification idempotency key was used for different input"
                )
            return {**self.inspect(run_id, identifier, live=True), "idempotent": True}
        directory = self._directory(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            directory / "lease", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TaskConflict(
                    "verification request is already starting; query its evidence ID"
                ) from exc
            with store.atomic():
                with store._connection() as db:
                    if db.execute(
                        "SELECT 1 FROM external_verifications WHERE id=?", (identifier,)
                    ).fetchone():
                        raise TaskConflict(
                            "verification request started concurrently; query its evidence ID"
                        )
                current = self.tasks.inspect(run_id, live=True)
                fresh_run = store.run(run_id)
                if fresh_run["status"] != "running" or fresh_run["stage"] != "external":
                    raise TaskConflict("external task is no longer running")
                if current["revision"] != expected_revision:
                    raise TaskConflict(
                        "checkpoint revision changed before verification"
                    )
                if any(
                    value != "workspace_changed_since_checkpoint"
                    for value in current["validity"]
                ):
                    raise TaskConflict(
                        "task policy or base changed before verification"
                    )
                snapshot = self.tasks._snapshot(
                    Path(record["workspace_root"]), run["repository"]
                )
                if snapshot["digest"] != expected_snapshot:
                    raise TaskConflict("workspace changed before verification")
                now = utc_now()
                with store._connection() as db:
                    db.execute(
                        """INSERT INTO external_verifications
                        (id,run_id,project_id,revision,idempotency_key,request_digest,profile,profile_digest,
                        policy_digest,base_commit,snapshot,outcome,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,'running',?,?)""",
                        (
                            identifier,
                            run_id,
                            record["project_id"],
                            expected_revision,
                            idempotency_key,
                            request_digest,
                            profile,
                            profile_digest,
                            record["policy_digest"],
                            record["base_commit"],
                            json.dumps(snapshot),
                            now,
                            now,
                        ),
                    )
            outcome, reason, result = "unknown", "verification did not finish", {}
            try:
                policy = replace(
                    self.tasks._policy(run["repository"]),
                    bootstrap_commands=selected.bootstrap_commands,
                )

                def guard(copy: Path, phase: str) -> None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise VerificationCancelled("verification cancelled by client")
                    verify_snapshot_copy(
                        copy,
                        snapshot,
                        exact=phase == "copied",
                        trusted_sensitive_paths=self.config.safety.tracked_sensitive_paths_for(
                            policy.name
                        ),
                    )
                    observed = self.tasks.inspect(run_id, live=True)
                    if (
                        any(
                            value != "workspace_changed_since_checkpoint"
                            for value in observed["validity"]
                        )
                        or observed["current_snapshot"]["digest"] != snapshot["digest"]
                    ):
                        raise TaskConflict(
                            "source workspace or authority changed during verification"
                        )

                verification = self.verifier.verify(
                    Path(record["workspace_root"]),
                    policy,
                    AgentResult("external verification", "", "", selected.commands),
                    run_dir=directory,
                    snapshot_guard=guard,
                    **(
                        {"cancel_event": cancel_event}
                        if cancel_event is not None
                        else {}
                    ),
                )
                result = self._result(verification, directory)
                outcome = "passed" if verification.passed else "failed"
                reason = verification.reason
                if any(command.exit_code == 124 for command in verification.commands):
                    outcome, reason = "unknown", "verification timed out"
            except (KeyboardInterrupt, VerificationCancelled):
                outcome, reason = "cancelled", "verification cancelled by operator"
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                outcome, reason = (
                    "unknown",
                    f"verification interrupted: {type(exc).__name__}: {str(exc)[:500]}",
                )
            finally:
                if not result:
                    result = self._partial_result(directory, selected)
                with store._connection() as db:
                    db.execute(
                        "UPDATE external_verifications SET outcome=?,reason=?,result=?,updated_at=? WHERE id=?",
                        (outcome, reason, json.dumps(result), utc_now(), identifier),
                    )
        return {**self.inspect(run_id, identifier, live=True), "idempotent": False}

    @staticmethod
    def _partial_result(
        directory: Path, profile: VerificationProfile
    ) -> dict[str, Any]:
        commands = []
        planned = (
            [("00-bootstrap.log", " && ".join(profile.bootstrap_commands))]
            if profile.bootstrap_commands
            else []
        ) + [
            (f"{index:02d}-command.log", command)
            for index, command in enumerate(profile.commands, 1)
        ]
        for filename, command in planned:
            path = directory / "verification" / filename
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as handle:
                raw = handle.read(8_000_001)
            if len(raw) > 8_000_000:
                continue
            commands.append(
                {
                    "command": command,
                    "exit_code": None,
                    "output": raw.decode("utf-8", errors="replace")[-2000:],
                    "output_truncated": True,
                    "log": {
                        "index": len(commands),
                        "path": f"verification/{filename}",
                        "stored_digest": hashlib.sha256(raw).hexdigest(),
                        "stored_bytes": len(raw),
                        "complete": False,
                    },
                }
            )
        return {
            "passed": False,
            "commands": commands,
            "integrity": "partial",
            "reason": "execution did not establish a terminal test result",
        }

    @staticmethod
    def _result(verification, directory: Path) -> dict[str, Any]:
        commands = []
        for index, command in enumerate(verification.commands):
            value = asdict(command)
            log_path = value.pop("log_path")
            value["output"] = value["output"][-2000:]
            value["output_truncated"] = (
                command.output_truncated or len(command.output) > 2000
            )
            if log_path:
                path = Path(log_path)
                if path.is_symlink() or not path.resolve().is_relative_to(
                    directory.resolve()
                ):
                    raise VerificationError("verification log escaped evidence storage")
                raw = path.read_bytes()
                value["log"] = {
                    "index": index,
                    "path": str(path.relative_to(directory)),
                    "stored_digest": hashlib.sha256(raw).hexdigest(),
                    "stored_bytes": len(raw),
                    "complete": not command.log_truncated,
                }
            commands.append(value)
        return {
            "passed": verification.passed,
            "commands": commands,
            "reason": verification.reason,
        }

    def evidence(
        self, run_id: str, evidence_id: str, *, offset: int = 0, limit: int = 8000
    ) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError(
                "evidence needs a nonnegative offset and limit between 1 and 16000"
            )
        store = self.tasks._store()
        self.tasks._record(store, run_id)
        parts = evidence_id.split(":")
        raw = None
        digest = ""
        complete = True
        if len(parts) == 2 and parts[0] == "verification":
            raw = json.dumps(
                self.inspect(run_id, parts[1]), ensure_ascii=False, sort_keys=True
            ).encode()
        elif len(parts) == 3 and parts[0] == "log" and parts[2].isdigit():
            row = self._row(store, run_id, parts[1])
            index = int(parts[2])
            commands = row["result"].get("commands", [])
            if index >= len(commands) or not commands[index].get("log"):
                raise KeyError("verification log is unavailable")
            log = commands[index]["log"]
            directory = self._directory(parts[1])
            relative = Path(log["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise VerificationError("invalid evidence log path")
            target = directory / relative
            if any(
                parent.is_symlink()
                for parent in [target, *target.parents]
                if parent != directory.parent
            ):
                raise VerificationError("evidence log path must not contain symlinks")
            try:
                descriptor = os.open(
                    target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                with os.fdopen(descriptor, "rb") as handle:
                    raw = handle.read(8_000_001)
            except FileNotFoundError:
                raw = None
            digest = log["stored_digest"]
            complete = log["complete"]
        elif len(parts) == 2 and parts[0] == "source":
            with store._connection() as db:
                row = db.execute(
                    "SELECT digest FROM external_task_sources WHERE run_id=? AND digest=?",
                    (run_id, parts[1]),
                ).fetchone()
            if row is None:
                raise KeyError("source evidence not found in this task")
            digest = row["digest"]
            raw = store.content_blob(digest)
        elif (
            len(parts) == 3
            and parts[0] == "checkpoint"
            and parts[1] == run_id
            and parts[2].isdigit()
        ):
            with store._connection() as db:
                row = db.execute(
                    """SELECT c.payload FROM external_task_checkpoints e JOIN checkpoints c ON c.id=e.checkpoint_id
                    WHERE e.run_id=? AND e.revision=?""",
                    (run_id, int(parts[2])),
                ).fetchone()
            raw = row["payload"].encode() if row else None
        else:
            raise KeyError("unsupported or out-of-scope evidence ID")
        if raw is None:
            return {
                "evidence_id": evidence_id,
                "availability": "unknown",
                "reason": "evidence payload unavailable",
            }
        if len(raw) > 8_000_000 or digest and hashlib.sha256(raw).hexdigest() != digest:
            return {
                "evidence_id": evidence_id,
                "availability": "unknown",
                "reason": "evidence integrity check failed",
            }
        content = raw.decode("utf-8", errors="replace")
        end = min(len(content), offset + limit)
        return {
            "evidence_id": evidence_id,
            "run_id": run_id,
            "availability": "available",
            "digest": hashlib.sha256(raw).hexdigest(),
            "complete": complete,
            "text": content[offset:end],
            "offset": offset,
            "returned": len(content[offset:end]),
            "total": len(content),
            "unit": "characters",
            "next_offset": end if end < len(content) else None,
            "public_write": False,
        }
