from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from .retention import RetentionError
from .runner import RunError, RunManager
from .safety import BranchWriteSafetyError
from .store import StoreError
from .workflow import WorkflowError
from .workflow_control import ReconciliationConfirmationRequired, WorkflowOperationError


manager = RunManager()
server = Server("long-running-tasks")


TOOL_DEFINITIONS = [
    Tool(
        name="start_run",
        description="Start an asynchronous isolated Codex run for TASK.md and PLAN.md. Returns a stable run_id and initial state. Use when a user explicitly requests durable multi-step repository work.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Absolute path to the clean Git repository containing TASK.md and PLAN.md."},
                "max_iterations": {"type": "integer", "minimum": 1, "description": "Optional maximum number of plan slices before the run pauses."},
                "approval_mode": {"type": "string", "enum": ["policy", "on-request", "auto_review", "never", "unattended"], "default": "policy", "description": "Approval policy; policy is safe-by-default and unattended is an explicit no-routine-prompt opt-in."},
                "model": {"type": "string", "description": "Optional Codex model identifier."},
            },
            "required": ["repo_path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=False),
    ),
    Tool(
        name="get_run_status",
        description="Read the current state of an asynchronous Codex run, including its worktree, branch, slice, approval state, and last checkpoint. Use when monitoring or diagnosing a run.",
        inputSchema={
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Stable run identifier returned by start_run."}},
            "required": ["run_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="list_runs",
        description="List bounded historical and active Codex runs for a repository using an opaque cursor. Returns run summaries and next_cursor. Use when finding an existing run to resume or inspect.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Optional absolute repository path filter."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Maximum number of runs to return."},
                "cursor": {"type": "string", "description": "Opaque cursor returned by a previous list_runs call."},
            },
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="get_run_logs",
        description="Read bounded structured logs for a Codex run with an opaque cursor. Returns whole log entries and next_cursor. Use when inspecting progress, verification, approval, or recovery details.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Stable run identifier."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100, "description": "Maximum number of log entries to return."},
                "cursor": {"type": "string", "description": "Opaque cursor returned by a previous get_run_logs call."},
            },
            "required": ["run_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="resume_run",
        description="Resume a paused, failed, approval-gated, or recoverable Codex run in its retained worktree. Returns the same run_id and queued state. Use after reviewing the reported reason.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Stable run identifier."},
                "approval_mode": {"type": "string", "enum": ["policy", "on-request", "auto_review", "never", "unattended"], "description": "Optional replacement approval policy for the resumed run."},
            },
            "required": ["run_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="cancel_run",
        description="Request safe cancellation of a running Codex subprocess group and preserve its retained worktree for inspection. Returns cancellation progress and eventually the terminal cancelled state. Use only when the user confirms cancellation.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Stable run identifier."},
                "confirm": {"type": "boolean", "description": "Must be true to cancel the run and terminate its subprocess group."},
            },
            "required": ["run_id", "confirm"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="recover_run",
        description="Reconcile a crash at a commit or annotated-tag boundary without overwriting conflicting Git refs. Returns the recovered run state. Use when a run reports needs_recovery.",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Stable run identifier."},
                "confirm": {"type": "boolean", "description": "Must be true to perform idempotent checkpoint reconciliation."},
            },
            "required": ["run_id", "confirm"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="create_workflow",
        description="Create or idempotently register one validated local workflow record linking triage, spec, ticket, and run artifacts. Use when a workflow record has been prepared for the isolated runner.",
        inputSchema={
            "type": "object",
            "properties": {
                "workflow": {"type": "object", "description": "Validated workflow record JSON with local provenance and typed nodes and edges."},
            },
            "required": ["workflow"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="get_workflow",
        description="Read one canonical local workflow record, including lifecycle history, provenance digests, blocker edges, and retained run nodes. Use when inspecting workflow ownership or recovery state.",
        inputSchema={
            "type": "object",
            "properties": {"workflow_id": {"type": "string", "description": "Stable canonical workflow identifier."}},
            "required": ["workflow_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="list_workflows",
        description="List bounded canonical local workflows with an opaque cursor and optional repository filter. Use when finding a workflow to schedule, reconcile, or inspect.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Optional absolute repository path filter."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Maximum number of workflow records to return."},
                "cursor": {"type": "string", "description": "Opaque cursor returned by a previous list_workflows call."},
            },
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="schedule_workflow",
        description="Select at most one current, approved, unblocked ticket from a canonical workflow's serial frontier. Use before starting a workflow run; stale sources and parallel mode fail closed.",
        inputSchema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "Stable canonical workflow identifier."},
                "automation_mode": {"type": "string", "enum": ["serial"], "default": "serial", "description": "The only supported automatic scheduling mode."},
            },
            "required": ["workflow_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="start_workflow",
        description="Start the existing isolated runner from a canonical workflow record and immutable ticket execution projection. Use after schedule_workflow; the base checkout remains read-only to the runner.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Absolute path to the clean Git repository for the workflow."},
                "workflow_id": {"type": "string", "description": "Existing canonical workflow identifier."},
                "workflow": {"type": "object", "description": "Validated workflow record JSON to register locally before starting."},
                "ticket_id": {"type": "string", "description": "Optional selected ticket node; omitted selects the serial frontier."},
                "max_iterations": {"type": "integer", "minimum": 1, "description": "Optional maximum number of plan slices."},
                "approval_mode": {"type": "string", "enum": ["policy", "on-request", "auto_review", "never", "unattended"], "default": "policy", "description": "Approval policy for the isolated runner."},
                "model": {"type": "string", "description": "Optional Codex model identifier."},
            },
            "required": ["repo_path"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=False),
    ),
    Tool(
        name="get_workflow_projection",
        description="Read the immutable execution projection captured by a workflow-bound run, including source digests and the selected ticket. Use when verifying exactly what a run received.",
        inputSchema={
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "Stable workflow-bound run identifier."}},
            "required": ["run_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="reconcile_workflow",
        description="Audit local triage, spec, and ticket source artifacts against their recorded provenance and optionally persist stale transitions. Use explicit confirmation before changing the canonical workflow record.",
        inputSchema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "Stable canonical workflow identifier."},
                "confirm": {"type": "boolean", "default": False, "description": "Set true to persist the reconciliation result; false returns a dry-run audit."},
                "require_confirmation": {"type": "boolean", "default": False, "description": "Set true to return an error instead of a dry-run when changes need confirmation."},
            },
            "required": ["workflow_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="retention_preview",
        description="Preview bounded retention candidates for terminal runtime artifacts without deleting anything. Use it before pruning; failed, interrupted, and recovery-needed runs remain protected and inspectable.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Optional absolute repository path filter."},
                "max_age_seconds": {"type": "number", "minimum": 0, "description": "Optional age bound for terminal runtime records."},
                "max_runs": {"type": "integer", "minimum": 0, "description": "Optional number of newest terminal records to retain."},
            },
        },
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False, idempotentHint=True),
    ),
    Tool(
        name="prune_runs",
        description="Prune explicitly confirmed terminal runtime artifacts under a bounded retention policy. Use only after reviewing retention_preview; protected failed and interrupted runs are never removed.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Optional absolute repository path filter."},
                "max_age_seconds": {"type": "number", "minimum": 0, "description": "Optional age bound for terminal runtime records."},
                "max_runs": {"type": "integer", "minimum": 0, "description": "Optional number of newest terminal records to retain."},
                "confirm": {"type": "boolean", "description": "Must be true to delete the selected runtime artifacts."},
            },
            "required": ["confirm"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False, idempotentHint=True),
    ),
]


