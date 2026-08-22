from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reposteward.config import RepositoryPolicy
from reposteward.context import (
    build_context_pack,
    failed_checkpoint,
    portable_bundle,
    ready_checkpoint,
    running_checkpoint,
)
from reposteward.models import (
    AgentDecision,
    AgentResult,
    Candidate,
    Issue,
    RepositoryInfo,
    VerificationResult,
)
from reposteward.protocol import (
    ProtocolValidationError,
    read_context_bundle,
    schema_document,
    validate_checkpoint,
    validate_context_bundle,
    validate_context_pack,
)


def _candidate() -> Candidate:
    return Candidate(
        issue=Issue(
            repository="owner/repo",
            number=7,
            node_id=8,
            title="Fix the edge case",
            body="Reproduce the bug",
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


def _pack(root: Path):
    return build_context_pack(
        _candidate(),
        RepositoryPolicy(name="owner/repo", verification_prefixes=("pytest ",)),
        work_item_id="work-1",
        run_id="run-1",
        worktree=root,
        base_commit="a" * 40,
        harness="codex-cli",
        model="gpt-example",
    )


class ProtocolSchemaTests(unittest.TestCase):
    def test_packaged_schemas_are_draft_2020_12_documents(self) -> None:
        expected_versions = {
            "context-pack": 2,
            "checkpoint": 1,
            "context-bundle": 2,
        }
        for name, version in expected_versions.items():
            schema = schema_document(name)
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertTrue(str(schema["$id"]).startswith("urn:reposteward:schema:"))
            self.assertTrue(str(schema["$id"]).endswith(f":{version}"))
        self.assertTrue(schema_document("context-pack", 1)["$id"].endswith(":1"))

    def test_generated_context_and_every_checkpoint_state_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pack = _pack(Path(directory))
        result = AgentResult(
            summary="Fixed the edge case.",
            pr_title="fix(repo): handle edge case",
            implementation_notes="Changed one branch.",
            verification_commands=("pytest tests/test_edge.py",),
            tests_observed=("pytest tests/test_edge.py",),
            risks=("Review the fallback.",),
            decisions=(
                AgentDecision(
                    statement="Keep the public behavior.",
                    rationale="The issue is limited to an edge case.",
                    evidence=("tests/test_edge.py",),
                ),
            ),
        )
        validate_context_pack(pack.to_dict())
        validate_checkpoint(
            running_checkpoint(
                pack,
                head_commit="a" * 40,
                completed=("Cloned repository.",),
                next_action="run_harness",
            )
        )
        validate_checkpoint(
            ready_checkpoint(
                pack,
                head_commit="b" * 40,
                result=result,
                verification=VerificationResult(True, ()),
                changed_files=("src/example.py",),
            )
        )
        validate_checkpoint(
            failed_checkpoint(pack, error="verification failed", head_commit="b" * 40)
        )

    def test_schema_rejects_unknown_fields_and_future_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = _pack(Path(directory)).to_dict()
        payload["unexpected"] = True
        with self.assertRaisesRegex(ProtocolValidationError, "unexpected"):
            validate_context_pack(payload)
        payload.pop("unexpected")
        payload["schema_version"] = 3
        with self.assertRaisesRegex(ProtocolValidationError, "schema_version"):
            validate_context_pack(payload)

    def test_v1_context_pack_and_bundle_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = _pack(Path(directory))
        legacy_pack = generated.to_dict()
        legacy_pack.pop("skill_catalog")
        legacy_pack["schema_version"] = 1
        validate_context_pack(legacy_pack)
        checkpoint = running_checkpoint(
            generated,
            head_commit="a" * 40,
            completed=("Cloned repository.",),
            next_action="run_harness",
        )
        raw = {
            "work_item": {
                "id": "work-1",
                "repository": "owner/repo",
                "kind": "github_issue",
                "external_id": "7",
                "title": "Fix the edge case",
                "status": "active",
                "payload": {},
            },
            "harness_run": {
                "run_id": "run-1",
                "harness": "codex-cli",
                "model": "gpt-example",
                "native_session_id": "",
                "created_at": "2026-01-02T00:00:00Z",
            },
            "context_metadata": {
                "id": generated.id,
                "schema_version": 1,
                "source_digest": generated.source_digest,
                "base_commit": "a" * 40,
                "created_at": "2026-01-02T00:00:00Z",
            },
            "context_pack": legacy_pack,
            "checkpoint": checkpoint,
        }
        bundle = portable_bundle(raw)
        self.assertEqual(bundle["bundle_schema_version"], 1)
        validate_context_bundle(bundle, require_checkpoint=True)

    def test_skill_catalog_digest_is_part_of_the_v2_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = _pack(Path(directory)).to_dict()
        payload["skill_catalog"]["digest"] = "0" * 64
        with self.assertRaisesRegex(ProtocolValidationError, "catalog digest"):
            validate_context_pack(payload)

    def test_bundle_validation_checks_digest_and_cross_document_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pack = _pack(Path(directory))
        checkpoint = running_checkpoint(
            pack,
            head_commit="a" * 40,
            completed=("Cloned repository.",),
            next_action="run_harness",
        )
        raw = {
            "work_item": {
                "id": "work-1",
                "repository": "owner/repo",
                "kind": "github_issue",
                "external_id": "7",
                "title": "Fix the edge case",
                "status": "active",
                "payload": {},
            },
            "harness_run": {
                "run_id": "run-1",
                "harness": "codex-cli",
                "model": "gpt-example",
                "native_session_id": "",
                "created_at": "2026-01-02T00:00:00Z",
            },
            "context_metadata": {
                "id": pack.id,
                "schema_version": pack.schema_version,
                "source_digest": pack.source_digest,
                "base_commit": "a" * 40,
                "created_at": "2026-01-02T00:00:00Z",
            },
            "context_pack": pack.to_dict(),
            "checkpoint": checkpoint,
        }
        bundle = portable_bundle(raw)
        validate_context_bundle(bundle, require_checkpoint=True)

        corrupted = replace_bundle(bundle, bundle_digest="0" * 64)
        with self.assertRaisesRegex(ProtocolValidationError, "digest"):
            validate_context_bundle(corrupted)

        inconsistent_raw = {**raw, "work_item": {**raw["work_item"], "id": "other"}}
        inconsistent = portable_bundle(inconsistent_raw)
        with self.assertRaisesRegex(ProtocolValidationError, "work item"):
            validate_context_bundle(inconsistent)

        changed_pack = pack.to_dict()
        changed_pack["sources"][0]["locator"] = "https://example.invalid/changed"
        stale_source_digest = portable_bundle({**raw, "context_pack": changed_pack})
        with self.assertRaisesRegex(ProtocolValidationError, "source digest"):
            validate_context_bundle(stale_source_digest)

    def test_reader_rejects_oversized_or_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.json"
            invalid.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ProtocolValidationError, "cannot read"):
                read_context_bundle(invalid)


def replace_bundle(bundle: dict, **updates) -> dict:
    return json.loads(json.dumps({**bundle, **updates}))


if __name__ == "__main__":
    unittest.main()
