#!/usr/bin/env python3
"""Plan and apply exact cleanup of merged same-repository GitHub branches."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Protocol
from urllib.parse import quote, urlencode

SCHEMA_VERSION = 1
WRITE_GATE = "REPOSTEWARD_ENABLE_BRANCH_CLEANUP"


class BranchCleanupError(RuntimeError):
    """Reject an unsafe plan or report a bounded GitHub operation failure."""


class GitHubAPI(Protocol):
    def authenticated_login(self) -> str: ...

    def repository(self) -> dict[str, Any]: ...

    def branches(self) -> list[dict[str, Any]]: ...

    def pull_requests(
        self, *, state: str = "all", head: str = ""
    ) -> list[dict[str, Any]]: ...

    def branch(self, name: str) -> dict[str, Any] | None: ...

    def pull_request(self, number: int) -> dict[str, Any]: ...

    def delete_branch(self, name: str, expected_sha: str) -> None: ...


class GhAPI:
    """Small GitHub REST adapter that relies on the host gh authentication."""

    def __init__(self, repository: str) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(parts):
            raise BranchCleanupError("repository must be owner/name")
        self.repository_name = "/".join(parts)
        self.endpoint_repository = quote(self.repository_name, safe="/")

    @staticmethod
    def _json(command: list[str], *, allow_not_found: bool = False) -> Any:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise BranchCleanupError("GitHub request could not start") from exc
        if completed.returncode:
            if allow_not_found and "404" in completed.stderr:
                return None
            raise BranchCleanupError(
                f"GitHub request failed with exit code {completed.returncode}"
            )
        if not completed.stdout.strip():
            return None
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BranchCleanupError("GitHub returned invalid JSON") from exc

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        paginate: bool = False,
        allow_not_found: bool = False,
    ) -> Any:
        command = ["gh", "api"]
        if method != "GET":
            command.extend(["--method", method])
        if paginate:
            command.extend(["--paginate", "--slurp"])
        command.append(endpoint)
        payload = self._json(command, allow_not_found=allow_not_found)
        if not paginate or payload is None:
            return payload
        if not isinstance(payload, list):
            raise BranchCleanupError("paginated GitHub response is not a list")
        pages = payload
        if pages and all(isinstance(page, list) for page in pages):
            return [item for page in pages for item in page]
        return pages

    def repository(self) -> dict[str, Any]:
        payload = self._api(f"repos/{self.endpoint_repository}")
        if not isinstance(payload, dict):
            raise BranchCleanupError("GitHub repository response is incomplete")
        return payload

    def authenticated_login(self) -> str:
        payload = self._api("user")
        if not isinstance(payload, dict) or not str(payload.get("login") or ""):
            raise BranchCleanupError("GitHub authenticated identity is incomplete")
        return str(payload["login"])

    def branches(self) -> list[dict[str, Any]]:
        payload = self._api(
            f"repos/{self.endpoint_repository}/branches?per_page=100", paginate=True
        )
        if not isinstance(payload, list):
            raise BranchCleanupError("GitHub branch response is incomplete")
        return payload

    def pull_requests(
        self, *, state: str = "all", head: str = ""
    ) -> list[dict[str, Any]]:
        query = {"state": state, "per_page": "100", "sort": "updated"}
        if head:
            query["head"] = head
        endpoint = (
            f"repos/{self.endpoint_repository}/pulls?{urlencode(query, safe='/')}"
        )
        payload = self._api(endpoint, paginate=True)
        if not isinstance(payload, list):
            raise BranchCleanupError("GitHub pull-request response is incomplete")
        return payload

    def branch(self, name: str) -> dict[str, Any] | None:
        payload = self._api(
            f"repos/{self.endpoint_repository}/branches/{quote(name, safe='')}",
            allow_not_found=True,
        )
        if payload is not None and not isinstance(payload, dict):
            raise BranchCleanupError("GitHub branch response is incomplete")
        return payload

    def pull_request(self, number: int) -> dict[str, Any]:
        payload = self._api(f"repos/{self.endpoint_repository}/pulls/{number}")
        if not isinstance(payload, dict):
            raise BranchCleanupError("GitHub pull-request response is incomplete")
        return payload

    def delete_branch(self, name: str, expected_sha: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
            raise BranchCleanupError("expected branch SHA is invalid")
        remote = f"git@github.com:{self.repository_name}.git"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GH_ENTERPRISE_TOKEN",
                "GH_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GITHUB_TOKEN",
            }
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "push",
                    "--porcelain",
                    f"--force-with-lease=refs/heads/{name}:{expected_sha}",
                    remote,
                    f":refs/heads/{name}",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        except OSError as exc:
            raise BranchCleanupError(
                "leased Git branch deletion could not start"
            ) from exc
        if completed.returncode:
            raise BranchCleanupError(
                f"leased Git branch deletion failed with exit code {completed.returncode}"
            )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _head_facts(pull: dict[str, Any]) -> tuple[str, str, str]:
    head = pull.get("head")
    if not isinstance(head, dict):
        return "", "", ""
    repository = head.get("repo")
    full_name = (
        str(repository.get("full_name") or "") if isinstance(repository, dict) else ""
    )
    return full_name, str(head.get("ref") or ""), str(head.get("sha") or "")


def _is_merged(pull: dict[str, Any]) -> bool:
    return str(pull.get("state") or "").casefold() == "closed" and bool(
        pull.get("merged_at")
    )


def _local_json(command: list[str]) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GH_ENTERPRISE_TOKEN",
            "GH_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GITHUB_TOKEN",
        }
    }
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, env=environment
        )
    except OSError as exc:
        raise BranchCleanupError("local RepoSteward read could not start") from exc
    if completed.returncode:
        raise BranchCleanupError(
            f"local RepoSteward read failed with exit code {completed.returncode}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BranchCleanupError(
            "local RepoSteward read returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BranchCleanupError("local RepoSteward read is incomplete")
    return payload


def load_managed_runs(repository: str, run_ids: list[str]) -> list[dict[str, Any]]:
    """Bind selected submitted runs to their local branch, SHA, and PR identity."""
    selected = sorted(set(run_ids))
    if not selected:
        raise BranchCleanupError("at least one --run-id is required")
    if any(not re.fullmatch(r"[0-9a-f]{32}", run_id) for run_id in selected):
        raise BranchCleanupError("--run-id must be a 32-character lowercase hex ID")

    report = _local_json(
        [
            "uv",
            "run",
            "reposteward",
            "usage",
            "report",
            repository,
            "--group-by",
            "none",
            "--include-runs",
        ]
    )
    rows = report.get("runs")
    if not isinstance(rows, list):
        raise BranchCleanupError("usage report did not include run identities")
    indexed = {
        str(row.get("run_id") or ""): row for row in rows if isinstance(row, dict)
    }

    managed: list[dict[str, Any]] = []
    for run_id in selected:
        row = indexed.get(run_id)
        if row is None:
            raise BranchCleanupError(
                f"selected run is absent from usage report: {run_id}"
            )
        if (
            str(row.get("repository") or "").casefold() != repository.casefold()
            or str(row.get("status") or "") != "submitted"
            or int(row.get("pull_number") or 0) <= 0
        ):
            raise BranchCleanupError(
                f"selected run is not a submitted PR run: {run_id}"
            )
        inspected = _local_json(["uv", "run", "reposteward", "inspect", run_id])
        branch = str(inspected.get("branch") or "")
        head_sha = str(inspected.get("commit_sha") or "")
        if (
            str(inspected.get("repository") or "").casefold() != repository.casefold()
            or str(inspected.get("status") or "") != "submitted"
            or not branch
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
        ):
            raise BranchCleanupError(f"selected run inspection is incomplete: {run_id}")
        managed.append(
            {
                "run_id": run_id,
                "branch": branch,
                "head_sha": head_sha,
                "pull_number": int(row["pull_number"]),
            }
        )
    return managed


def build_plan(
    repository: dict[str, Any],
    branches: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    managed_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify branches from complete repository, branch, and PR snapshots."""
    repository_name = str(repository.get("full_name") or "")
    default_branch = str(repository.get("default_branch") or "")
    if not repository_name or not default_branch:
        raise BranchCleanupError("repository identity or default branch is missing")
    folded_repository = repository_name.casefold()
    candidates: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    managed_by_branch: dict[str, list[dict[str, Any]]] = {}
    normalized_runs: list[dict[str, Any]] = []
    for run in managed_runs:
        run_id = str(run.get("run_id") or "")
        branch_name = str(run.get("branch") or "")
        head_sha = str(run.get("head_sha") or "")
        pull_number = int(run.get("pull_number") or 0)
        if (
            not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or not branch_name
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or pull_number <= 0
        ):
            raise BranchCleanupError("managed run binding is incomplete")
        normalized = {
            "run_id": run_id,
            "branch": branch_name,
            "head_sha": head_sha,
            "pull_number": pull_number,
        }
        normalized_runs.append(normalized)
        managed_by_branch.setdefault(branch_name, []).append(normalized)
    normalized_runs.sort(key=lambda value: str(value["run_id"]))
    present_branches: set[str] = set()

    for branch in sorted(branches, key=lambda value: str(value.get("name") or "")):
        name = str(branch.get("name") or "")
        commit = branch.get("commit")
        head_sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        if not name or not head_sha:
            raise BranchCleanupError("branch identity or head SHA is missing")
        present_branches.add(name)

        branch_pulls = []
        for pull in pulls:
            head_repository, head_branch, _pull_sha = _head_facts(pull)
            if head_repository.casefold() != folded_repository or head_branch != name:
                continue
            branch_pulls.append(pull)

        reasons: list[str] = []
        if name == default_branch:
            reasons.append("default_branch")
        if branch.get("protected") is True:
            reasons.append("protected_branch")
        elif branch.get("protected") is not False:
            reasons.append("protection_unknown")
        if any(
            str(pull.get("state") or "").casefold() == "open" for pull in branch_pulls
        ):
            reasons.append("open_pull_request")
        bindings = managed_by_branch.get(name, [])
        if not bindings:
            reasons.append("not_selected_managed_run")
        elif len(bindings) != 1:
            reasons.append("ambiguous_managed_runs")
        else:
            binding = bindings[0]
            if str(binding["head_sha"]) != head_sha:
                reasons.append("head_changed_from_managed_run")
            matching_pulls = [
                pull
                for pull in branch_pulls
                if int(pull.get("number") or 0) == int(binding["pull_number"])
                and _is_merged(pull)
                and _head_facts(pull)[2] == head_sha
            ]
            if len(branch_pulls) != 1:
                reasons.append("shared_branch_history")
            if len(matching_pulls) != 1:
                reasons.append("no_exact_merged_pull")

        if bindings and len(bindings) == 1 and not branch_pulls:
            reasons.append("no_exact_merged_pull")

        if reasons:
            retained.append(
                {
                    "branch": name,
                    "head_sha": head_sha,
                    "reasons": sorted(set(reasons)),
                }
            )
            continue

        binding = bindings[0]
        pull = branch_pulls[0]
        candidates.append(
            {
                "branch": name,
                "head_sha": head_sha,
                "pull_number": int(binding["pull_number"]),
                "pull_url": str(pull.get("html_url") or ""),
                "run_id": str(binding["run_id"]),
            }
        )

    for run in normalized_runs:
        if str(run["branch"]) not in present_branches:
            absent.append({**run, "status": "already_absent"})

    facts = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository_name,
        "default_branch": default_branch,
        "delete_branch_on_merge": bool(repository.get("delete_branch_on_merge")),
        "managed_runs": normalized_runs,
        "candidates": candidates,
        "retained": retained,
        "absent": absent,
    }
    return {
        **facts,
        "counts": {
            "candidates": len(candidates),
            "retained": len(retained),
            "already_absent": len(absent),
            "total": len(candidates) + len(retained) + len(absent),
        },
        "plan_digest": _digest(facts),
        "public_write": False,
    }