def _text(value: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(value, indent=2, sort_keys=True, default=str))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_DEFINITIONS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments = arguments or {}
    try:
        if name == "start_run":
            result = await asyncio.to_thread(
                manager.start,
                arguments["repo_path"],
                max_iterations=arguments.get("max_iterations"),
                approval_mode=arguments.get("approval_mode", "policy"),
                model=arguments.get("model"),
            )
        elif name == "get_run_status":
            result = await asyncio.to_thread(manager.get, arguments["run_id"])
        elif name == "list_runs":
            result = await asyncio.to_thread(
                manager.list,
                arguments.get("repo_path"),
                limit=arguments.get("limit", 20),
                cursor=arguments.get("cursor"),
            )
        elif name == "get_run_logs":
            result = await asyncio.to_thread(
                manager.logs,
                arguments["run_id"],
                limit=arguments.get("limit", 100),
                cursor=arguments.get("cursor"),
            )
        elif name == "resume_run":
            result = await asyncio.to_thread(
                manager.resume,
                arguments["run_id"],
                approval_mode=arguments.get("approval_mode"),
            )
        elif name == "cancel_run":
            result = await asyncio.to_thread(manager.cancel, arguments["run_id"], confirm=arguments.get("confirm", False))
        elif name == "recover_run":
            result = await asyncio.to_thread(manager.recover, arguments["run_id"], confirm=arguments.get("confirm", False))
        elif name == "create_workflow":
            result = await asyncio.to_thread(manager.create_workflow, arguments["workflow"])
        elif name == "get_workflow":
            result = await asyncio.to_thread(manager.get_workflow, arguments["workflow_id"])
        elif name == "list_workflows":
            result = await asyncio.to_thread(
                manager.list_workflows,
                arguments.get("repo_path"),
                limit=arguments.get("limit", 20),
                cursor=arguments.get("cursor"),
            )
        elif name == "schedule_workflow":
            result = await asyncio.to_thread(
                manager.schedule_workflow,
                arguments["workflow_id"],
                automation_mode=arguments.get("automation_mode"),
            )
        elif name == "start_workflow":
            result = await asyncio.to_thread(
                manager.start_workflow,
                arguments["repo_path"],
                arguments.get("workflow"),
                workflow_id=arguments.get("workflow_id"),
                ticket_id=arguments.get("ticket_id"),
                max_iterations=arguments.get("max_iterations"),
                approval_mode=arguments.get("approval_mode", "policy"),
                model=arguments.get("model"),
            )
        elif name == "get_workflow_projection":
            projection = await asyncio.to_thread(manager.get_workflow_projection, arguments["run_id"])
            result = projection.to_dict()
        elif name == "reconcile_workflow":
            result = await asyncio.to_thread(
                manager.reconcile_workflow,
                arguments["workflow_id"],
                confirm=arguments.get("confirm", False),
                require_confirmation=arguments.get("require_confirmation", False),
            )
        elif name == "retention_preview":
            result = await asyncio.to_thread(
                manager.retention_preview,
                repo_path=arguments.get("repo_path"),
                max_age_seconds=arguments.get("max_age_seconds"),
                max_runs=arguments.get("max_runs"),
            )
        elif name == "prune_runs":
            result = await asyncio.to_thread(
                manager.prune_runs,
                repo_path=arguments.get("repo_path"),
                max_age_seconds=arguments.get("max_age_seconds"),
                max_runs=arguments.get("max_runs"),
                confirm=arguments.get("confirm", False),
            )
        else:
            return _text({"error": f"Unknown tool: {name}"})
        return _text(result)
    except (
        KeyError,
        RunError,
        StoreError,
        ValueError,
        WorkflowError,
        WorkflowOperationError,
        ReconciliationConfirmationRequired,
        RetentionError,
        BranchWriteSafetyError,
    ) as exc:
        return _text({"error": str(exc), "tool": name})
    except Exception as exc:  # pragma: no cover - defensive MCP boundary
        return _text({"error": f"Unhandled tool error: {exc}", "tool": name})


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
