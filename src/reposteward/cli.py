from __future__ import annotations

import argparse
import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .discovery import DiscoveryService
from .doctor import run_doctor
from .issues import read_details
from .pipeline import Pipeline
from .setup import add_repository, initialize_user_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reposteward",
        description="A local-first, policy-gated Issue-to-PR workbench.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="project TOML config (default: discover and layer over user config)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="create per-user configuration")
    initialize.add_argument("--path", type=Path, default=None)
    initialize.add_argument("--login", default="")
    initialize.add_argument("--git-name", default="")
    initialize.add_argument("--git-email", default="")
    initialize.add_argument("--issue-project-owner", default="")
    initialize.add_argument("--issue-project-number", type=int, default=0)
    initialize.add_argument(
        "--issue-project-owner-type",
        choices=("user", "organization"),
        default="user",
    )
    initialize.add_argument("--force", action="store_true")

    repo = subparsers.add_parser("repo", help="manage project repositories")
    repo_commands = repo.add_subparsers(dest="repo_command", required=True)
    repo_add = repo_commands.add_parser("add", help="add a repository policy skeleton")
    repo_add.add_argument("repository")
    repo_add.add_argument("--path", type=Path, default=None)
    repo_add.add_argument(
        "--mode", choices=("contributor", "maintainer"), default="contributor"
    )

    issue = subparsers.add_parser("issue", help="prepare local issue drafts")
    issue_commands = issue.add_subparsers(dest="issue_command", required=True)
    issue_draft = issue_commands.add_parser(
        "draft", help="create a structured local Markdown draft"
    )
    issue_draft.add_argument("repository")
    issue_draft.add_argument("--title", required=True)
    issue_draft.add_argument("--summary", required=True)
    issue_draft.add_argument("--actual", required=True)
    issue_draft.add_argument("--expected", required=True)
    issue_draft.add_argument("--reproduction", default="")
    issue_draft.add_argument("--environment", default="")
    issue_draft.add_argument("--acceptance", action="append", default=[])
    issue_draft.add_argument("--details-file", type=Path, default=None)
    issue_draft.add_argument("--language", choices=("en", "zh"), default="en")
    issue_list = issue_commands.add_parser("list", help="list local issue drafts")
    issue_list.add_argument("--limit", type=int, default=30)
    issue_inspect = issue_commands.add_parser(
        "inspect", help="print one local issue draft"
    )
    issue_inspect.add_argument("draft_id")
    issue_duplicates = issue_commands.add_parser(
        "duplicate-check", help="search GitHub for potentially similar issues"
    )
    issue_duplicates.add_argument("draft_id")
    issue_stage = issue_commands.add_parser(
        "stage", help="stage a local draft in the configured GitHub Project"
    )
    issue_stage.add_argument("draft_id")
    issue_stage.add_argument("--submitted-by", required=True)
    issue_review = issue_commands.add_parser(
        "review", help="review the latest online proposal and duplicate snapshot"
    )
    issue_review.add_argument(
        "project_item_id", help="GraphQL node ID, numeric itemId, or Project item URL"
    )
    issue_review.add_argument("--repository", required=True)
    issue_promote = issue_commands.add_parser(
        "promote", help="convert an approved Project draft into a repository Issue"
    )
    issue_promote.add_argument(
        "project_item_id", help="GraphQL node ID, numeric itemId, or Project item URL"
    )
    issue_promote.add_argument("--repository", required=True)
    issue_promote.add_argument("--reviewed-by", required=True)
    issue_promote.add_argument("--review-digest", required=True)
    issue_promote.add_argument(
        "--duplicates-reviewed", action="store_true", required=True
    )

    subparsers.add_parser("doctor", help="check local tools and authentication")
    image = subparsers.add_parser("image", help="manage the isolated verifier image")
    image.add_argument("action", choices=("build",))

    discover = subparsers.add_parser("discover", help="refresh issue candidates")
    discover.add_argument("--repo", default="", help="limit to owner/name")

    listing = subparsers.add_parser("list", help="list ranked issue candidates")
    listing.add_argument(
        "--all", action="store_true", help="include blocked candidates"
    )
    listing.add_argument("--status", default="candidate")
    listing.add_argument("--limit", type=int, default=30)

    gate = subparsers.add_parser("gate", help="check contribution gates for one issue")
    gate.add_argument("repository")
    gate.add_argument("issue", type=int)

    prepare = subparsers.add_parser(
        "prepare", help="solve and verify one issue locally"
    )
    prepare.add_argument("repository")
    prepare.add_argument("issue", type=int)

    adopt = subparsers.add_parser(
        "adopt", help="verify and register an existing local commit"
    )
    adopt.add_argument("repository")
    adopt.add_argument("issue", type=int)
    adopt.add_argument("--worktree", type=Path, required=True)
    adopt.add_argument("--summary", required=True)
    adopt.add_argument("--notes", required=True)
    adopt.add_argument("--verify", action="append", required=True)

    inspect = subparsers.add_parser(
        "inspect", help="print a compact, reusable review packet for one run"
    )
    inspect.add_argument("run_id")

    context = subparsers.add_parser(
        "context", help="inspect, export, or import portable task context"
    )
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_inspect = context_commands.add_parser(
        "inspect", help="print the context pack and latest checkpoint"
    )
    context_inspect.add_argument("run_id")
    context_export = context_commands.add_parser(
        "export", help="write a portable context bundle atomically"
    )
    context_export.add_argument("run_id")
    context_export.add_argument("--output", type=Path, required=True)
    context_import = context_commands.add_parser(
        "import", help="validate and import a portable context bundle"
    )
    context_import.add_argument("source", type=Path)

    logs = subparsers.add_parser(
        "logs", help="list verification logs or print one bounded log tail"
    )
    logs.add_argument("run_id")
    logs.add_argument(
        "--command",
        type=int,
        default=None,
        help="one-based verification command number",
    )
    logs.add_argument("--tail-chars", type=int, default=12000)

    follow_up = subparsers.add_parser(
        "follow-up",
        help="fetch only pull request activity changed since the last check",
    )
    follow_up.add_argument("run_id")

    submit = subparsers.add_parser(
        "submit", help="push a reviewed change and create or reopen a PR"
    )
    submit.add_argument("repository")
    submit.add_argument("issue", type=int)
    submit.add_argument("--reviewed-by", required=True)
    submit.add_argument(
        "--reopen",
        type=int,
        default=0,
        help="update and reopen a closed PR instead of creating a new one",
    )

    run = subparsers.add_parser(
        "run", help="discover and prepare top auto-enabled issues"
    )
    run.add_argument("--limit", type=int, default=1)
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _runner_dockerfile() -> Path:
    bundled = importlib.resources.files("reposteward").joinpath(
        "data", "Dockerfile.runner"
    )
    if bundled.is_file():
        return Path(str(bundled))
    source = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.runner"
    if source.is_file():
        return source
    raise ConfigError("runner Dockerfile is missing from this installation")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            _json(
                initialize_user_config(
                    path=args.path,
                    login=args.login,
                    git_name=args.git_name,
                    git_email=args.git_email,
                    issue_project_owner=args.issue_project_owner,
                    issue_project_number=args.issue_project_number,
                    issue_project_owner_type=args.issue_project_owner_type,
                    force=args.force,
                )
            )
            return 0
        if args.command == "repo":
            if args.repo_command == "add":
                _json(
                    add_repository(
                        args.repository,
                        path=args.path,
                        mode=args.mode,
                    )
                )
                return 0
            raise AssertionError(f"unhandled repo command: {args.repo_command}")
        config = load_config(args.config, include_user=True)
        pipeline = Pipeline(config)
        if args.command == "issue":
            if args.issue_command == "draft":
                _json(
                    pipeline.create_issue_draft(
                        args.repository,
                        title=args.title,
                        summary=args.summary,
                        actual=args.actual,
                        expected=args.expected,
                        reproduction=args.reproduction,
                        environment=args.environment,
                        acceptance=tuple(args.acceptance),
                        details=read_details(args.details_file),
                        language=args.language,
                    )
                )
                return 0
            if args.issue_command == "list":
                _json(pipeline.issue_drafts(limit=args.limit))
                return 0
            if args.issue_command == "inspect":
                _json(pipeline.issue_draft(args.draft_id))
                return 0
            if args.issue_command == "duplicate-check":
                _json(pipeline.issue_duplicate_check(args.draft_id))
                return 0
            if args.issue_command == "stage":
                _json(
                    pipeline.stage_issue_proposal(
                        args.draft_id, submitted_by=args.submitted_by
                    )
                )
                return 0
            if args.issue_command == "review":
                _json(
                    pipeline.issue_proposal_review(
                        args.project_item_id,
                        repository=args.repository,
                    )
                )
                return 0
            if args.issue_command == "promote":
                _json(
                    pipeline.promote_issue_proposal(
                        args.project_item_id,
                        repository=args.repository,
                        reviewed_by=args.reviewed_by,
                        review_digest=args.review_digest,
                        duplicates_reviewed=args.duplicates_reviewed,
                    )
                )
                return 0
            raise AssertionError(f"unhandled issue command: {args.issue_command}")
        if args.command == "doctor":
            report, ok = run_doctor(config)
            _json(report)
            return 0 if ok else 1
        if args.command == "image":
            dockerfile = _runner_dockerfile()
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    config.runner.image,
                    "-f",
                    str(dockerfile),
                    str(dockerfile.parent),
                ],
                check=False,
            )
            return result.returncode
        if args.command == "discover":
            service = DiscoveryService(config, pipeline.github, pipeline.store)
            candidates = service.discover(args.repo)
            _json(
                [
                    {
                        "key": item.key,
                        "score": item.score,
                        "blocked": list(item.blockers),
                        "title": item.issue.title,
                        "url": item.issue.url,
                    }
                    for item in candidates
                ]
            )
            return 0
        if args.command == "list":
            rows = pipeline.store.candidates(
                include_blocked=args.all, status=args.status, limit=args.limit
            )
            _json(
                [
                    {
                        "key": item.key,
                        "score": item.score,
                        "status": status,
                        "title": item.issue.title,
                        "blockers": list(item.blockers),
                    }
                    for item, status in rows
                ]
            )
            return 0
        if args.command == "gate":
            _json(pipeline.gate_status(args.repository, args.issue))
            return 0
        if args.command == "prepare":
            _json(pipeline.prepare(args.repository, args.issue))
            return 0
        if args.command == "adopt":
            _json(
                pipeline.adopt(
                    args.repository,
                    args.issue,
                    worktree=args.worktree,
                    summary_text=args.summary,
                    implementation_notes=args.notes,
                    verification_commands=tuple(args.verify),
                )
            )
            return 0
        if args.command == "inspect":
            _json(pipeline.inspect_run(args.run_id))
            return 0
        if args.command == "context":
            if args.context_command == "inspect":
                _json(pipeline.context_bundle(args.run_id))
                return 0
            if args.context_command == "export":
                _json(pipeline.export_context(args.run_id, args.output))
                return 0
            if args.context_command == "import":
                _json(pipeline.import_context(args.source))
                return 0
            raise AssertionError(f"unhandled context command: {args.context_command}")
        if args.command == "logs":
            _json(
                pipeline.run_logs(
                    args.run_id,
                    command_number=args.command,
                    tail_chars=args.tail_chars,
                )
            )
            return 0
        if args.command == "follow-up":
            _json(pipeline.follow_up(args.run_id))
            return 0
        if args.command == "submit":
            _json(
                pipeline.submit(
                    args.repository,
                    args.issue,
                    reviewed_by=args.reviewed_by,
                    reopen_pull_request=args.reopen,
                )
            )
            return 0
        if args.command == "run":
            service = DiscoveryService(config, pipeline.github, pipeline.store)
            service.discover()
            auto_repositories = tuple(
                policy.name
                for policy in config.repositories.values()
                if policy.enabled and policy.auto_prepare
            )
            rows = pipeline.store.candidates(
                limit=max(args.limit * 10, args.limit),
                auto_prepare_repositories=auto_repositories,
                min_score=config.discovery.min_score,
            )
            results = []
            for candidate, _ in rows:
                policy = pipeline.policy(candidate.issue.repository)
                if policy.require_assignment_before_submit:
                    gate = pipeline.gate_status(
                        candidate.issue.repository, candidate.issue.number
                    )
                    if not gate["submission_ready"]:
                        continue
                prepared = pipeline.prepare(
                    candidate.issue.repository, candidate.issue.number
                )
                results.append({"prepared": prepared})
                if len(results) >= args.limit:
                    break
            _json(results)
            return 0
        raise AssertionError(f"unhandled command: {args.command}")
    except (ConfigError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"reposteward: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
