from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .retention import RetentionError
from .runner import RunError, RunManager
from .safety import BranchWriteSafetyError
from .store import StoreError
from .workflow import WorkflowError


APPROVAL_MODES = ["policy", "on-request", "auto_review", "never", "unattended"]


def _add_workflow_create(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Register a canonical workflow")
    parser.add_argument("workflow_json", nargs="?", help="JSON text or a path to a workflow record JSON file")
    parser.add_argument("--workflow", dest="workflow_text", help="Workflow record JSON text")


def _add_workflow_get(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Read a canonical workflow")
    parser.add_argument("workflow_id")


def _add_workflow_list(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="List canonical workflows")
    parser.add_argument("--repo-path")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cursor")


def _add_workflow_schedule(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Select a serial workflow frontier")
    parser.add_argument("workflow_id")
    parser.add_argument("--automation-mode", choices=["serial"], default="serial")


def _add_workflow_start(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Start an isolated run from a workflow")
    parser.add_argument("repo_path")
    parser.add_argument("--workflow-id")
    parser.add_argument("--workflow-json", dest="workflow_json", help="JSON text or a path to a workflow record JSON file")
    parser.add_argument("--ticket-id")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--approval-mode", choices=APPROVAL_MODES, default="policy")
    parser.add_argument("--model")
    parser.add_argument("--detach", action="store_true")


def _add_workflow_projection(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Read a workflow execution projection")
    parser.add_argument("run_id")


def _add_workflow_reconcile(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Reconcile workflow source artifacts")
    parser.add_argument("workflow_id")
    parser.add_argument("--confirm", action="store_true")


def _add_retention_preview(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Preview runtime retention")
    parser.add_argument("--repo-path")
    parser.add_argument("--max-age-seconds", type=float)
    parser.add_argument("--max-runs", type=int)


def _add_prune_runs(container, name: str, *, aliases: list[str] | None = None) -> None:
    parser = container.add_parser(name, aliases=aliases or [], help="Prune confirmed terminal runtime artifacts")
    parser.add_argument("--repo-path")
    parser.add_argument("--max-age-seconds", type=float)
    parser.add_argument("--max-runs", type=int)
    parser.add_argument("--confirm", action="store_true")


def _add_workflow_commands(subparsers) -> None:
    workflow = subparsers.add_parser("workflow", aliases=["workflows"], help="Operate on canonical workflows")
    workflow_subparsers = workflow.add_subparsers(dest="workflow_command", required=True)
    _add_workflow_create(workflow_subparsers, "create")
    _add_workflow_get(workflow_subparsers, "get")
    _add_workflow_list(workflow_subparsers, "list")
    _add_workflow_schedule(workflow_subparsers, "schedule")
    _add_workflow_start(workflow_subparsers, "start")
    _add_workflow_projection(workflow_subparsers, "projection")
    _add_workflow_reconcile(workflow_subparsers, "reconcile")

    _add_workflow_create(subparsers, "workflow-create", aliases=["create-workflow"])
    _add_workflow_get(subparsers, "workflow-get", aliases=["get-workflow"])
    _add_workflow_list(subparsers, "workflow-list", aliases=["list-workflows"])
    _add_workflow_schedule(subparsers, "workflow-schedule", aliases=["schedule-workflow"])
    _add_workflow_start(subparsers, "workflow-start", aliases=["start-workflow"])
    _add_workflow_projection(subparsers, "workflow-projection", aliases=["get-workflow-projection"])
    _add_workflow_reconcile(subparsers, "workflow-reconcile", aliases=["reconcile-workflow"])


def _add_retention_commands(subparsers) -> None:
    retention = subparsers.add_parser("retention", help="Preview or apply runtime retention")
    retention_subparsers = retention.add_subparsers(dest="retention_command", required=True)
    _add_retention_preview(retention_subparsers, "preview")
    _add_prune_runs(retention_subparsers, "prune")
    _add_retention_preview(subparsers, "retention-preview", aliases=["preview-retention"])
    _add_prune_runs(subparsers, "prune-runs", aliases=["prune"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run explicit repository tasks through isolated Codex slices.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start and monitor a run")
    start.add_argument("repo_path")
    start.add_argument("--max-iterations", type=int)
    start.add_argument("--approval-mode", choices=APPROVAL_MODES, default="policy")
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
    _add_workflow_commands(subparsers)
    _add_retention_commands(subparsers)
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _load_workflow(value: str | None) -> dict:
    if not value:
        raise RunError("A workflow JSON object or JSON file path is required")
    candidate = Path(value).expanduser()
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    if is_file:
        try:
            value = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunError(f"Unable to read workflow JSON file: {candidate}") from exc
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RunError("Workflow input must be valid JSON or a readable JSON file") from exc
    if not isinstance(payload, dict):
        raise RunError("Workflow input must be a JSON object")
    return payload


def _workflow_command(args: argparse.Namespace) -> str | None:
    if args.command in {"workflow", "workflows"}:
        return f"workflow-{args.workflow_command}"
    if args.command in {
        "workflow-create", "create-workflow", "workflow-get", "get-workflow",
        "workflow-list", "list-workflows", "workflow-schedule", "schedule-workflow",
        "workflow-start", "start-workflow", "workflow-projection", "get-workflow-projection",
        "workflow-reconcile", "reconcile-workflow",
    }:
        return args.command
    return None


def _retention_command(args: argparse.Namespace) -> str | None:
    if args.command == "retention":
        return f"retention-{args.retention_command}"
    if args.command in {"retention-preview", "preview-retention", "prune-runs", "prune"}:
        return args.command
    return None


def _monitor(manager: RunManager, result: dict, *, detach: bool) -> int:
    _print(result)
    if detach:
        return 0
    while True:
        time.sleep(0.25)
        result = manager.get(result["run_id"])
        if result["status"] in {"completed", "cancelled", "failed", "needs_approval", "needs_recovery", "paused"}:
            _print(result)
            return 0 if result["status"] == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manager = RunManager()
    try:
        if args.command == "start":
            result = manager.start(args.repo_path, max_iterations=args.max_iterations, approval_mode=args.approval_mode, model=args.model)
            return _monitor(manager, result, detach=args.detach)
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
        else:
            workflow_command = _workflow_command(args)
            if workflow_command == "workflow-create":
                _print(manager.create_workflow(_load_workflow(args.workflow_text or args.workflow_json)))
            elif workflow_command == "workflow-get":
                _print(manager.get_workflow(args.workflow_id))
            elif workflow_command == "workflow-list":
                _print(manager.list_workflows(args.repo_path, limit=args.limit, cursor=args.cursor))
            elif workflow_command == "workflow-schedule":
                _print(manager.schedule_workflow(args.workflow_id, automation_mode=args.automation_mode))
            elif workflow_command == "workflow-start":
                workflow = _load_workflow(args.workflow_json) if args.workflow_json else None
                result = manager.start_workflow(
                    args.repo_path,
                    workflow,
                    workflow_id=args.workflow_id,
                    ticket_id=args.ticket_id,
                    max_iterations=args.max_iterations,
                    approval_mode=args.approval_mode,
                    model=args.model,
                )
                return _monitor(manager, result, detach=args.detach)
            elif workflow_command == "workflow-projection":
                _print(manager.get_workflow_projection(args.run_id).to_dict())
            elif workflow_command == "workflow-reconcile":
                _print(manager.reconcile_workflow(args.workflow_id, confirm=args.confirm))
            else:
                retention_command = _retention_command(args)
                if retention_command in {"retention-preview", "preview-retention"}:
                    _print(manager.retention_preview(repo_path=args.repo_path, max_age_seconds=args.max_age_seconds, max_runs=args.max_runs))
                elif retention_command in {"retention-prune", "prune-runs", "prune"}:
                    _print(manager.prune_runs(repo_path=args.repo_path, max_age_seconds=args.max_age_seconds, max_runs=args.max_runs, confirm=args.confirm))
        return 0
    except (RunError, StoreError, WorkflowError, RetentionError, BranchWriteSafetyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
