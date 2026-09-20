from __future__ import annotations

import argparse
import json
import sys
import time

from .runner import RunError, RunManager


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run explicit repository tasks through isolated Codex slices.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start and monitor a run")
    start.add_argument("repo_path")
    start.add_argument("--max-iterations", type=int)
    start.add_argument("--approval-mode", choices=["policy", "on-request", "auto_review", "never", "unattended"], default="policy")
    start.add_argument("--model")
    start.add_argument("--detach", action="store_true", help="Return after queuing; the MCP server is preferred for durable detached runs")

    status = subparsers.add_parser("status", help="Read a run status")
    status.add_argument("run_id")

    listing = subparsers.add_parser("list", help="List runs")
    listing.add_argument("--repo-path")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--cursor")

    logs = subparsers.add_parser("logs", help="Read run logs")
    logs.add_argument("run_id")
    logs.add_argument("--limit", type=int, default=100)
    logs.add_argument("--cursor")

    resume = subparsers.add_parser("resume", help="Resume a run")
    resume.add_argument("run_id")
    resume.add_argument("--approval-mode", choices=["policy", "on-request", "auto_review", "never", "unattended"])

    cancel = subparsers.add_parser("cancel", help="Cancel a run")
    cancel.add_argument("run_id")
    cancel.add_argument("--confirm", action="store_true")

    recover = subparsers.add_parser("recover", help="Recover a run")
    recover.add_argument("run_id")
    recover.add_argument("--confirm", action="store_true")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manager = RunManager()
    try:
        if args.command == "start":
            result = manager.start(args.repo_path, max_iterations=args.max_iterations, approval_mode=args.approval_mode, model=args.model)
            _print(result)
            if args.detach:
                return 0
            while True:
                time.sleep(0.25)
                result = manager.get(result["run_id"])
                if result["status"] in {"completed", "cancelled", "failed", "needs_approval", "needs_recovery", "paused"}:
                    _print(result)
                    return 0 if result["status"] == "completed" else 1
        if args.command == "status":
            _print(manager.get(args.run_id))
        elif args.command == "list":
            _print(manager.list(args.repo_path, limit=args.limit, cursor=args.cursor))
        elif args.command == "logs":
            _print(manager.logs(args.run_id, limit=args.limit, cursor=args.cursor))
        elif args.command == "resume":
            _print(manager.resume(args.run_id, approval_mode=args.approval_mode))
        elif args.command == "cancel":
            _print(manager.cancel(args.run_id, confirm=args.confirm))
        elif args.command == "recover":
            _print(manager.recover(args.run_id, confirm=args.confirm))
        return 0
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
