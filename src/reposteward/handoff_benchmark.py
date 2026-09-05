"""Offline handoff observations compared with independently versioned gold facts.

Fixture repositories and injected verification results exercise control-plane
semantics. They are not measurements of a model or evidence of real tests passing.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from .agent import build_harness_prompt
from .config import VerificationProfile, load_config
from .context import build_context_pack, portable_bundle
from .context_budget import build_follow_up_context
from .external_tasks import ExternalTasks, TaskConflict
from .external_verification import ExternalVerification
from .models import Candidate, Issue, RepositoryInfo, VerificationResult
from .prompt_budget import fit_context
from .store import Store
from .workspace import sanitized_environment


def handoff_gold() -> dict:
    value = json.loads(
        importlib.resources.files("reposteward")
        .joinpath("data", "handoff-gold-v1.json")
        .read_text(encoding="utf-8")
    )
    if value.get("schema_version") != 1 or set(value["cases"]) != set(OBSERVERS):
        raise ValueError("unsupported handoff gold fixture")
    return value


def _issue(body: str = "Handle empty input without changing legacy output") -> Issue:
    return Issue(
        repository="owner/repo",
        number=7,
        node_id=8,
        title="Fix the edge case",
        body=body,
        url="https://github.com/owner/repo/issues/7",
        labels=("bug",),
        comments=0,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        author_login="reporter",
        author_association="NONE",
    )


class _FixtureGitHub:
    """No transport, credential discovery, or public mutation implementation."""

    def __init__(self, head: str):
        self.head = head

    def authenticated_login(self):
        return "owner"

    def issue(self, *_):
        return _issue()

    def repository(self, *_):
        return RepositoryInfo(
            "owner/repo", "main", 1000, 20, 5, "2026-01-02T00:00:00Z", False, False
        )

    def branch_head_sha(self, *_):
        return self.head


@contextmanager
def _task():
    with tempfile.TemporaryDirectory(prefix="reposteward-handoff-") as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        env = sanitized_environment(keep_codex_credentials=False)
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    *args,
                ],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()

        git("init", "-b", "main")
        (repo / "source.txt").write_text("original\n")
        git("add", "source.txt")
        git("commit", "-m", "Fixture baseline")
        git("remote", "add", "origin", "git@github.com:owner/repo.git")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("switch", "-c", "fixture/handoff")
        config_path = root / "config.toml"
        config_path.write_text(
            '[github]\nlogin="owner"\n[repositories."owner/repo"]\nmode="maintainer"\n'
        )
        config = load_config(config_path, include_user=False)
        policy = replace(
            config.repositories["owner/repo"],
            min_stars=0,
            require_assignment_before_submit=False,
            require_no_competing_work=False,
            verification_prefixes=("python ",),
            required_verification_markers=(),
        )
        config = replace(
            config,
            state_dir=root / "state",
            repositories={"owner/repo": policy},
            verification_profiles=(
                VerificationProfile(
                    "owner/repo", "fixture", ("python -m unittest",), ()
                ),
            ),
        )
        service = ExternalTasks(config, github=_FixtureGitHub(git("rev-parse", "HEAD")))
        service.registry.link(repo)
        run = service.start(repo, issue_number=7, reviewed_by="owner")
        yield service, repo, run["run_id"]


def _checkpoint(service, run_id, payload, *, key="checkpoint"):
    current = service.inspect(run_id, live=True)
    return service.checkpoint(
        run_id,
        expected_revision=current["revision"],
        expected_snapshot=current["current_snapshot"]["digest"],
        idempotency_key=key,
        payload=payload,
    )


def _tail():
    tail = "TAIL-AC: preserve the legacy empty-input output."
    with _task() as (service, repo, _):
        candidate = Candidate(
            _issue("background " * 2500 + tail), _FixtureGitHub("").repository(), 50
        )
        pack = build_context_pack(
            candidate,
            service.config.repositories["owner/repo"],
            work_item_id="work",
            run_id="run",
            worktree=repo,
            base_commit="a" * 40,
            harness="external",
            model="",
            task_description_max_bytes=500,
        )
        fitted, _ = fit_context(pack, 40000)
        return {
            "tail_retained": tail in build_harness_prompt(fitted),
            "description_trimmed": tail not in fitted.task.description,
            "contract_review": fitted.task_contract.review_status,
        }


def _feedback():
    with _task() as (service, _, run_id):
        store = Store(service.path)
        comments = [
            {
                "id": n,
                "author": "reviewer",
                "body": f"AC-{n}: " + "detail " * 400,
                "updated_at": "2026-01-02T00:00:00Z",
            }
            for n in range(1, 21)
        ]
        activity = {
            "pull_request": {"number": 12, "head_sha": "a" * 40},
            "comments": [],
            "reviews": [],
            "review_comments": comments,
            "checks": [],
        }
        store.ingest_github_pr_activity(
            run_id=run_id, repository="owner/repo", pull_number=12, activity=activity
        )
        pending = store.pending_feedback("owner/repo", 12)
        plan = build_follow_up_context(
            activity=activity, events=pending["events"], budget_tokens=5000
        )
        after = store.pending_feedback("owner/repo", 12)
        return {
            "pending_items": len(after["events"]),
            "pending_unchanged": pending == after,
            "context_omits_feedback": plan["stats"]["retained_events"]
            < len(pending["events"]),
            "bounded": plan["estimated_tokens"] <= 5000,
        }


def _decisions():
    with _task() as (service, _, run_id):
        old = {
            "statement": "Use an unbounded history",
            "rationale": "initial proposal",
            "evidence": [],
        }
        first = _checkpoint(
            service, run_id, {"next_action": "review choice", "decisions": [old]}
        )
        new = {
            "statement": "Keep current open work and bounded evidence pages",
            "rationale": "Explicitly replaces the unbounded history proposal",
            "evidence": [f"checkpoint:{run_id}:1"],
        }
        _checkpoint(
            service,
            run_id,
            {"next_action": "verify pagination", "decisions": [new]},
            key="replacement",
        )
        result = service.context(run_id)
        historical = ExternalVerification(service.config).evidence(
            run_id, f"checkpoint:{run_id}:1"
        )
        return {
            "current_decisions": [d["statement"] for d in result["decisions"]],
            "replacement_explained": result["decisions"][0]["rationale"]
            == new["rationale"],
            "previous_decision_retrievable": old["statement"] in json.dumps(historical),
            "revision_advanced": result["revision"] == first["revision"] + 1,
        }


def _late_bundle():
    with _task() as (service, _, run_id):
        store = Store(service.path)
        old = portable_bundle(store.context_bundle(run_id))
        _checkpoint(
            service,
            run_id,
            {
                "remaining": ["Handle empty input", "Preserve legacy output"],
                "next_action": "verify both acceptance criteria",
            },
        )
        store.import_context_bundle(old)
        return {
            "open_work": service.context(run_id)["open_work"],
            "revision": service.inspect(run_id)["revision"],
        }


def _dirty():
    with _task() as (service, repo, run_id):
        before = service.inspect(run_id, live=True)
        (repo / "source.txt").write_text("uncommitted change\n")
        after = service.inspect(run_id, live=True)
        rejected = False
        try:
            service.checkpoint(
                run_id,
                expected_revision=0,
                expected_snapshot=before["current_snapshot"]["digest"],
                idempotency_key="old",
                payload={"next_action": "continue"},
            )
        except TaskConflict:
            rejected = True
        return {
            "same_head": before["current_snapshot"]["head"]
            == after["current_snapshot"]["head"],
            "different_snapshot": before["current_snapshot"]["digest"]
            != after["current_snapshot"]["digest"],
            "dirty": after["current_snapshot"]["dirty"],
            "old_snapshot_rejected": rejected,
        }


class _FixtureVerifier:
    def verify(self, *_, **__):
        return VerificationResult(
            True, (), "injected fixture result; no tests executed"
        )


def _stale():
    with _task() as (service, repo, run_id):
        verification = ExternalVerification(service.config, verifier=_FixtureVerifier())
        current = service.inspect(run_id, live=True)
        first = verification.request(
            run_id,
            profile="fixture",
            expected_revision=0,
            expected_snapshot=current["current_snapshot"]["digest"],
            idempotency_key="fixture",
        )
        (repo / "source.txt").write_text("later change\n")
        after = verification.inspect(
            run_id, first["evidence_id"].split(":")[1], live=True
        )
        return {
            "initial_applicability": first["current_applicability"],
            "historical_outcome": after["outcome"],
            "current_applicability": after["current_applicability"],
            "publication_eligible": after["publication_eligible"],
            "verification_backend": "injected_fixture",
        }


def _recovery():
    with _task() as (service, _, run_id):
        current = service.inspect(run_id, live=True)
        args = {
            "expected_revision": 0,
            "expected_snapshot": current["current_snapshot"]["digest"],
            "idempotency_key": "retry",
            "payload": {"remaining": ["Handle empty input"], "next_action": "verify"},
        }
        try:
            with patch.object(
                Store, "update_run", side_effect=RuntimeError("injected interruption")
            ):
                service.checkpoint(run_id, **args)
        except RuntimeError:
            pass
        rolled_back = service.inspect(run_id)["revision"] == 0
        first = service.checkpoint(run_id, **args)
        retry = service.checkpoint(run_id, **args)
        return {
            "interrupted_write_rolled_back": rolled_back,
            "revision": service.inspect(run_id)["revision"],
            "retry_same_checkpoint": first["checkpoint_id"] == retry["checkpoint_id"],
            "retry_idempotent": retry["idempotent"],
            "open_work": service.context(run_id)["open_work"],
        }


OBSERVERS = {
    "tail_acceptance": _tail,
    "feedback_overflow": _feedback,
    "decision_replacement": _decisions,
    "late_bundle": _late_bundle,
    "dirty_snapshot": _dirty,
    "stale_evidence": _stale,
    "interrupted_checkpoint": _recovery,
}


def observe_handoff(case: str) -> dict:
    gold = handoff_gold()
    actual = OBSERVERS[case]()
    expected = gold["cases"][case]
    if actual != expected:
        raise AssertionError(
            f"handoff {case}: observed {actual!r}; expected {expected!r}"
        )
    return {
        "metrics": {"gold_facts_matched": len(expected), "gold_facts_missing": 0},
        "facts": {
            "gold_version": gold["schema_version"],
            "gold_sha256": hashlib.sha256(
                json.dumps(gold, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "observations": actual,
            "measurement": "offline_control_plane",
            "human_handoff_minutes": None,
            "model_quality_measured": False,
        },
    }