def plan_from_client(
    client: GitHubAPI, managed_runs: list[dict[str, Any]]
) -> dict[str, Any]:
    return build_plan(
        client.repository(), client.branches(), client.pull_requests(), managed_runs
    )


def _candidate_blockers(
    client: GitHubAPI, candidate: dict[str, Any]
) -> tuple[list[str], bool]:
    repository = client.repository()
    repository_name = str(repository.get("full_name") or "")
    default_branch = str(repository.get("default_branch") or "")
    branch_name = str(candidate["branch"])
    expected_sha = str(candidate["head_sha"])
    branch = client.branch(branch_name)
    if branch is None:
        return [], True

    blockers: list[str] = []
    commit = branch.get("commit")
    current_sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
    if branch_name == default_branch:
        blockers.append("default_branch")
    if branch.get("protected") is True:
        blockers.append("protected_branch")
    elif branch.get("protected") is not False:
        blockers.append("protection_unknown")
    if current_sha != expected_sha:
        blockers.append("head_changed")

    owner = repository_name.split("/", 1)[0]
    branch_pulls = [
        pull
        for pull in client.pull_requests(state="all", head=f"{owner}:{branch_name}")
        if _head_facts(pull)[0].casefold() == repository_name.casefold()
        and _head_facts(pull)[1] == branch_name
    ]
    if any(str(pull.get("state") or "").casefold() == "open" for pull in branch_pulls):
        blockers.append("open_pull_request")
    if len(branch_pulls) != 1:
        blockers.append("shared_branch_history")

    pull = client.pull_request(int(candidate["pull_number"]))
    pull_repository, pull_branch, pull_sha = _head_facts(pull)
    if (
        pull_repository.casefold() != repository_name.casefold()
        or pull_branch != branch_name
    ):
        blockers.append("pull_head_changed")
    if pull_sha != expected_sha:
        blockers.append("pull_sha_changed")
    if not _is_merged(pull):
        blockers.append("pull_not_merged")
    if not any(
        int(value.get("number") or 0) == int(candidate["pull_number"])
        for value in branch_pulls
    ):
        blockers.append("pull_history_changed")
    return sorted(set(blockers)), False


