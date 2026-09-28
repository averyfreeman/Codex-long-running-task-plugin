from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from long_running_tasks.codex_exec import CodexResult
from long_running_tasks.runner import RunManager
from long_running_tasks.scheduler import StaleWorkflowError
from long_running_tasks.store import RunStore
from long_running_tasks.workflow import Provenance, WorkflowEdge, WorkflowNode, WorkflowRecord
from long_running_tasks.workflow_run import WorkflowExecutionProjection


FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Workflow Runner", cwd=repo)
    _git("config", "user.email", "runner@example.test", cwd=repo)
    (repo / "TASK.md").write_text(
        """# Task

## Goal

Create the workflow marker.

## Acceptance criteria

- The workflow marker exists.

## Verification

```sh
test -f marker.txt
```
""",
        encoding="utf-8",
    )
    (repo / "PLAN.md").write_text("- [ ] T001: Create the workflow marker\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _node(node_id: str, kind: str, status: str = "approved") -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=kind,
        status=status,
        title=node_id,
        provenance=Provenance.from_text(node_id, f"{node_id}\n", captured_at=FIXED_TIME),
        current=True,
        approved=status == "approved",
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )


def _workflow(
    repo: Path,
    *,
    triage_status: str = "approved",
    workflow_id: str = "workflow-run-test",
) -> WorkflowRecord:
    return WorkflowRecord.create(
        workflow_id,
        repo,
        nodes=[
            _node("triage-1", "triage", triage_status),
            _node("spec-1", "spec"),
            _node("ticket-1", "ticket"),
        ],
        edges=[
            WorkflowEdge("triage-1", "spec-1", "produces"),
            WorkflowEdge("spec-1", "ticket-1", "produces"),
        ],
        now=FIXED_TIME,
    )


class WritingExecutor:
    def run(self, *, worktree, **kwargs):
        Path(worktree, "marker.txt").write_text("workflow\n", encoding="utf-8")
        return CodexResult(
            returncode=0,
            session_id="workflow-session",
            context_ratio=0.1,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="",
            stderr="",
        )


class FailingExecutor:
    def run(self, *, worktree, **kwargs):
        Path(worktree, "marker.txt").write_text("workflow\n", encoding="utf-8")
        return CodexResult(
            returncode=7,
            session_id="workflow-failed-session",
            context_ratio=0.1,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="",
            stderr="failed",
        )


def _wait(manager: RunManager, run_id: str, statuses: set[str]) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        run = manager.get(run_id)
        if run["status"] in statuses:
            return run
        time.sleep(0.02)
    return manager.get(run_id)


def test_projection_round_trips_the_selected_ticket_and_provenance():
    record = _workflow(Path("/tmp/workflow-projection-repo"))
    projection = WorkflowExecutionProjection.from_workflow(record, "ticket-1", run_id="run-1")

    assert projection.ticket_id == "ticket-1"
    assert projection.source_digests["triage-1"] == record.node("triage-1").provenance.digest
    assert projection.blocker_node_ids == ()
    assert WorkflowExecutionProjection.from_json(projection.to_json()).digest == projection.digest


def test_workflow_run_isolated_projection_and_status_are_external(tmp_path: Path):
    repo = _repo(tmp_path)
    store = RunStore(tmp_path / "runtime")
    manager = RunManager(store=store, executor_factory=WritingExecutor)
    run = manager.start_workflow(str(repo), _workflow(repo), ticket_id="ticket-1", max_iterations=1)
    result = _wait(manager, run["run_id"], {"completed", "failed", "needs_recovery"})

    assert result["status"] == "completed"
    projection_path = Path(result["workflow_projection_path"])
    assert projection_path.is_file()
    assert projection_path.parent == store.workflow_projections_dir
    assert projection_path.is_relative_to(repo) is False
    projection_bytes = projection_path.read_bytes()

    workflow = store.get_workflow("workflow-run-test")
    run_node = workflow.node(result["workflow_node_id"])
    assert run_node.status == "completed"
    assert run_node.metadata["ticket_id"] == "ticket-1"
    assert any(
        edge.source == "ticket-1"
        and edge.target == result["workflow_node_id"]
        and edge.kind == "executes"
        for edge in workflow.edges
    )
    assert manager.get_workflow_projection(result["run_id"]).run_id == result["run_id"]
    assert projection_path.read_bytes() == projection_bytes
    assert _git("status", "--porcelain", cwd=repo) == ""


def test_failed_workflow_run_remains_recoverable_and_stale_source_cannot_start(tmp_path: Path):
    repo = _repo(tmp_path)
    store = RunStore(tmp_path / "runtime")
    manager = RunManager(store=store, executor_factory=FailingExecutor)
    run = manager.start_workflow(str(repo), _workflow(repo), max_iterations=1)
    result = _wait(manager, run["run_id"], {"failed", "completed", "needs_recovery"})

    assert result["status"] == "failed"
    workflow = store.get_workflow("workflow-run-test")
    assert workflow.node(result["workflow_node_id"]).is_recoverable

    stale = _workflow(repo, triage_status="stale", workflow_id="stale-workflow")
    with pytest.raises(StaleWorkflowError, match="reconciliation"):
        manager.start_workflow(str(repo), stale)
