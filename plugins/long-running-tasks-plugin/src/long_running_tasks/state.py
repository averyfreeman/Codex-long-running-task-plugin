from __future__ import annotations

from datetime import datetime, timezone


STATUSES = {
    "queued",
    "running",
    "needs_approval",
    "verifying",
    "checkpointing",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "needs_recovery",
    "paused",
}

TERMINAL_STATUSES = {"completed", "cancelled"}
RESTARTABLE_STATUSES = {"needs_approval", "failed", "paused"}

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelling", "cancelled", "needs_recovery", "failed"},
    "running": {"running", "needs_approval", "verifying", "failed", "cancelled", "cancelling", "needs_recovery", "paused"},
    "needs_approval": {"queued", "cancelling", "cancelled", "needs_recovery"},
    "verifying": {"checkpointing", "needs_approval", "failed", "cancelled", "cancelling", "needs_recovery"},
    "checkpointing": {"running", "completed", "needs_recovery", "failed"},
    "cancelling": {"cancelled", "needs_recovery", "completed"},
    "completed": set(),
    "failed": {"queued", "cancelling", "cancelled", "needs_recovery"},
    "cancelled": {"queued"},
    "needs_recovery": {"queued", "paused", "cancelling", "failed", "cancelled", "completed"},
    "paused": {"queued", "running", "cancelling", "cancelled", "needs_recovery"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def transition(state: dict, new_status: str, *, reason: str | None = None) -> dict:
    if new_status not in STATUSES:
        raise ValueError(f"Unknown run status: {new_status}")
    old_status = state.get("status")
    if old_status not in TRANSITIONS:
        raise ValueError(f"Unknown current run status: {old_status}")
    if new_status not in TRANSITIONS[old_status]:
        raise ValueError(f"Invalid run transition {old_status} -> {new_status}")
    state["status"] = new_status
    state["updated_at"] = utc_now()
    if reason is not None:
        state["reason"] = reason
    return state


def touch(state: dict) -> dict:
    state["updated_at"] = utc_now()
    return state


def initial_state(
    *,
    run_id: str,
    repo_path: str,
    worktree_path: str,
    branch: str,
    task_digest: str,
    plan_digest: str,
    approval_mode: str,
    base_sha: str,
    task_snapshot_path: str | None = None,
) -> dict:
    now = utc_now()
    return {
        "version": 2,
        "run_id": run_id,
        "repo_path": repo_path,
        "worktree_path": worktree_path,
        "branch": branch,
        "base_sha": base_sha,
        "status": "queued",
        "approval_mode": approval_mode,
        "task_digest": task_digest,
        "task_snapshot_path": task_snapshot_path,
        "plan_digest": plan_digest,
        "current_item": None,
        "iteration": 0,
        "session_id": None,
        "continuation_count": 0,
        "resume_attempts": 0,
        "pending_checkpoint": None,
        "last_checkpoint": None,
        "created_at": now,
        "updated_at": now,
    }
