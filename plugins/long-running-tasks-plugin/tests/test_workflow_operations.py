from __future__ import annotations

import json
from pathlib import Path

import pytest

from long_running_tasks.cli import _parser
from long_running_tasks.mcp_server import TOOL_DEFINITIONS
from long_running_tasks.runner import RunManager
from long_running_tasks.scheduler import StaleWorkflowError
from long_running_tasks.store import RunStore
from long_running_tasks.workflow import Provenance, WorkflowEdge, WorkflowNode, WorkflowRecord
from long_running_tasks.workflow_control import ReconciliationConfirmationRequired
from long_running_tasks.retention import RetentionError
from long_running_tasks.safety import BranchWriteSafetyError


FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _source_node(
    node_id: str,
    kind: str,
    path: Path,
    repo: Path,
    *,
    status: str = "approved",
) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=kind,
        status=status,
        title=node_id,
        provenance=Provenance.from_file(path, captured_at=FIXED_TIME),
        artifact=str(path.relative_to(repo)),
        current=True,
        approved=True,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )


def _workflow(repo: Path) -> WorkflowRecord:
    scratch = repo / ".scratch" / "feature"
    scratch.mkdir(parents=True)
    triage = scratch / "triage.md"
    spec = scratch / "spec.md"
    ticket = scratch / "issues" / "01-ticket.md"
    ticket.parent.mkdir()
    triage.write_text("# Triage\n", encoding="utf-8")
    spec.write_text("# Spec\n", encoding="utf-8")
    ticket.write_text("# Ticket\n", encoding="utf-8")
    return WorkflowRecord.create(
        "workflow-ops",
        repo,
        status="ready",
        nodes=[
            _source_node("triage-1", "triage", triage, repo),
            _source_node("spec-1", "spec", spec, repo),
            _source_node("ticket-1", "ticket", ticket, repo),
        ],
        edges=[
            WorkflowEdge("triage-1", "spec-1", "produces"),
            WorkflowEdge("spec-1", "ticket-1", "produces"),
        ],
        now=FIXED_TIME,
    )


def test_workflow_operations_schedule_and_reconcile_without_writing_source(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore(tmp_path / "runtime")
    manager = RunManager(store=store, executor_factory=lambda: None)
    record = _workflow(repo)
    manager.create_workflow(record)

    scheduled = manager.schedule_workflow(record.workflow_id)
    assert [node["node_id"] for node in scheduled["frontier"]] == ["ticket-1"]

    source = repo / ".scratch" / "feature" / "triage.md"
    source.write_text("# Triage changed\n", encoding="utf-8")

    preview = manager.reconcile_workflow(record.workflow_id)
    assert preview["applied"] is False
    assert preview["requires_confirmation"] is True
    assert "triage-1" in preview["changed"]
    assert store.get_workflow(record.workflow_id).node("triage-1").status == "approved"
    with pytest.raises(ReconciliationConfirmationRequired):
        manager.reconcile_workflow(record.workflow_id, require_confirmation=True)

    applied = manager.reconcile_workflow(record.workflow_id, confirm=True)
    assert applied["applied"] is True
    assert store.get_workflow(record.workflow_id).node("triage-1").status == "stale"
    with pytest.raises(StaleWorkflowError, match="reconciliation"):
        manager.schedule_workflow(record.workflow_id)
    assert source.read_text(encoding="utf-8") == "# Triage changed\n"


def _run_record(store: RunStore, run_id: str, status: str) -> Path:
    path = store.run_path(run_id)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_path": "/tmp/repo",
                "status": status,
                "updated_at": "2020-01-01T00:00:00+00:00",
                "worker_pid": None,
                "worker_launch_pid": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_retention_preview_and_prune_preserve_recoverable_runs(tmp_path: Path):
    store = RunStore(tmp_path / "runtime")
    manager = RunManager(store=store, executor_factory=lambda: None)
    completed = _run_record(store, "completed", "completed")
    failed = _run_record(store, "failed", "failed")

    preview = manager.retention_preview(max_age_seconds=1)
    assert [item["run_id"] for item in preview["candidates"]] == ["completed"]
    with pytest.raises(RetentionError, match="confirm"):
        manager.prune_runs(max_age_seconds=1)

    result = manager.prune_runs(max_age_seconds=1, confirm=True)
    assert result["removed"] == ["completed"]
    assert not completed.exists()
    assert failed.exists()


def test_branch_write_gate_rejects_base_checkout_and_non_runner_branch(tmp_path: Path):
    manager = RunManager(store=RunStore(tmp_path / "runtime"), executor_factory=lambda: None)
    with pytest.raises(BranchWriteSafetyError, match="base checkout"):
        manager.assert_branch_write_safe(
            {
                "repo_path": str(tmp_path / "repo"),
                "worktree_path": str(tmp_path / "repo"),
                "branch": "main",
            }
        )

    with pytest.raises(BranchWriteSafetyError, match="runner-owned branch"):
        manager.assert_branch_write_safe(
            {
                "repo_path": str(tmp_path / "repo"),
                "worktree_path": str(tmp_path / "runtime" / "worktrees" / "run-1"),
                "branch": "main",
            }
        )


def test_workflow_mcp_tools_and_nested_cli_commands_are_discoverable():
    names = {tool.name for tool in TOOL_DEFINITIONS}
    assert {
        "create_workflow",
        "get_workflow",
        "list_workflows",
        "schedule_workflow",
        "start_workflow",
        "get_workflow_projection",
        "reconcile_workflow",
        "retention_preview",
        "prune_runs",
    } <= names
    tools = {tool.name: tool for tool in TOOL_DEFINITIONS}
    assert tools["schedule_workflow"].annotations.readOnlyHint is True
    assert tools["retention_preview"].annotations.readOnlyHint is True
    assert tools["prune_runs"].annotations.destructiveHint is True

    assert _parser().parse_args(["workflow", "list"]).workflow_command == "list"
    assert _parser().parse_args(["workflow", "reconcile", "workflow-ops", "--confirm"]).confirm is True
    assert _parser().parse_args(["retention", "prune", "--confirm"]).confirm is True
