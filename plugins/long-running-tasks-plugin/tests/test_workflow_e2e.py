from __future__ import annotations

import subprocess
import time
from pathlib import Path

from long_running_tasks.artifacts import LocalMattArtifactCatalog
from long_running_tasks.codex_exec import CodexResult
from long_running_tasks.runner import RunManager
from long_running_tasks.store import RunStore


FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _workflow_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Workflow E2E", cwd=repo)
    _git("config", "user.email", "workflow-e2e@example.test", cwd=repo)
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
    feature = repo / ".scratch" / "e2e-feature"
    issues = feature / "issues"
    issues.mkdir(parents=True)
    (feature / "triage.md").write_text(
        "---\nstatus: ready-for-agent\n---\n# Triage\n\nApproved for implementation.\n",
        encoding="utf-8",
    )
    (feature / "spec.md").write_text(
        "---\nstatus: current\n---\n# Spec\n\nKeep the run isolated.\n",
        encoding="utf-8",
    )
    (issues / "01-foundation.md").write_text(
        "---\nstatus: ready-for-agent\n---\n# 01: Foundation\n\nCreate the marker.\n",
        encoding="utf-8",
    )
    (issues / "02-follow-up.md").write_text(
        "---\nstatus: ready-for-agent\nblocked_by: 01: Foundation\n---\n"
        "# 02: Follow-up\n\nKeep the blocker edge.\n",
        encoding="utf-8",
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "workflow fixture", cwd=repo)
    return repo


class FlakyExecutor:
    """Fail once, retain the implementation, then allow a recovery run."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, worktree: Path, **_kwargs) -> CodexResult:
        self.calls += 1
        if self.calls == 1:
            Path(worktree, "marker.txt").write_text("retained\n", encoding="utf-8")
            return CodexResult(
                returncode=7,
                session_id="e2e-failed-session",
                context_ratio=0.1,
                context_pressure=False,
                approval_requested=False,
                cancelled=False,
                timed_out=False,
                stdout="",
                stderr="simulated failure",
            )
        return CodexResult(
            returncode=0,
            session_id="e2e-recovered-session",
            context_ratio=0.1,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="",
            stderr="",
        )


def _wait_for(manager: RunManager, run_id: str, statuses: set[str]) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        result = manager.get(run_id)
        if result["status"] in statuses:
            return result
        time.sleep(0.02)
    return manager.get(run_id)


def test_local_matt_catalog_to_failed_run_to_recovered_checkpoint(tmp_path: Path):
    repo = _workflow_repo(tmp_path)
    original_source = {
        path.relative_to(repo): path.read_bytes()
        for path in (repo / ".scratch").rglob("*")
        if path.is_file()
    }
    workflow = LocalMattArtifactCatalog(repo).as_workflow_record(
        "e2e-workflow",
        repo,
        status="ready",
        now=FIXED_TIME,
    )
    assert {node.kind for node in workflow.nodes} == {"triage", "spec", "ticket"}
    assert workflow.blockers_for("ticket-e2e-feature-02-follow-up") == (
        "ticket-e2e-feature-01-foundation",
    )

    store = RunStore(tmp_path / "runtime")
    executor = FlakyExecutor()
    manager = RunManager(store=store, executor_factory=lambda: executor)
    first = manager.start_workflow(str(repo), workflow, max_iterations=1)
    failed = _wait_for(manager, first["run_id"], {"failed", "completed", "needs_recovery"})

    assert failed["status"] == "failed"
    manager.project_workflow_status(failed["run_id"])
    failed_workflow = store.get_workflow("e2e-workflow")
    failed_node = failed_workflow.node(failed["workflow_node_id"])
    assert failed_node.status == "failed"
    assert failed_node.is_recoverable
    projection_path = Path(failed["workflow_projection_path"])
    projection_before = projection_path.read_bytes()

    resumed = manager.resume(failed["run_id"])
    completed = _wait_for(manager, resumed["run_id"], {"completed", "failed", "needs_recovery"})

    assert completed["run_id"] == failed["run_id"]
    assert completed["status"] == "completed"
    manager.project_workflow_status(completed["run_id"])
    assert executor.calls == 2
    assert projection_path.read_bytes() == projection_before
    completed_workflow = store.get_workflow("e2e-workflow")
    assert completed_workflow.node(completed["workflow_node_id"]).status == "completed"
    assert Path(completed["worktree_path"], "marker.txt").read_text(encoding="utf-8") == "retained\n"
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert {
        path.relative_to(repo): path.read_bytes()
        for path in (repo / ".scratch").rglob("*")
        if path.is_file()
    } == original_source


def test_interrupted_workflow_records_are_recoverable_and_retained(tmp_path: Path):
    repo = _workflow_repo(tmp_path)
    workflow = LocalMattArtifactCatalog(repo).as_workflow_record(
        "interrupted-workflow",
        repo,
        status="interrupted",
        now=FIXED_TIME,
    )
    from long_running_tasks.workflow import Provenance, WorkflowNode

    interrupted = WorkflowNode(
        node_id="run-interrupted",
        kind="run",
        status="interrupted",
        title="Interrupted fixture run",
        provenance=Provenance.from_text(
            "workflow://interrupted-workflow/runs/run-interrupted",
            "interrupted fixture",
            captured_at=FIXED_TIME,
        ),
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
        metadata={"recoverable": True},
    )
    workflow = workflow.add_node(interrupted, now=FIXED_TIME)
    store = RunStore(tmp_path / "runtime")
    manager = RunManager(store=store, executor_factory=lambda: object())
    manager.create_workflow(workflow)

    run_path = store.run_path("interrupted-run")
    run_path.write_text(
        '{"run_id":"interrupted-run","repo_path":"/tmp/repo",'
        '"status":"interrupted","updated_at":"2020-01-01T00:00:00+00:00",'
        '"worker_pid":null,"worker_launch_pid":null}\n',
        encoding="utf-8",
    )

    persisted = store.get_workflow("interrupted-workflow")
    assert persisted.node("run-interrupted").is_recoverable
    preview = manager.retention_preview(max_age_seconds=1)
    assert [item["run_id"] for item in preview["candidates"]] == []
    assert any(item["run_id"] == "interrupted-run" for item in preview["retained"])
