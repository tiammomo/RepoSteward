from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
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
    CommandResult,
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
from reposteward.store import Store


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
            "context-pack": 3,
            "checkpoint": 1,
            "context-bundle": 3,
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
        payload["schema_version"] = 99
        with self.assertRaisesRegex(ProtocolValidationError, "schema_version"):
            validate_context_pack(payload)

    def test_v1_context_pack_and_bundle_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = _pack(Path(directory))
        legacy_pack = generated.to_dict()
        for field in ("skill_catalog", "task_contract", "repair_feedback", "coverage"):
            legacy_pack.pop(field)
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


class CheckpointBoundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pack = _pack(self.root)
        self.result = AgentResult("Prepared", "refactor: organize", "Moved modules", ())

    def ready(self, paths, commands=()):
        return ready_checkpoint(
            self.pack,
            head_commit="b" * 40,
            result=self.result,
            verification=VerificationResult(True, commands),
            changed_files=paths,
        )

    def test_small_and_boundary_checkpoints_are_unchanged(self):
        for count in (0, 1, 127):
            paths = tuple(f"file-{n}.py" for n in range(count))
            value = self.ready(paths)
            validate_checkpoint(value)
            self.assertEqual(len(value["evidence"]), count + 1)
            self.assertEqual(tuple(e["locator"] for e in value["evidence"][1:]), paths)

    def test_large_change_keeps_commit_commands_and_verifiable_manifest(self):
        paths = tuple(f"file-{n}.py" for n in range(149))
        command = CommandResult("test", 0, "", 1.0, output_sha256="a" * 64)
        value = self.ready(paths, (command,))
        validate_checkpoint(value)
        evidence = value["evidence"]
        self.assertEqual(len(evidence), 128)
        self.assertEqual([e["kind"] for e in evidence[:2]], ["commit", "verification"])
        manifest = evidence[-1]
        facts = json.loads(manifest["summary"])
        self.assertEqual((facts["total"], facts["omitted"]), (151, 24))
        commit = self.ready(())["evidence"][0]
        receipt = self.ready((), (command,))["evidence"][-1]
        complete = [
            commit,
            *[
                {
                    "kind": "changed_file",
                    "locator": p,
                    "status": "changed",
                    "digest": "",
                    "summary": "",
                }
                for p in paths
            ],
            receipt,
        ]
        digest = hashlib.sha256(
            json.dumps(
                complete, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.assertEqual(manifest["digest"], digest)
        self.assertEqual(value, self.ready(paths, (command,)))
        changed = self.ready((*paths[:-1], "changed.py"), (command,))
        self.assertNotEqual(changed["evidence"][-1]["digest"], digest)

    def test_many_failed_commands_stay_failed_in_both_checkpoint_states(self):
        commands = tuple(CommandResult(str(n), 1, "", 1.0) for n in range(150))
        failed = failed_checkpoint(
            self.pack,
            error="verification failed",
            details={"verification": asdict(VerificationResult(False, commands))},
        )
        ready = ready_checkpoint(
            self.pack,
            head_commit="b" * 40,
            result=self.result,
            verification=VerificationResult(False, commands),
            changed_files=("a.py",),
        )
        for value in (failed, ready):
            validate_checkpoint(value)
            self.assertEqual(len(value["evidence"]), 128)
            manifest = value["evidence"][-1]
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(
                json.loads(manifest["summary"])["failed_verifications"], 150
            )
        self.assertEqual(ready["evidence"][0]["status"], "unverified")

    def test_large_checkpoint_persists_with_full_run_details(self):
        store = Store(self.root / "state.sqlite3")
        work = store.ensure_work_item(
            "owner/repo",
            kind="github_issue",
            external_id="7",
            title="Example",
            payload={},
        )
        run = store.start_run("owner/repo", 7, "agent")
        pack = build_context_pack(
            _candidate(),
            RepositoryPolicy(name="owner/repo"),
            work_item_id=work["id"],
            run_id=run,
            worktree=self.root,
            base_commit="a" * 40,
            harness="external-workspace",
            model="",
        )
        store.save_context_run(
            pack_id=pack.id,
            work_item_id=work["id"],
            run_id=run,
            schema_version=pack.schema_version,
            source_digest=pack.source_digest,
            base_commit="a" * 40,
            payload=pack.to_dict(),
            harness="external-workspace",
        )
        self.pack = pack
        paths = tuple(f"file-{n}.py" for n in range(149))
        value = self.ready(paths)
        store.save_checkpoint(
            work_item_id=work["id"],
            run_id=run,
            context_pack_id=pack.id,
            status="ready",
            payload=value,
        )
        store.update_run(
            run, status="ready", stage="review", details={"changed_files": paths}
        )
        with store._connection() as db:
            saved = json.loads(
                db.execute(
                    "SELECT payload FROM checkpoints WHERE run_id=?", (run,)
                ).fetchone()[0]
            )
            details = json.loads(
                db.execute("SELECT details FROM runs WHERE id=?", (run,)).fetchone()[0]
            )
        self.assertEqual(len(saved["evidence"]), 128)
        self.assertEqual(details["changed_files"], list(paths))


if __name__ == "__main__":
    unittest.main()