def apply_plan(
    client: GitHubAPI,
    plan: dict[str, Any],
    *,
    expected_digest: str,
    gate_enabled: bool,
    reviewed_by: str,
) -> dict[str, Any]:
    if not gate_enabled:
        raise BranchCleanupError(f"set {WRITE_GATE}=1 for apply")
    current_digest = str(plan.get("plan_digest") or "")
    if not expected_digest or expected_digest != current_digest:
        raise BranchCleanupError("--expected-digest does not match the fresh plan")
    authenticated = client.authenticated_login()
    if not reviewed_by or reviewed_by.casefold() != authenticated.casefold():
        raise BranchCleanupError(
            "--reviewed-by must match the authenticated GitHub login"
        )
    permissions = client.repository().get("permissions")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        raise BranchCleanupError(
            "authenticated GitHub login lacks confirmed push access"
        )

    actions: list[dict[str, Any]] = [
        {
            "branch": str(value["branch"]),
            "head_sha": str(value["head_sha"]),
            "run_id": str(value["run_id"]),
            "status": "already_absent",
            "write_attempted": False,
        }
        for value in plan.get("absent", [])
    ]
    for candidate in plan.get("candidates", []):
        branch_name = str(candidate["branch"])
        head_sha = str(candidate["head_sha"])
        run_id = str(candidate["run_id"])
        try:
            blockers, absent = _candidate_blockers(client, candidate)
        except BranchCleanupError:
            actions.append(
                {
                    "branch": branch_name,
                    "head_sha": head_sha,
                    "run_id": run_id,
                    "status": "failed",
                    "reasons": ["freshness_read_failed"],
                    "write_attempted": False,
                }
            )
            continue
        if absent:
            actions.append(
                {
                    "branch": branch_name,
                    "head_sha": head_sha,
                    "run_id": run_id,
                    "status": "already_absent",
                    "write_attempted": False,
                }
            )
            continue
        if blockers:
            actions.append(
                {
                    "branch": branch_name,
                    "head_sha": head_sha,
                    "run_id": run_id,
                    "status": "blocked",
                    "reasons": blockers,
                    "write_attempted": False,
                }
            )
            continue

        try:
            client.delete_branch(branch_name, head_sha)
        except BranchCleanupError:
            try:
                remaining = client.branch(branch_name)
            except BranchCleanupError:
                remaining = "unknown"
            if remaining == "unknown":
                actions.append(
                    {
                        "branch": branch_name,
                        "head_sha": head_sha,
                        "run_id": run_id,
                        "status": "outcome_unknown",
                        "reasons": ["delete_and_reconciliation_failed"],
                        "write_attempted": True,
                    }
                )
            elif remaining is None:
                actions.append(
                    {
                        "branch": branch_name,
                        "head_sha": head_sha,
                        "run_id": run_id,
                        "status": "reconciled_deleted",
                        "write_attempted": True,
                    }
                )
            else:
                actions.append(
                    {
                        "branch": branch_name,
                        "head_sha": head_sha,
                        "run_id": run_id,
                        "status": "failed",
                        "reasons": ["leased_delete_failed_branch_still_exists"],
                        "write_attempted": True,
                    }
                )
            continue

        try:
            remaining = client.branch(branch_name)
        except BranchCleanupError:
            remaining = "unknown"
        if remaining is None:
            status, reasons = "deleted", []
        elif remaining == "unknown":
            status, reasons = "outcome_unknown", ["delete_confirmation_failed"]
        else:
            status, reasons = "failed", ["delete_confirmation_found_branch"]
        action = {
            "branch": branch_name,
            "head_sha": head_sha,
            "run_id": run_id,
            "status": status,
            "write_attempted": True,
        }
        if reasons:
            action["reasons"] = reasons
        actions.append(action)

    complete = all(
        action["status"] in {"already_absent", "deleted", "reconciled_deleted"}
        for action in actions
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": plan["repository"],
        "plan_digest": current_digest,
        "complete": complete,
        "actions": actions,
        "public_write": any(bool(action["write_attempted"]) for action in actions),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan exact cleanup of merged same-repository GitHub branches."
    )
    parser.add_argument("repository", help="GitHub repository as owner/name")
    parser.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="submitted RepoSteward run that owns a branch; repeat as needed",
    )
    parser.add_argument("--apply", action="store_true", help="delete eligible branches")
    parser.add_argument(
        "--expected-digest",
        default="",
        help="fresh plan_digest required together with --apply",
    )
    parser.add_argument(
        "--reviewed-by",
        default="",
        help="authenticated GitHub login required together with --apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client = GhAPI(args.repository)
        managed_runs = load_managed_runs(args.repository, args.run_id)
        plan = plan_from_client(client, managed_runs)
        if not args.apply:
            result = plan
            exit_code = 0
        else:
            result = apply_plan(
                client,
                plan,
                expected_digest=args.expected_digest,
                gate_enabled=os.environ.get(WRITE_GATE) == "1",
                reviewed_by=args.reviewed_by,
            )
            exit_code = 0 if result["complete"] else 1
    except BranchCleanupError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "repository": args.repository,
            "error": str(exc),
            "public_write": False,
        }
        exit_code = 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
