from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .discovery import DiscoveryService
from .doctor import run_doctor
from .pipeline import Pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="starfix", description="Turn suitable GitHub issues into policy-gated PRs."
    )
    parser.add_argument("--config", default="starfix.toml", help="path to TOML config")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    submit = subparsers.add_parser(
        "submit", help="push a reviewed change and open a draft PR"
    )
    submit.add_argument("repository")
    submit.add_argument("issue", type=int)
    submit.add_argument("--reviewed-by", required=True)

    run = subparsers.add_parser(
        "run", help="discover and prepare top auto-enabled issues"
    )
    run.add_argument("--limit", type=int, default=1)
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        pipeline = Pipeline(config)
        if args.command == "doctor":
            report, ok = run_doctor(config)
            _json(report)
            return 0 if ok else 1
        if args.command == "image":
            root = config.path.parent
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    config.runner.image,
                    "-f",
                    str(root / "docker" / "Dockerfile.runner"),
                    str(root),
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
        if args.command == "submit":
            _json(
                pipeline.submit(
                    args.repository, args.issue, reviewed_by=args.reviewed_by
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
        print(f"starfix: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
