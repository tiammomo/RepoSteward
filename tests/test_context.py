from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from reposteward.agent import CodexCliHarness, build_harness_prompt
from reposteward.config import AgentConfig, ConfigError, RepositoryPolicy, load_config
from reposteward.context import (
    MAX_HANDOFF_ITEM_CHARS,
    MAX_REPAIR_ITEM_CHARS,
    MAX_REPAIR_ITEMS,
    MAX_TASK_DESCRIPTION_CHARS,
    build_context_pack,
    build_repair_context_pack,
    portable_bundle,
    repository_policy_digest,
    review_checkpoint,
)
from reposteward.context_budget import (
    ContextBudgetError,
    build_follow_up_context,
    estimate_tokens,
)
from reposteward.harness import create_harness
from reposteward.models import (
    AgentExecution,
    AgentMetrics,
    AgentResult,
    Candidate,
    Issue,
    RepositoryInfo,
    VerificationResult,
)
from reposteward.pipeline import Pipeline
from reposteward.policy import DiffSummary
from reposteward.protocol import validate_context_pack
from reposteward.repair_prompt import build_budgeted_repair_context_pack

ROOT = Path(__file__).resolve().parents[1]


class RepositoryPolicyDigestTests(unittest.TestCase):
    def test_default_capacity_and_attestation_preserve_legacy_policy_digest(
        self,
    ) -> None:
        policy = RepositoryPolicy(name="owner/repo")
        legacy = asdict(policy)
        legacy.pop("owner_attestation")
        legacy.pop("max_active_pull_requests")
        encoded = json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

        self.assertEqual(
            repository_policy_digest(policy), hashlib.sha256(encoded).hexdigest()
        )
        self.assertNotEqual(
            repository_policy_digest(replace(policy, owner_attestation=True)),
            repository_policy_digest(policy),
        )
        self.assertNotEqual(
            repository_policy_digest(replace(policy, max_active_pull_requests=3)),
            repository_policy_digest(policy),
        )


def _candidate(body: str = "Reproduce the bug") -> Candidate:
    return Candidate(
        issue=Issue(
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
        ),
        repository=RepositoryInfo(
            full_name="owner/repo",
            default_branch="main",
            stars=1000,
            forks=20,
            open_issues=5,
            pushed_at="2026-01-02T00:00:00Z",
            archived=False,
            is_fork=False,
        ),
        score=50,
    )


