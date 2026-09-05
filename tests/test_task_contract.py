from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_context import _candidate

from reposteward.agent import build_harness_prompt
from reposteward.config import RepositoryPolicy
from reposteward.context import build_context_pack
from reposteward.context_budget import ContextBudgetError, estimate_tokens
from reposteward.prompt_budget import fit_context
from reposteward.protocol import ProtocolValidationError, validate_context_pack
from reposteward.task_contract import digest, issue_digest, review_contract


class TaskContractTests(unittest.TestCase):
    def pack(self, body: str, **options):
        with tempfile.TemporaryDirectory() as directory:
            return build_context_pack(
                _candidate(body),
                RepositoryPolicy(name="owner/repo"),
                work_item_id="work",
                run_id="run",
                worktree=Path(directory),
                base_commit="a" * 40,
                harness="external",
                model="",
                **options,
            )

    def test_unreviewed_tail_requirement_survives_description_trimming(self) -> None:
        tail = "必须保持旧 API 与 existing clients 兼容。"
        pack = self.pack("background " * 2500 + tail, task_description_max_bytes=500)
        self.assertNotIn(tail, pack.task.description)
        fitted, stats = fit_context(pack, 40000)
        self.assertIn(tail, build_harness_prompt(fitted))
        self.assertEqual(fitted.task_contract.review_status, "source_bound")
        self.assertEqual(fitted.task_contract.reviewed_by, "")
        self.assertGreater(fitted.coverage[0]["omitted"], 0)
        self.assertEqual(
            stats["estimated_tokens"], estimate_tokens(build_harness_prompt(fitted))
        )
        validate_context_pack(fitted.to_dict())

    def test_mandatory_source_cannot_fit_fails_until_explicit_contract_review(
        self,
    ) -> None:
        body = "中英 mixed background. " * 10000
        pack = self.pack(body)
        with self.assertRaisesRegex(ContextBudgetError, "mandatory task contract"):
            fit_context(pack, 12000)
        issue = _candidate(body).issue
        proposal = {
            "source_digest": issue_digest(issue),
            "goal": issue.title,
            "acceptance_criteria": ["通过旧客户端兼容性测试"],
            "scope_boundaries": ["保持公开接口签名"],
        }
        contract = review_contract(issue, proposal, reviewed_by="operator")
        fitted, stats = fit_context(self.pack(body, task_contract=contract), 12000)
        self.assertLessEqual(stats["estimated_tokens"], 12000)
        self.assertEqual(
            fitted.task_contract.acceptance_criteria, ("通过旧客户端兼容性测试",)
        )
        self.assertEqual(fitted.task_contract.scope_boundaries, ("保持公开接口签名",))
        self.assertEqual(fitted.task_contract.source_requirements, "")
        validate_context_pack(fitted.to_dict())

    def test_changed_source_requires_fresh_review_and_explicit_supersession(
        self,
    ) -> None:
        issue = _candidate().issue
        proposal = {
            "source_digest": issue_digest(issue),
            "goal": issue.title,
            "acceptance_criteria": ["A"],
        }
        first = review_contract(issue, proposal, reviewed_by="operator")
        with self.assertRaisesRegex(ValueError, "source changed"):
            review_contract(
                replace(issue, body="new requirements"),
                proposal,
                reviewed_by="operator",
            )
        second = review_contract(
            issue,
            {**proposal, "acceptance_criteria": ["B"], "supersedes": first.digest},
            reviewed_by="operator",
        )
        self.assertEqual(second.supersedes, first.digest)
        self.assertNotEqual(second.digest, first.digest)
        self.assertEqual(first.acceptance_criteria, ("A",))

    def test_claimed_review_and_tampered_source_fail_validation(self) -> None:
        original = self.pack("retain exact requirement").to_dict()
        original["task_contract"]["source_requirements"] = "weakened"
        original["task_contract"]["digest"] = digest(
            {k: v for k, v in original["task_contract"].items() if k != "digest"}
        )
        with self.assertRaisesRegex(ProtocolValidationError, "original Issue"):
            validate_context_pack(original)
        issue = _candidate().issue
        with self.assertRaisesRegex(ValueError, "operator reviewer"):
            review_contract(issue, {}, reviewed_by="")
        proposal = {
            "source_digest": issue_digest(issue),
            "goal": "fix",
            "acceptance_criteria": ["A"],
            "reviewed_by": "model",
        }
        with self.assertRaisesRegex(ValueError, "unknown"):
            review_contract(issue, proposal, reviewed_by="operator")

    def test_v2_remains_strictly_readable_without_implied_contract(self) -> None:
        pack = self.pack("legacy body").to_dict()
        for field in ("task_contract", "repair_feedback", "coverage"):
            pack.pop(field)
        pack["schema_version"] = 2
        validate_context_pack(pack)
        pack["task_contract"] = {}
        with self.assertRaises(ProtocolValidationError):
            validate_context_pack(pack)

    def test_checkpoint_omissions_reference_complete_original_record(self) -> None:
        checkpoint = {
            "id": "checkpoint",
            "status": "running",
            "head_commit": "a" * 40,
            "remaining": ["unresolved " * 100] * 20,
            "decisions": [
                {"statement": "decision " * 500, "rationale": "why", "evidence": []}
            ]
            * 10,
        }
        pack = self.pack("fix", previous_checkpoint=checkpoint)
        references = [
            value for value in pack.coverage if value["field"].startswith("handoff.")
        ]
        self.assertGreater(len(references), 0)
        self.assertTrue(
            all(
                value["locator"] == "checkpoint"
                and value["digest"] == digest(checkpoint)
                for value in references
            )
        )
        source = next(
            value for value in pack.sources if value.kind == "reposteward_checkpoint"
        )
        self.assertEqual(source.digest, digest(checkpoint))


if __name__ == "__main__":
    unittest.main()
