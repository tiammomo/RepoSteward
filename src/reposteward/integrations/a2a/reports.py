"""A2A 1.0 report projection over existing scoped assistance operations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

from a2a.types import AgentCard, ListTasksResponse, SendMessageRequest, Task
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import MessageToDict, ParseDict

from reposteward import __version__
from reposteward.storage.local_queue import digest

STATES = {
    "pending": "TASK_STATE_SUBMITTED",
    "running": "TASK_STATE_WORKING",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "cancelled": "TASK_STATE_CANCELED",
}


class ReportAgent:
    def __init__(self, service, *, origin: str, token: str):
        self.service, self.origin, self.token = service, origin, token

    def card(self) -> dict:
        value = {
            "name": "RepoSteward project reports",
            "description": "Source-cited reports for one explicitly linked workspace. Report completion is not development-task delivery.",
            "version": __version__,
            "supportedInterfaces": [
                {
                    "url": self.origin,
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "securitySchemes": {
                "localBearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
            },
            "securityRequirements": [{"schemes": {"localBearer": {"list": []}}}],
            "skills": [
                {
                    "id": "project-understanding",
                    "name": "Understand a linked project",
                    "description": "Generate a bounded reading route from an explicitly scanned source index. Text is the focus; JSON accepts focus, mode and limit. Poll task IDs after reconnecting.",
                    "tags": ["code", "onboarding", "evidence"],
                }
            ],
        }
        ParseDict(value, AgentCard())
        return value

    def operation(self, identifier: str) -> dict:
        value = self.service.get(identifier)
        if value["action"] != "assistance.understanding":
            raise TaskNotFoundError()
        return value

    def task(self, operation: dict, *, artifacts: bool = True) -> dict:
        value = {
            "id": operation["id"],
            "contextId": self.service.scope,
            "status": {
                "state": STATES[operation["state"]],
                "timestamp": operation["updated_at"],
            },
            "metadata": {
                "operationId": operation["id"],
                "cancelRequested": operation["cancel_requested"],
                "developmentTaskCompleted": False,
                "errorCode": operation["last_error_code"],
            },
        }
        if artifacts and operation["stages"]:
            result = operation["stages"][-1]["result"]
            value["artifacts"] = [
                {
                    "artifactId": digest(result),
                    "name": "Project understanding report",
                    "parts": [{"data": result, "mediaType": "application/json"}],
                }
            ]
        ParseDict(value, Task())
        return value

    def send(self, body: dict) -> tuple[dict, bool]:
        request = ParseDict(body, SendMessageRequest())
        message, config = request.message, request.configuration
        if (
            request.tenant
            or message.task_id
            or message.reference_task_ids
            or message.extensions
        ):
            raise UnsupportedOperationError()
        if message.context_id and message.context_id != self.service.scope:
            raise InvalidParamsError(message="Unknown workspace context.")
        if (
            message.role != 1
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", message.message_id)
            or len(message.parts) != 1
        ):
            raise InvalidParamsError(
                message="One user message part and a bounded message ID are required."
            )
        if config.HasField("history_length") and config.history_length < 0:
            raise InvalidParamsError()
        if config.HasField("task_push_notification_config"):
            from a2a.utils.errors import PushNotificationNotSupportedError

            raise PushNotificationNotSupportedError()
        if (
            config.accepted_output_modes
            and "application/json" not in config.accepted_output_modes
        ):
            raise ContentTypeNotSupportedError()
        part = message.parts[0]
        kind = part.WhichOneof("content")
        if kind == "text" and part.media_type in {"", "text/plain"}:
            arguments = {"focus": part.text}
        elif kind == "data" and part.media_type in {"", "application/json"}:
            arguments = MessageToDict(part)["data"]
            if not isinstance(arguments, dict):
                raise InvalidParamsError()
        else:
            raise ContentTypeNotSupportedError()
        if set(arguments) - {"mode", "focus", "limit"}:
            raise InvalidParamsError(
                message="Only focus, mode and limit are supported."
            )
        if any(
            name in arguments and not isinstance(arguments[name], str)
            for name in ("mode", "focus")
        ):
            raise InvalidParamsError(message="Mode and focus must be strings.")
        # Protobuf Value represents all JSON numbers as floats.
        if (
            "limit" in arguments
            and isinstance(arguments["limit"], float)
            and arguments["limit"].is_integer()
        ):
            arguments["limit"] = int(arguments["limit"])
        operation = self.service.start(
            "understanding", idempotency_key="a2a:" + message.message_id, **arguments
        )
        return self.task(operation), config.return_immediately

    def cancel(self, identifier: str) -> dict:
        current = self.operation(identifier)
        if current["state"] not in {"pending", "running"}:
            raise TaskNotCancelableError()
        if not current["cancel_requested"]:
            current = self.service.cancel(
                identifier,
                expected_revision=current["revision"],
                idempotency_key="a2a-cancel:" + current["revision"],
            )
        return self.task(current)

    def listing(self, parameters) -> dict:
        from a2a.types import ListTasksRequest

        raw = dict(parameters)
        for field in ("pageSize", "historyLength"):
            if field in raw:
                if not re.fullmatch(r"[0-9]{1,6}", raw[field]):
                    raise InvalidParamsError()
                raw[field] = int(raw[field])
        if "includeArtifacts" in raw:
            if raw["includeArtifacts"] not in {"true", "false"}:
                raise InvalidParamsError()
            raw["includeArtifacts"] = raw["includeArtifacts"] == "true"
        request = ParseDict(raw, ListTasksRequest())
        size = request.page_size if request.HasField("page_size") else 50
        if request.tenant or not 1 <= size <= 100 or request.history_length < 0:
            raise InvalidParamsError()
        self.service.bridge._scope()
        reverse = {v: k for k, v in STATES.items()}
        from a2a.types import TaskState

        state = ""
        if request.status:
            state = reverse.get(TaskState.Name(request.status), "unrepresented")
        after = (
            request.status_timestamp_after.ToDatetime().isoformat(
                timespec="microseconds"
            )
            + "+00:00"
            if request.HasField("status_timestamp_after")
            else ""
        )
        fingerprint = digest(
            {
                "scope": self.service.scope,
                "context": request.context_id,
                "state": state,
                "after": after,
                "artifacts": request.include_artifacts,
                "size": size,
            }
        )
        before_time, before_id = "", ""
        if request.page_token:
            try:
                encoded, signature = request.page_token.split(".")
                if len(encoded) > 1000 or not hmac.compare_digest(
                    signature,
                    hmac.new(
                        self.token.encode(), encoded.encode(), hashlib.sha256
                    ).hexdigest(),
                ):
                    raise ValueError("invalid cursor")
                cursor = json.loads(base64.urlsafe_b64decode(encoded))
                if cursor["fingerprint"] != fingerprint:
                    raise ValueError("changed query")
                before_time, before_id = cursor["time"], cursor["id"]
            except (ValueError, KeyError, TypeError) as exc:
                raise InvalidParamsError(message="Invalid page token.") from exc
        store = self.service.local.store()
        rows, total = [], 0
        if store and request.context_id in {"", self.service.scope}:
            where = """FROM queue_tasks q JOIN local_operation_plans p ON p.id=q.plan_id
                WHERE q.operation_family='local' AND q.action='assistance.understanding'
                AND q.account_digest=? AND q.scope_key=? AND json_extract(p.payload,'$.scope_digest')=?
                AND (?='' OR q.state=?) AND (?='' OR q.updated_at>=?)"""
            args = (
                self.service.local.account,
                self.service.project,
                self.service.scope,
                state,
                state,
                after,
                after,
            )
            with store._connection() as db:
                total = db.execute("SELECT count(*) " + where, args).fetchone()[0]
                rows = db.execute(
                    "SELECT q.id,q.updated_at "
                    + where
                    + " AND (?='' OR q.updated_at<? OR (q.updated_at=? AND q.id<?)) ORDER BY q.updated_at DESC,q.id DESC LIMIT ?",
                    (*args, before_time, before_time, before_time, before_id, size + 1),
                ).fetchall()
        tasks, last = [], None
        for row in rows[:size]:
            task = self.task(
                self.operation(row["id"]), artifacts=request.include_artifacts
            )
            if tasks and len(json.dumps([*tasks, task]).encode()) > 260_000:
                break
            tasks.append(task)
            last = row
        next_token = ""
        if len(rows) > len(tasks) and last:
            encoded = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "time": last["updated_at"],
                        "id": last["id"],
                    }
                ).encode()
            ).decode()
            next_token = (
                encoded
                + "."
                + hmac.new(
                    self.token.encode(), encoded.encode(), hashlib.sha256
                ).hexdigest()
            )
        result = {
            "tasks": tasks,
            "nextPageToken": next_token,
            "pageSize": size,
            "totalSize": total,
        }
        ParseDict(result, ListTasksResponse())
        return result
