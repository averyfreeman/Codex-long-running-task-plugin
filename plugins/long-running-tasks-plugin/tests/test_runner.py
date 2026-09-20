from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import long_running_tasks.runner as runner_module
from long_running_tasks.codex_exec import CodexResult
from long_running_tasks.runner import RunError, RunManager
from long_running_tasks.store import RunStore, StoreError


TASK_TEMPLATE = """# Task

## Goal

Create a marker for the selected slice.

## Acceptance criteria

- The marker exists after the slice.

## Verification

```sh
{verification}
```
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def make_repo(tmp_path: Path, verification: str = "test -f marker.txt") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.name", "Test Runner", cwd=repo)
    git("config", "user.email", "runner@example.test", cwd=repo)
    (repo / "TASK.md").write_text(TASK_TEMPLATE.format(verification=verification), encoding="utf-8")
    (repo / "PLAN.md").write_text("- [ ] T001: Create the marker\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "initial", cwd=repo)
    return repo


def install_fake_codex(tmp_path: Path, mode: str = "success") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

if '--version' in sys.argv:
    print('codex-cli fake')
    raise SystemExit(0)
if '--help' in sys.argv:
    raise SystemExit(0)
prompt = sys.stdin.read()
mode = os.environ.get('FAKE_CODEX_MODE', 'success')
print(json.dumps({'type': 'thread.started', 'thread_id': 'fake-session'}), flush=True)
if mode == 'approval':
    print(json.dumps({'type': 'approval.requested', 'requires_approval': True}), flush=True)
    raise SystemExit(0)
if mode == 'context':
    print(json.dumps({'type': 'usage', 'input_tokens': 80000, 'context_window': 100000}), flush=True)
    time.sleep(10)
if mode != 'noop':
    pathlib.Path('marker.txt').write_text('done\\n', encoding='utf-8')
if mode == 'failure':
    raise SystemExit(7)
print(json.dumps({'type': 'response.completed', 'prompt_length': len(prompt)}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def wait_for(manager: RunManager, run_id: str, statuses: set[str]) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        record = manager.get(run_id)
        if record["status"] in statuses:
            return record
        time.sleep(0.05)
    return manager.get(run_id)


def manager_for(tmp_path: Path, mode: str = "success", verification_timeout: float = 5) -> RunManager:
    fake_bin = install_fake_codex(tmp_path, mode)
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
    os.environ["FAKE_CODEX_MODE"] = mode
    return RunManager(
        store=RunStore(tmp_path / "runtime"),
        worker_backend="inline",
        verification_timeout_seconds=verification_timeout,
        codex_timeout_seconds=4,
    )


def test_start_preflight_failure_creates_no_persistent_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    store = RunStore(tmp_path / "runtime")

    def fail_preflight(*args, **kwargs):
        raise runner_module.CodexError(
            "--approve-for-me cannot be combined with --sandbox"
        )

    monkeypatch.setattr(runner_module, "preflight_command", fail_preflight)
    manager = RunManager(store=store, worker_backend="inline")
    with pytest.raises(RunError, match="preflight failed"):
        manager.start(str(repo), approval_mode="unattended")

    assert not list(store.runs_dir.glob("*.json"))
    assert not list((store.root / "worktrees").glob("*"))
    assert not list(store.locks_dir.glob("*.json"))


def test_successful_slice_isolated_and_checkpointed(tmp_path: Path):
    repo = make_repo(tmp_path)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    manager = manager_for(tmp_path)
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"completed", "failed", "needs_recovery", "needs_approval", "paused"})

    assert result["status"] == "completed"
    assert result["branch"].startswith("codex/long-running/")
    worktree = Path(result["worktree_path"])
    assert git("branch", "--show-current", cwd=worktree) == result["branch"]
    assert git("rev-parse", "HEAD", cwd=repo) == base_sha
    assert "- [x] T001" in (worktree / "PLAN.md").read_text(encoding="utf-8")
    assert result["last_checkpoint"]["commit"] == git("rev-parse", "HEAD", cwd=worktree)
    assert git("rev-parse", f"{result['last_checkpoint']['tag']}^{{}}", cwd=worktree) == result["last_checkpoint"]["commit"]
    assert not str(worktree) in result["log_path"]


def test_verification_failure_creates_no_checkpoint(tmp_path: Path):
    repo = make_repo(tmp_path, verification="false")
    manager = manager_for(tmp_path)
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"failed", "completed", "needs_recovery"})

    assert result["status"] == "failed"
    worktree = Path(result["worktree_path"])
    assert "- [ ] T001" in (worktree / "PLAN.md").read_text(encoding="utf-8")
    assert result["last_checkpoint"] is None
    assert not list(worktree.glob(".git/refs/tags/**"))


def test_approval_pauses_without_checkpoint(tmp_path: Path):
    repo = make_repo(tmp_path)
    manager = manager_for(tmp_path, mode="approval")
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"needs_approval", "failed", "completed"})

    assert result["status"] == "needs_approval"
    assert result["last_checkpoint"] is None


class SequenceExecutor:
    def __init__(self):
        self.calls: list[str | None] = []

    def run(self, *, prompt, worktree, session_id, **kwargs):
        self.calls.append(session_id)
        if len(self.calls) == 1:
            return CodexResult(
                returncode=-2,
                session_id="native-session",
                context_ratio=0.8,
                context_pressure=True,
                approval_requested=False,
                cancelled=False,
                timed_out=False,
                stdout="partial implementation",
                stderr="",
            )
        Path(worktree, "marker.txt").write_text("continued\n", encoding="utf-8")
        return CodexResult(
            returncode=0,
            session_id="native-session",
            context_ratio=0.2,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="continued successfully",
            stderr="",
        )


def test_context_pressure_persists_summary_and_resumes_same_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    executor = SequenceExecutor()
    manager = RunManager(
        store=RunStore(tmp_path / "runtime"),
        executor_factory=lambda: executor,
        verification_timeout_seconds=5,
    )
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"completed", "failed", "needs_recovery"})

    assert result["status"] == "completed"
    assert executor.calls == [None, "native-session"]
    summaries = list((tmp_path / "runtime" / "logs" / run["run_id"]).glob("continuation-*.md"))
    assert summaries


def test_checkpoint_recovery_reconciles_missing_tag_idempotently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    manager = manager_for(tmp_path)
    original = runner_module.create_annotated_tag
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash before tag")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner_module, "create_annotated_tag", fail_once)
    run = manager.start(str(repo), max_iterations=1)
    failed = wait_for(manager, run["run_id"], {"needs_recovery", "completed", "failed"})
    assert failed["status"] == "needs_recovery"
    recovered = manager.recover(run["run_id"], confirm=True)
    result = wait_for(manager, run["run_id"], {"completed", "failed", "needs_recovery"})
    assert recovered["run_id"] == run["run_id"]
    assert result["status"] == "completed"
    tag = result["last_checkpoint"]["tag"]
    assert git("rev-parse", f"{tag}^{{}}", cwd=Path(result["worktree_path"]))


def test_store_locks_and_log_cursors_are_bounded(tmp_path: Path):
    store = RunStore(tmp_path / "store")
    store.acquire_repo_lock("repo-key", "run-1")
    with pytest.raises(StoreError):
        store.acquire_repo_lock("repo-key", "run-2")
    store.release_repo_lock("repo-key", "run-1")
    store.append_log("run-1", {"kind": "one"})
    store.append_log("run-1", {"kind": "two"})
    first = store.read_logs("run-1", limit=1)
    assert len(first["entries"]) == 1
    assert first["next_cursor"] == "line:1"
    second = store.read_logs("run-1", limit=1, cursor=first["next_cursor"])
    assert second["entries"][0]["kind"] == "two"


def test_stale_repository_and_worker_ownership_requires_recovery(tmp_path: Path):
    store = RunStore(tmp_path / "store")
    store.create({"run_id": "run-1", "status": "running", "worker_pid": 999999, "worker_launch_pid": 999999})
    store.acquire_repo_lock("repo-key", "run-1")
    with pytest.raises(StoreError, match="stale run"):
        store.acquire_repo_lock("repo-key", "run-2")

    worker_lock = store.worker_lock_path("run-1")
    worker_lock.write_text('{"pid": 999999, "token": "old"}', encoding="utf-8")
    store.claim_worker("run-1", "new-token")
    assert store.get("run-1")["worker_pid"] == os.getpid()
    store.release_worker("run-1", "new-token")


class BlockingExecutor:
    def run(self, *, cancel_event, **kwargs):
        cancel_event.wait(timeout=5)
        return CodexResult(
            returncode=-2,
            session_id="blocking-session",
            context_ratio=0.1,
            context_pressure=False,
            approval_requested=False,
            cancelled=cancel_event.is_set(),
            timed_out=False,
            stdout="",
            stderr="",
        )

    def cancel(self):
        return None


def test_cancel_terminates_run_without_checkpoint(tmp_path: Path):
    repo = make_repo(tmp_path)
    manager = RunManager(store=RunStore(tmp_path / "runtime"), executor_factory=BlockingExecutor)
    run = manager.start(str(repo), max_iterations=1)
    deadline = time.time() + 5
    while time.time() < deadline and manager.get(run["run_id"])["status"] == "queued":
        time.sleep(0.02)
    result = manager.cancel(run["run_id"], confirm=True)
    assert result["status"] == "cancelled"
    assert result["last_checkpoint"] is None


class WritingExecutor:
    def run(self, *, worktree, **kwargs):
        Path(worktree, "marker.txt").write_text("done\n", encoding="utf-8")
        return CodexResult(
            returncode=0,
            session_id="writing-session",
            context_ratio=0.1,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="",
            stderr="",
        )


def test_cancel_during_verification_never_checkpoints(tmp_path: Path):
    repo = make_repo(tmp_path, verification="sleep 30")
    manager = RunManager(
        store=RunStore(tmp_path / "runtime"),
        executor_factory=WritingExecutor,
        verification_timeout_seconds=60,
    )
    run = manager.start(str(repo), max_iterations=1)
    deadline = time.time() + 5
    while time.time() < deadline and manager.get(run["run_id"])["status"] != "verifying":
        time.sleep(0.02)
    result = manager.cancel(run["run_id"], confirm=True)
    assert result["status"] == "cancelled"
    worktree = Path(result["worktree_path"])
    assert "- [ ] T001" in (worktree / "PLAN.md").read_text(encoding="utf-8")
    assert result["last_checkpoint"] is None


def test_verification_artifacts_are_not_staged(tmp_path: Path):
    repo = make_repo(tmp_path, verification="echo generated > verification-artifact.log && test -f marker.txt")
    manager = RunManager(store=RunStore(tmp_path / "runtime"), executor_factory=WritingExecutor)
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"completed", "failed", "needs_recovery"})
    assert result["status"] == "completed"
    worktree = Path(result["worktree_path"])
    assert (worktree / "verification-artifact.log").is_file()
    assert "verification-artifact.log" not in git("show", "--format=", "--name-only", "HEAD", cwd=worktree)


class ResumeFailureExecutor:
    def __init__(self):
        self.calls: list[str | None] = []

    def run(self, *, session_id, **kwargs):
        self.calls.append(session_id)
        if len(self.calls) == 1:
            return CodexResult(
                returncode=-2,
                session_id="resume-session",
                context_ratio=0.8,
                context_pressure=True,
                approval_requested=False,
                cancelled=False,
                timed_out=False,
                stdout="partial",
                stderr="",
            )
        return CodexResult(
            returncode=9,
            session_id="resume-session",
            context_ratio=0.2,
            context_pressure=False,
            approval_requested=False,
            cancelled=False,
            timed_out=False,
            stdout="resume failed",
            stderr="",
        )


def test_native_resume_retries_three_times_then_requires_recovery(tmp_path: Path):
    repo = make_repo(tmp_path)
    executor = ResumeFailureExecutor()
    manager = RunManager(
        store=RunStore(tmp_path / "runtime"),
        executor_factory=lambda: executor,
        resume_attempt_limit=3,
    )
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"needs_recovery", "failed", "completed"})
    assert result["status"] == "needs_recovery"
    assert executor.calls == [None, "resume-session", "resume-session", "resume-session"]


class RiskyExecutor(WritingExecutor):
    def run(self, *, worktree, **kwargs):
        result = super().run(worktree=worktree, **kwargs)
        return CodexResult(
            **{**result.__dict__, "stdout": "running git push origin main"}
        )


def test_policy_pauses_on_push_intent(tmp_path: Path):
    repo = make_repo(tmp_path)
    manager = RunManager(store=RunStore(tmp_path / "runtime"), executor_factory=RiskyExecutor)
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"needs_approval", "failed", "completed"})
    assert result["status"] == "needs_approval"
    assert result["last_checkpoint"] is None


def test_recovery_refuses_unrelated_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    manager = manager_for(tmp_path)
    original = runner_module.create_annotated_tag

    def fail_tag(*args, **kwargs):
        raise RuntimeError("simulated tag interruption")

    monkeypatch.setattr(runner_module, "create_annotated_tag", fail_tag)
    run = manager.start(str(repo), max_iterations=1)
    failed = wait_for(manager, run["run_id"], {"needs_recovery", "completed"})
    assert failed["status"] == "needs_recovery"
    worktree = Path(failed["worktree_path"])
    (worktree / "unrelated.txt").write_text("do not commit\n", encoding="utf-8")
    monkeypatch.setattr(runner_module, "create_annotated_tag", original)
    with pytest.raises(RunError, match="Unrelated worktree changes"):
        manager.recover(run["run_id"], confirm=True)
    assert "unrelated.txt" not in git("show", "--format=", "--name-only", "HEAD", cwd=worktree)


def test_recovery_rejects_conflicting_existing_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    manager = manager_for(tmp_path)
    original = runner_module.create_annotated_tag

    def fail_tag(*args, **kwargs):
        raise RuntimeError("simulated tag interruption")

    monkeypatch.setattr(runner_module, "create_annotated_tag", fail_tag)
    run = manager.start(str(repo), max_iterations=1)
    failed = wait_for(manager, run["run_id"], {"needs_recovery", "completed"})
    assert failed["status"] == "needs_recovery"
    worktree = Path(failed["worktree_path"])
    tag_name = failed["pending_checkpoint"]["tag"]
    git("tag", "-a", tag_name, failed["base_sha"], "-m", "conflicting tag", cwd=worktree)
    monkeypatch.setattr(runner_module, "create_annotated_tag", original)
    with pytest.raises(RunError, match="exact recorded checkpoint commit"):
        manager.recover(run["run_id"], confirm=True)


def test_process_worker_backend_runs_a_real_child(tmp_path: Path):
    repo = make_repo(tmp_path)
    fake_bin = install_fake_codex(tmp_path)
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
    os.environ["FAKE_CODEX_MODE"] = "success"
    manager = RunManager(
        store=RunStore(tmp_path / "runtime"),
        worker_backend="process",
        verification_timeout_seconds=5,
        codex_timeout_seconds=4,
    )
    run = manager.start(str(repo), max_iterations=1)
    result = wait_for(manager, run["run_id"], {"completed", "failed", "needs_recovery"})
    assert result["status"] == "completed"
    assert result["last_checkpoint"]["commit"]