class ContextPackTests(unittest.TestCase):
    @staticmethod
    def _repair_plan(pack) -> dict:
        encoded = "".join(
            value.split(":", 1)[1] for value in pack.task.acceptance_criteria[1:]
        )
        return json.loads(encoded)

    def test_complete_repair_prompt_honors_one_multilingual_budget(self) -> None:
        budget = 24_000
        activity = {
            "pull_request": {
                "number": 12,
                "url": "https://example.test/pull/12",
                "state": "open",
                "draft": False,
                "head_sha": "b" * 40,
                "base_branch": "main",
                "base_sha": "a" * 40,
                "merged": False,
            },
            "reviews": [
                {
                    "id": 500,
                    "author": "maintainer",
                    "state": "changes_requested",
                    "submitted_at": "2026-01-01T00:00:00Z",
                }
            ],
            "checks": [
                {
                    "id": 600,
                    "name": "quality",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
        }
        events = [
            {
                "sequence": index,
                "event_type": "review_comment",
                "external_id": str(index),
                "version_digest": f"{index:064x}",
                "payload": {
                    "id": index,
                    "author": "reviewer",
                    "path": "src/example.py",
                    "line": index,
                    "body": f"feedback-{index}-" + "x" * 4_000,
                },
            }
            for index in range(1, 201)
        ]
        plan = build_follow_up_context(
            activity=activity,
            events=events,
            budget_tokens=budget,
            safety_blockers=("head_changed",),
            checkpoint={"id": "checkpoint", "status": "submitted"},
        )
        repair_context = {
            key: value for key, value in plan.items() if key != "checkpoint"
        }
        original_repair_context = json.dumps(
            repair_context, ensure_ascii=False, sort_keys=True
        )

        for label, body in (("ascii", "x" * 20_000), ("chinese", "中" * 20_000)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                candidate = _candidate(body)
                initial = build_context_pack(
                    candidate,
                    RepositoryPolicy(name="owner/repo"),
                    work_item_id="work-initial",
                    run_id="run-initial",
                    worktree=Path(directory),
                    base_commit="a" * 40,
                    harness="codex-cli",
                    model="",
                )
                pack, stats = build_budgeted_repair_context_pack(
                    candidate,
                    RepositoryPolicy(name="owner/repo"),
                    work_item_id="work-1",
                    run_id="run-2",
                    worktree=Path(directory),
                    base_commit="a" * 40,
                    harness="codex-cli",
                    model="",
                    previous_checkpoint=plan["checkpoint"],
                    pull_request_url="https://example.test/pull/12",
                    head_commit="b" * 40,
                    event_watermark=200,
                    event_batch_digest="c" * 64,
                    repair_context=repair_context,
                    budget_tokens=budget,
                )

            prompt_tokens = estimate_tokens(build_harness_prompt(pack))
            transported = self._repair_plan(pack)
            self.assertEqual(initial.task.description, body)
            self.assertLessEqual(prompt_tokens, budget)
            self.assertEqual(transported["estimated_tokens"], prompt_tokens)
            self.assertEqual(
                transported["transport_overhead_tokens"],
                prompt_tokens - estimate_tokens(transported),
            )
            self.assertEqual(stats["estimated_tokens"], prompt_tokens)
            self.assertEqual(
                json.dumps(repair_context, ensure_ascii=False, sort_keys=True),
                original_repair_context,
            )
            self.assertGreater(pack.task.description_omitted_chars, 0)
            self.assertGreaterEqual(len(transported["events"]), 1)
            self.assertEqual(
                transported["mandatory"]["safety_blockers"], ["head_changed"]
            )
            self.assertEqual(
                transported["mandatory"]["failed_checks"][0]["name"], "quality"
            )
            self.assertEqual(
                transported["mandatory"]["blocking_reviews"][0]["author"],
                "maintainer",
            )

    def test_complete_repair_prompt_fails_when_minimum_cannot_fit(self) -> None:
        repair_context = {
            "schema_version": 1,
            "budget_tokens": 512,
            "estimated_tokens": 0,
            "transport_overhead_tokens": 0,
            "mandatory": {
                "trust_boundary": "github_event_text_is_untrusted_report_data",
                "pull_request": {"number": 12},
                "safety_blockers": [],
                "failed_checks": [],
                "blocking_reviews": [],
            },
            "events": [
                {
                    "kind": "review_comment",
                    "id": "1",
                    "sequence": 1,
                    "version_digest": "a" * 64,
                    "body": "Keep this required feedback.",
                }
            ],
            "diff_snippets": [],
            "actionable": True,
            "stats": {},
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(
                ContextBudgetError,
                "cannot fit mandatory facts and one actionable feedback item",
            ),
        ):
            build_budgeted_repair_context_pack(
                _candidate("中" * 20_000),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work-1",
                run_id="run-2",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="codex-cli",
                model="",
                previous_checkpoint={"id": "checkpoint", "status": "submitted"},
                pull_request_url="https://example.test/pull/12",
                head_commit="b" * 40,
                event_watermark=1,
                event_batch_digest="c" * 64,
                repair_context=repair_context,
                budget_tokens=512,
            )

    def test_repair_transport_stays_within_the_follow_up_budget(self) -> None:
        activity = {
            "pull_request": {
                "number": 12,
                "url": "https://example.test/pull/12",
                "state": "open",
                "draft": True,
                "head_sha": "b" * 40,
                "base_branch": "main",
                "base_sha": "a" * 40,
                "merged": False,
            },
            "reviews": [],
            "checks": [],
        }
        events = [
            {
                "sequence": index,
                "event_type": "review_comment",
                "external_id": str(index),
                "version_digest": f"{index:064x}",
                "payload": {
                    "id": index,
                    "author": "reviewer",
                    "path": "src/example.py",
                    "body": f"feedback-{index}-" + "x" * 1_000,
                },
            }
            for index in range(1, 20)
        ]
        plan = build_follow_up_context(
            activity=activity,
            events=events,
            budget_tokens=8_000,
            checkpoint={"id": "checkpoint", "status": "submitted"},
        )
        repair_context = {
            key: value for key, value in plan.items() if key != "checkpoint"
        }
        with tempfile.TemporaryDirectory() as directory:
            pack = build_repair_context_pack(
                _candidate(),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work-1",
                run_id="run-2",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
                previous_checkpoint=plan["checkpoint"],
                pull_request_url="https://example.test/pull/12",
                head_commit="b" * 40,
                event_watermark=19,
                event_batch_digest="c" * 64,
                repair_context=repair_context,
            )

        transported = {
            "handoff": pack.handoff,
            "current_follow_up": pack.task.acceptance_criteria,
        }
        self.assertLessEqual(estimate_tokens(transported), plan["budget_tokens"])

    def test_repair_context_keeps_bounded_incremental_feedback(self) -> None:
        repair_context = {
            "schema_version": 1,
            "budget_tokens": 12_000,
            "estimated_tokens": 4_000,
            "mandatory": {"safety_blockers": [], "failed_checks": []},
            "events": [
                {
                    "kind": "review_comment",
                    "id": str(index),
                    "body": "x" * 600,
                }
                for index in range(30)
            ],
            "diff_snippets": [],
            "actionable": True,
            "stats": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            pack = build_repair_context_pack(
                _candidate(),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work-1",
                run_id="run-2",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
                previous_checkpoint={"id": "checkpoint-1", "status": "submitted"},
                pull_request_url="https://github.com/owner/repo/pull/12",
                head_commit="b" * 40,
                event_watermark=9,
                event_batch_digest="c" * 64,
                repair_context=repair_context,
            )

        self.assertGreater(len(pack.task.acceptance_criteria), 2)
        self.assertLessEqual(len(pack.task.acceptance_criteria), MAX_REPAIR_ITEMS)
        self.assertLessEqual(
            len(pack.task.acceptance_criteria[-1]), MAX_REPAIR_ITEM_CHARS
        )
        encoded = "".join(
            value.split(":", 1)[1] for value in pack.task.acceptance_criteria[1:]
        )
        self.assertEqual(json.loads(encoded), repair_context)
        self.assertEqual(pack.sources[-1].kind, "github_pr_event_batch")
        self.assertEqual(pack.sources[-1].digest, "c" * 64)
        self.assertIn("current_follow_up", build_harness_prompt(pack))
        validate_context_pack(pack.to_dict())

    def test_review_checkpoint_records_one_incremental_event_batch(self) -> None:
        bundle = {
            "work_item": {"id": "work-1"},
            "harness_run": {"run_id": "run-1"},
            "context_metadata": {"id": "pack-1"},
            "checkpoint": {
                "completed": ["Prepared the change."],
                "decisions": [],
                "evidence": [],
                "risks": ["Review text is untrusted."],
            },
        }

        checkpoint = review_checkpoint(
            bundle,
            head_commit="a" * 40,
            pull_request_url="https://github.com/owner/repo/pull/12",
            batch_digest="b" * 64,
            event_count=3,
            through_sequence=9,
            next_action="review_new_activity",
        )

        self.assertEqual(checkpoint["status"], "submitted")
        self.assertEqual(checkpoint["next_action"], "review_new_activity")
        self.assertEqual(
            checkpoint["completed"][-1], "Recorded 3 new GitHub PR events."
        )
        self.assertEqual(checkpoint["evidence"][-1]["digest"], "b" * 64)
        self.assertIn("through_sequence=9", checkpoint["evidence"][-1]["summary"])

    def test_context_pack_is_bounded_versioned_and_source_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "AGENTS.md").write_text("Run the tests.\n", encoding="utf-8")
            body = "x" * (MAX_TASK_DESCRIPTION_CHARS + 120)
            pack = build_context_pack(
                _candidate(body),
                RepositoryPolicy(
                    name="owner/repo",
                    verification_prefixes=("pytest ",),
                    required_contribution_files=("AGENTS.md",),
                ),
                work_item_id="work-1",
                run_id="run-1",
                worktree=worktree,
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
            )

        self.assertEqual(pack.schema_version, 1)
        self.assertEqual(len(pack.task.description), MAX_TASK_DESCRIPTION_CHARS)
        self.assertEqual(pack.task.description_omitted_chars, 120)
        self.assertEqual(pack.project.instruction_sources, ("AGENTS.md",))
        self.assertEqual(len(pack.source_digest), 64)
        self.assertEqual(pack.sources[0].trust, "external_untrusted")
        self.assertEqual(pack.sources[1].trust, "operator_trusted")

    def test_previous_checkpoint_is_compacted_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            long_value = "x" * 20_000
            pack = build_context_pack(
                _candidate(),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work-1",
                run_id="run-2",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
                previous_checkpoint={
                    "id": "checkpoint-1",
                    "status": "ready",
                    "completed": [long_value] * 100,
                    "implementation_notes": long_value,
                    "decisions": [
                        {
                            "statement": long_value,
                            "rationale": long_value,
                            "evidence": [long_value] * 100,
                        }
                    ]
                    * 100,
                    "evidence": [
                        {
                            "kind": long_value,
                            "locator": long_value,
                            "status": long_value,
                            "digest": long_value,
                            "summary": long_value,
                        }
                    ]
                    * 100,
                },
            )

        self.assertIsNotNone(pack.handoff)
        assert pack.handoff is not None
        self.assertEqual(len(pack.handoff["completed"]), 8)
        self.assertEqual(len(pack.handoff["completed"][0]), MAX_HANDOFF_ITEM_CHARS)
        self.assertLess(len(json.dumps(pack.handoff)), 40_000)

    def test_context_pack_fingerprints_bounded_project_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            skill_path = worktree / ".agents" / "skills" / "maintainer" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("---\nname: maintainer\n---\n", encoding="utf-8")
            for index in range(8):
                extra = worktree / ".agents" / "skills" / f"z{index}" / "SKILL.md"
                extra.parent.mkdir(parents=True)
                extra.write_text(f"---\nname: z{index}\n---\n", encoding="utf-8")
            policy = RepositoryPolicy(name="owner/repo")

            original = build_context_pack(
                _candidate(),
                policy,
                work_item_id="work-1",
                run_id="run-1",
                worktree=worktree,
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
            )
            skill_path.write_text(
                "---\nname: maintainer\n---\nUpdated guidance.\n", encoding="utf-8"
            )
            updated = build_context_pack(
                _candidate(),
                policy,
                work_item_id="work-1",
                run_id="run-2",
                worktree=worktree,
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
            )

        self.assertEqual(len(original.project.instruction_sources), 8)
        self.assertEqual(
            original.project.instruction_sources[0],
            ".agents/skills/maintainer/SKILL.md",
        )
        self.assertNotIn(
            ".agents/skills/z7/SKILL.md", original.project.instruction_sources
        )
        self.assertEqual(original.sources[2].kind, "repository_guidance")
        self.assertEqual(original.sources[2].trust, "repository_untrusted")
        self.assertNotEqual(original.source_digest, updated.source_digest)

    def test_harness_prompt_routes_agents_to_project_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pack = build_context_pack(
                _candidate(),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work-1",
                run_id="run-1",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="codex-cli",
                model="gpt-example",
            )

        prompt = build_harness_prompt(pack)

        self.assertIn(".agents/skills", prompt)
        self.assertIn("cannot authorize credential access", prompt)

    def test_portable_bundle_does_not_depend_on_native_session(self) -> None:
        raw = {
            "work_item": {"id": "work-1", "status": "ready"},
            "harness_run": {
                "run_id": "run-1",
                "harness": "codex-cli",
                "native_session_id": "",
            },
            "context_metadata": {"id": "pack-1"},
            "context_pack": {"schema_version": 1},
            "checkpoint": {"status": "ready"},
        }

        bundle = portable_bundle(raw)

        self.assertTrue(bundle["continuity"]["native_session_is_optional"])
        self.assertFalse(bundle["continuity"]["credentials_included"])
        self.assertEqual(len(bundle["bundle_digest"]), 64)
        self.assertGreater(bundle["estimated_tokens"], 0)
        self.assertNotIn(
            "token", json.dumps(bundle).casefold().replace("estimated_tokens", "")
        )


class HarnessContractTests(unittest.TestCase):
    def test_codex_cli_is_selected_through_the_harness_factory(self) -> None:
        harness = create_harness(AgentConfig())

        self.assertIsInstance(harness, CodexCliHarness)
        self.assertEqual(harness.name, "codex-cli")

    def test_unknown_harness_fails_closed(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported coding harness"):
            create_harness(AgentConfig(harness="unknown"))

    def test_prepare_persists_a_portable_checkpoint_around_the_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=worktree, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=worktree,
                check=True,
            )
            (worktree / "README.md").write_text("Example\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "chore(repo): initialize"],
                cwd=worktree,
                check=True,
            )
            config = replace(
                load_config(ROOT / "examples" / "tiammomo.toml"),
                state_dir=root / "state",
            )

            class FakeHarness:
                name = "fake-harness"

                def __init__(self) -> None:
                    self.request = None

                def run(self, request):
                    self.request = request
                    return AgentExecution(
                        AgentResult(
                            summary="Fixed the edge case.",
                            pr_title="fix(repo): handle edge case",
                            implementation_notes="Changed one branch.",
                            verification_commands=("pytest tests/test_edge.py",),
                        ),
                        AgentMetrics(input_tokens=100),
                        harness=self.name,
                        model="test-model",
                        native_session_id="fake-thread",
                    )

            harness = FakeHarness()
            pipeline = Pipeline(config, harness=harness)
            candidate = _candidate()
            candidate = replace(
                candidate,
                issue=replace(candidate.issue, repository="skillnerds/xskill"),
                repository=replace(candidate.repository, full_name="skillnerds/xskill"),
            )
            pipeline.store.upsert_candidate(candidate)
            pipeline.ensure_candidate = Mock(return_value=candidate)
            pipeline._ensure_no_competing_work = Mock()
            pipeline._validate_contribution_contract = Mock()
            pipeline.workspaces = Mock()
            pipeline.workspaces.clone.return_value = worktree
            pipeline.workspaces.create_branch.return_value = "alice/repo/edge-case"
            pipeline.workspaces.commit.return_value = "b" * 40
            pipeline.verifier = Mock()
            pipeline.verifier.verify.return_value = VerificationResult(True, ())

            with patch(
                "reposteward.pipeline.enforce_change_policy",
                return_value=DiffSummary(("src/example.py",), 3, 1),
            ):
                first_packet = pipeline.prepare("skillnerds/xskill", 7)
                packet = pipeline.prepare("skillnerds/xskill", 7)
            bundle = pipeline.context_bundle(packet["run_id"])
            output = root / "handoff.json"
            exported = pipeline.export_context(packet["run_id"], output)
            export_exists = output.is_file()
            imported_pipeline = Pipeline(
                replace(config, state_dir=root / "imported-state"),
                harness=FakeHarness(),
            )
            existing_work_item = imported_pipeline.store.ensure_work_item(
                "skillnerds/xskill",
                kind="github_issue",
                external_id="7",
                title="Locally refreshed issue title",
                payload={"state": "open", "updated_at": "2026-01-03T00:00:00Z"},
            )
            imported = imported_pipeline.import_context(output)
            imported_again = imported_pipeline.import_context(output)
            restored_checkpoint = (
                imported_pipeline.store.latest_checkpoint_for_work_item(
                    imported["work_item_id"]
                )
            )
            restored_session = imported_pipeline.store.latest_harness_session(
                imported["work_item_id"], "fake-harness"
            )
            with closing(
                sqlite3.connect(imported_pipeline.store.path)
            ) as imported_connection:
                retained_work_item = imported_connection.execute(
                    "SELECT title, payload FROM work_items WHERE id=?",
                    (existing_work_item["id"],),
                ).fetchone()

        self.assertIsNotNone(harness.request)
        self.assertEqual(harness.request.context.task.title, "Fix the edge case")
        self.assertIsNotNone(harness.request.context.handoff)
        self.assertEqual(harness.request.context.handoff["status"], "ready")
        self.assertEqual(harness.request.native_session_id, "fake-thread")
        self.assertNotEqual(first_packet["run_id"], packet["run_id"])
        self.assertEqual(bundle["harness_run"]["harness"], "fake-harness")
        self.assertEqual(bundle["checkpoint"]["status"], "ready")
        self.assertEqual(bundle["checkpoint"]["next_action"], "human_review")
        self.assertEqual(exported["bundle_digest"], bundle["bundle_digest"])
        self.assertTrue(export_exists)
        self.assertTrue(imported["imported"])
        self.assertFalse(imported_again["imported"])
        self.assertEqual(imported["id"], imported_again["id"])
        self.assertEqual(imported["work_item_id"], existing_work_item["id"])
        self.assertEqual(retained_work_item[0], "Locally refreshed issue title")
        self.assertEqual(
            json.loads(retained_work_item[1])["updated_at"],
            "2026-01-03T00:00:00Z",
        )
        self.assertIsNotNone(restored_checkpoint)
        assert restored_checkpoint is not None
        self.assertEqual(restored_checkpoint["status"], "ready")
        self.assertEqual(restored_session, "fake-thread")


if __name__ == "__main__":
    unittest.main()
