from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from .runner import RunError, RunManager
from .store import StoreError


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
        else:
            return _text({"error": f"Unknown tool: {name}"})
        return _text(result)
    except (KeyError, RunError, StoreError, ValueError) as exc:
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
