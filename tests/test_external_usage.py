from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import test_external_tasks
from test_projects import repository

from reposteward.cli import main
from reposteward.codex_usage import UsageSourceError, read_codex_turns
from reposteward.config import UsagePrice
from reposteward.external_usage import ExternalUsage


def record(kind: str, **payload) -> dict:
    return {"timestamp": "2026-09-07T02:00:00Z", "type": kind, "payload": payload}


def counts(inputs=100, cached=50, outputs=10, reasoning=2) -> dict:
    return {
        "input_tokens": inputs,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": outputs,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inputs + outputs,
    }


def update(**kwargs) -> dict:
    return record(
        "event_msg", type="token_count", info={"total_token_usage": counts(**kwargs)}
    )


def context(cwd: Path, turn="chosen", model="model-a") -> dict:
    return record("turn_context", turn_id=turn, cwd=str(cwd), model=model)


def rollout(cwd: Path) -> list[dict]:
    return [
        record(
            "session_meta",
            id="session-1",
            cli_version="0.153.0",
            cwd=str(cwd),
            instructions="SENSITIVE_MARKER",
        ),
        context(cwd, "earlier"),
        update(),
        record(
            "event_msg",
            type="task_complete",
            turn_id="earlier",
            last_agent_message="SENSITIVE_MARKER",
        ),
        record("event_msg", type="task_started", turn_id="chosen"),
        context(cwd),
        record("response_item", type="message", content="SENSITIVE_MARKER"),
        update(inputs=160, cached=80, outputs=16, reasoning=4),
    ]


def write(path: Path, records: list[dict], *, append=False) -> None:
    with path.open("a" if append else "w") as stream:
        for row in records:
            stream.write(json.dumps(row) + "\n")


class CodexUsageParserTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "selected.jsonl"

    def parse(self, records=None):
        write(self.path, records if records is not None else rollout(self.root))
        return read_codex_turns(self.path, {"chosen"})["turns"]["chosen"]

    def test_cumulative_delta_duplicates_compaction_and_completion(self):
        rows = rollout(self.root)
        rows += [
            rows[-1],
            context(self.root),
            rows[-1],
            record("event_msg", type="task_complete", turn_id="chosen"),
        ]
        result = self.parse(rows)
        self.assertEqual(result["metrics"], counts(60, 30, 6, 2))
        self.assertEqual(result["completion"], "completed")
        self.assertEqual(result["unknown_reasons"], [])
        self.assertNotIn("SENSITIVE_MARKER", json.dumps(result))

    def test_missing_baseline_and_missing_fields_remain_unknown(self):
        rows = rollout(self.root)
        result = self.parse([rows[0], *rows[4:]])
        self.assertTrue(all(v is None for v in result["metrics"].values()))
        self.assertIn("baseline_unavailable", result["unknown_reasons"])
        del rows[-1]["payload"]["info"]["total_token_usage"]["cached_input_tokens"]
        result = self.parse(rows)
        self.assertIsNone(result["metrics"]["cached_input_tokens"])
        self.assertEqual(result["metrics"]["input_tokens"], 60)

    def test_resume_metadata_preserves_baseline_and_rejects_identity_changes(self):
        rows = rollout(self.root)
        result = self.parse(rows + [rows[0], context(self.root), rows[-1]])
        self.assertEqual(result["metrics"], counts(60, 30, 6, 2))
        changed = record("session_meta", id="another-session", cli_version="0.153.0")
        with self.assertRaisesRegex(UsageSourceError, "identity or version changed"):
            self.parse(rows + [changed])

    def test_missing_update_cannot_hide_an_intermediate_reset(self):
        missing = record(
            "event_msg", type="token_count", info={"total_token_usage": {}}
        )
        with self.assertRaisesRegex(UsageSourceError, "reset"):
            self.parse(rollout(self.root) + [missing, update(inputs=120, cached=60)])

    def test_no_usage_rate_limit_only_and_model_switch(self):
        rows = rollout(self.root)[:-1]
        rows += [record("event_msg", type="token_count", info=None)]
        result = self.parse(rows)
        self.assertIn("usage_unavailable", result["unknown_reasons"])
        result = self.parse(rollout(self.root) + [context(self.root, model="model-b")])
        self.assertIsNone(result["model"])
        self.assertIn("model_ambiguous", result["unknown_reasons"])

    def test_multiple_turns_exclude_unselected_usage(self):
        rows = rollout(self.root) + [
            context(self.root, "unrelated"),
            update(inputs=200, cached=100, outputs=20, reasoning=6),
            context(self.root, "next"),
            update(inputs=250, cached=120, outputs=30, reasoning=8),
        ]
        write(self.path, rows)
        turns = read_codex_turns(self.path, {"chosen", "next"})["turns"]
        self.assertEqual(turns["chosen"]["metrics"]["input_tokens"], 60)
        self.assertEqual(turns["next"]["metrics"]["input_tokens"], 50)

    def test_intermediate_reset_and_invalid_subsets_rejected(self):
        for bad in [
            update(inputs=120, cached=60),
            update(inputs=161, cached=150, outputs=16, reasoning=4),
            update(inputs=True),
            update(reasoning=20),
        ]:
            with self.subTest(bad=bad), self.assertRaises(UsageSourceError):
                self.parse(
                    rollout(self.root)
                    + [bad, update(inputs=500, cached=300, outputs=30, reasoning=10)]
                )

    def test_versions_envelopes_order_and_sensitive_errors(self):
        base = rollout(self.root)
        variants = [
            [
                {**base[0], "payload": {**base[0]["payload"], "cli_version": "99.0.0"}},
                *base[1:],
            ],
            [
                {**base[0], "payload": {**base[0]["payload"], "cli_version": []}},
                *base[1:],
            ],
            base + [context(self.root, "earlier")],
            base
            + [
                record(
                    "event_msg", type="token_count", info={"SENSITIVE_MARKER": "secret"}
                )
            ],
            base + [record("event_msg", type=[])],
            base
            + [
                record("event_msg", type="task_complete", turn_id="not-present"),
                context(self.root, "next"),
                record("event_msg", type="task_complete", turn_id="chosen"),
            ],
        ]
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(UsageSourceError) as caught:
                self.parse(rows)
            self.assertNotIn("SENSITIVE_MARKER", str(caught.exception))

    def test_partial_tail_growing_source_and_prefix_rewrite(self):
        write(self.path, rollout(self.root))
        with self.path.open("ab") as stream:
            stream.write(b'{"type":')
        first = read_codex_turns(self.path, {"chosen"})
        self.assertTrue(first["partial_tail"])
        prefix = (first["prefix_bytes"], first["prefix_sha256"])
        with self.path.open("ab") as stream:
            stream.write(b'"response_item","payload":{}}\n')
        write(
            self.path,
            [update(inputs=180, cached=90, outputs=18, reasoning=4)],
            append=True,
        )
        second = read_codex_turns(self.path, {"chosen"}, previous_prefix=prefix)
        self.assertEqual(second["turns"]["chosen"]["metrics"]["input_tokens"], 80)
        self.path.write_bytes(
            self.path.read_bytes().replace(b"SENSITIVE_MARKER", b"MODIFIED_MARKER")
        )
        with self.assertRaisesRegex(UsageSourceError, "rewritten|prefix changed"):
            read_codex_turns(self.path, {"chosen"}, previous_prefix=prefix)

    def test_malformed_complete_line_limits_and_truncation(self):
        write(self.path, rollout(self.root))
        result = read_codex_turns(self.path, {"chosen"})
        prefix = (result["prefix_bytes"], result["prefix_sha256"])
        with (
            patch("reposteward.codex_usage.MAX_SOURCE_BYTES", 10),
            self.assertRaisesRegex(UsageSourceError, "bounded regular"),
        ):
            read_codex_turns(self.path, {"chosen"})
        with (
            patch("reposteward.codex_usage.MAX_LINE_BYTES", 10),
            self.assertRaisesRegex(UsageSourceError, "line exceeds"),
        ):
            read_codex_turns(self.path, {"chosen"})
        with (
            patch("reposteward.codex_usage.MAX_RECORDS", 1),
            self.assertRaisesRegex(UsageSourceError, "record limit"),
        ):
            read_codex_turns(self.path, {"chosen"})
        self.path.write_bytes(b'{"SENSITIVE_MARKER":\n')
        with self.assertRaisesRegex(UsageSourceError, "truncated"):
            read_codex_turns(self.path, {"chosen"}, previous_prefix=prefix)
        with self.assertRaisesRegex(UsageSourceError, "invalid complete"):
            read_codex_turns(self.path, {"chosen"})


class ExternalUsageTests(unittest.TestCase):
    def setUp(self):
        test_external_tasks.ExternalTaskTests.setUp(self)
        self.tasks = self.service
        self.service = ExternalUsage(self.config)
        self.run = self.tasks.start(self.repo, issue_number=7, reviewed_by="owner")[
            "run_id"
        ]
        with closing(sqlite3.connect(self.tasks.path)) as db:
            self.core_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.source = self.root / "selected.jsonl"
        write(self.source, rollout(self.repo))

    def collect(self, run=None, turns=("chosen",)):
        return self.service.collect(
            run or self.run, codex_session=self.source, turn_ids=turns
        )

    def report(self):
        return self.service.report("owner/repo", include_turns=True)

    def test_repeat_refresh_successor_run_and_adopted_run_do_not_double_count(self):
        with patch(
            "reposteward.external_usage.utc_now",
            return_value="2026-09-07T02:00:00+00:00",
        ):
            self.assertEqual(self.collect()["sources"][0]["updated_turns"], 1)
        with patch(
            "reposteward.external_usage.utc_now",
            return_value="2026-09-07T03:00:00+00:00",
        ):
            self.assertTrue(self.collect()["sources"][0]["idempotent"])
        self.assertEqual(
            self.report()["summary"]["last_collected_at"], "2026-09-07T03:00:00+00:00"
        )
        successor = self.tasks.start(self.repo, issue_number=7, reviewed_by="owner")[
            "run_id"
        ]
        with closing(sqlite3.connect(self.tasks.path)) as db:
            db.execute(
                "UPDATE harness_runs SET harness='external-workspace' WHERE run_id=?",
                (successor,),
            )
            db.commit()
        self.assertTrue(self.service.collect(successor)["sources"][0]["idempotent"])
        write(
            self.source,
            [
                update(inputs=180, cached=90, outputs=18, reasoning=4),
                record("event_msg", type="task_complete", turn_id="chosen"),
            ],
            append=True,
        )
        self.service.collect(successor)
        report = self.report()
        self.assertEqual(report["summary"]["turns"], 1)
        self.assertEqual(report["summary"]["metrics"]["input_tokens"]["value"], 80)
        self.assertEqual(report["turns"][0]["first_run_id"], self.run)
        self.assertEqual(report["turns"][0]["completion"], "completed")
        with closing(sqlite3.connect(self.service.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM observations").fetchone()[0], 2
            )

    def test_no_messages_persist_and_core_schema_unchanged(self):
        self.collect()
        self.assertNotIn(b"SENSITIVE_MARKER", self.service.path.read_bytes())
        self.assertNotIn("SENSITIVE_MARKER", json.dumps(self.report()))
        self.assertNotIn(str(self.source), json.dumps(self.report()))
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(self.tasks.path)) as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], self.core_version
            )

    def test_rewrite_and_truncation_keep_previous_observation(self):
        self.collect()
        before = self.report()
        original = self.source.read_bytes()
        for value in (
            original[:50],
            original.replace(b"SENSITIVE_MARKER", b"MODIFIED_MARKER"),
        ):
            self.source.write_bytes(value)
            with self.assertRaises(UsageSourceError):
                self.collect()
            self.assertEqual(self.report(), before)

    def test_cross_project_and_cross_work_item_conflicts_are_atomic(self):
        self.collect()
        before = self.report()
        self.github.issue.return_value = replace(
            self.github.issue.return_value, number=8
        )
        other_run = self.tasks.start(self.repo, issue_number=8, reviewed_by="owner")[
            "run_id"
        ]
        with self.assertRaisesRegex(UsageSourceError, "another WorkItem"):
            self.collect(other_run)
        other = repository(
            self.root / "another", remote="git@github.com:owner/another.git"
        )
        write(
            self.source,
            [
                context(other, "next"),
                update(inputs=200, cached=100, outputs=20, reasoning=4),
            ],
            append=True,
        )
        with self.assertRaisesRegex(UsageSourceError, "another project"):
            self.collect(turns=("chosen", "next"))
        self.assertEqual(self.report(), before)

    def test_separate_clone_of_same_repository_allowed(self):
        clone = repository(
            self.root / "clone", remote="https://github.com/owner/repo.git"
        )
        write(self.source, rollout(clone))
        self.collect()
        self.assertEqual(self.report()["summary"]["turns"], 1)

    def test_concurrent_collection_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.collect(), range(2)))
        self.assertEqual(sum(r["sources"][0]["updated_turns"] for r in results), 1)
        self.assertEqual(self.report()["summary"]["turns"], 1)

    def test_read_only_cli_without_pipeline_authentication_or_db_creation(self):
        for command in (["usage", "external-report", "owner/repo"],):
            output = io.StringIO()
            with (
                patch("reposteward.cli.load_config", return_value=self.config),
                patch(
                    "reposteward.cli.Pipeline",
                    side_effect=AssertionError("must remain local"),
                ),
                patch(
                    "reposteward.github.GitHubClient",
                    side_effect=AssertionError("no auth"),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(main(command), 0)
            self.assertEqual(json.loads(output.getvalue())["summary"]["turns"], 0)
        self.assertFalse(self.service.path.exists())
        output = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            patch(
                "reposteward.cli.Pipeline",
                side_effect=AssertionError("must remain local"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "usage",
                        "collect",
                        self.run,
                        "--codex-session",
                        str(self.source),
                        "--turn-id",
                        "chosen",
                    ]
                ),
                0,
            )
        self.assertEqual(
            json.loads(output.getvalue())["sources"][0]["updated_turns"], 1
        )

    def test_missing_selection_managed_run_and_unknown_schema_rejected(self):
        with self.assertRaisesRegex(UsageSourceError, "no selected sources"):
            self.service.collect(self.run)
        with self.assertRaisesRegex(UsageSourceError, "provide both"):
            self.service.collect(self.run, turn_ids=("chosen",))
        with self.assertRaisesRegex(UsageSourceError, "registered WorkItem"):
            self.collect("absent")
        with closing(sqlite3.connect(self.tasks.path)) as db:
            db.execute(
                "UPDATE harness_runs SET harness='codex-sdk' WHERE run_id=?",
                (self.run,),
            )
            db.commit()
        with self.assertRaisesRegex(UsageSourceError, "external task"):
            self.collect()
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(UsageSourceError, "unsupported external usage"):
            self.report()

    def test_multiple_turns_groups_unknowns_and_configured_cost(self):
        write(
            self.source,
            [
                context(self.repo, "next", model="model-b"),
                update(inputs=200, cached=100, outputs=20, reasoning=6),
            ],
            append=True,
        )
        self.collect(turns=("chosen", "next"))
        report = self.service.report("owner/repo", group_by="model", include_turns=True)
        self.assertEqual([g["key"] for g in report["groups"]], ["model-a", "model-b"])
        self.assertEqual(report["summary"]["metrics"]["input_tokens"]["value"], 100)
        self.assertEqual(report["summary"]["cost_estimate"]["unknown_turns"], 2)
        self.assertIsNone(report["summary"]["tool_call_count"])
        self.assertEqual(
            self.service.report("owner/repo", issue=8)["summary"]["turns"], 0
        )
        price = UsagePrice(
            harness="codex-local",
            model="model-a",
            effective_from="2026-01-01",
            currency="USD",
            input_per_million=Decimal(2),
            cached_input_per_million=Decimal(1),
            output_per_million=Decimal(4),
        )
        config = replace(
            self.config,
            observability=replace(self.config.observability, prices=(price,)),
        )
        cost = ExternalUsage(config).report("owner/repo")["summary"]["cost_estimate"]
        self.assertEqual(Decimal(cost["by_currency"]["USD"]), Decimal("0.000114"))
        self.assertEqual(cost["unknown_turns"], 1)

    def test_unknown_version_cli_error_does_not_echo_source_body(self):
        rows = rollout(self.repo)
        rows[0]["payload"]["cli_version"] = "SENSITIVE_MARKER"
        write(self.source, rows)
        errors = io.StringIO()
        with (
            patch("reposteward.cli.load_config", return_value=self.config),
            redirect_stderr(errors),
        ):
            self.assertEqual(
                main(
                    [
                        "usage",
                        "collect",
                        self.run,
                        "--codex-session",
                        str(self.source),
                        "--turn-id",
                        "chosen",
                    ]
                ),
                2,
            )
        self.assertNotIn("SENSITIVE_MARKER", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
