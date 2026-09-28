"""Confirmation-gated retention for external run artifacts.

Retention is deliberately conservative.  Active, failed, interrupted, and
recovery-needed runs are never candidates for automatic removal.  A caller
may preview a policy without changing state and must explicitly confirm a
prune operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .git_ops import GitError, remove_worktree
from .safety import BranchWriteSafetyError, assert_run_write_safe
from .state import TERMINAL_STATUSES

if TYPE_CHECKING:
    from .store import RunStore


PROTECTED_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "needs_approval",
        "verifying",
        "checkpointing",
        "cancelling",
        "failed",
        "interrupted",
        "needs_recovery",
        "paused",
    }
)


class RetentionError(RuntimeError):
    """Raised when a retention policy is unsafe or cannot be applied."""


def _status_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values}))


@dataclass(frozen=True)
class RetentionPolicy:
    """A bounded policy for terminal runtime records.

    ``max_runs`` counts the newest terminal records that remain retained; it
    does not count protected active or recoverable records.  ``max_age`` and
    ``max_runs`` are unioned, so either bound can make an old terminal record a
    candidate.  Protected statuses are an invariant, not a caller override.
    """

    max_age_seconds: float | None = None
    max_runs: int | None = None
    retain_statuses: tuple[str, ...] = field(
        default_factory=lambda: _status_tuple(PROTECTED_RUN_STATUSES)
    )
    remove_workflow_projections: bool = False
    remove_worktrees: bool = False

    def __post_init__(self) -> None:
        if self.max_age_seconds is not None:
            try:
                age = float(self.max_age_seconds)
            except (TypeError, ValueError) as exc:
                raise RetentionError("max_age_seconds must be a non-negative number") from exc
            if age < 0:
                raise RetentionError("max_age_seconds must be non-negative")
            object.__setattr__(self, "max_age_seconds", age)
        if self.max_runs is not None:
            try:
                runs = int(self.max_runs)
            except (TypeError, ValueError) as exc:
                raise RetentionError("max_runs must be a non-negative integer") from exc
            if runs < 0:
                raise RetentionError("max_runs must be non-negative")
            object.__setattr__(self, "max_runs", runs)
        statuses = _status_tuple(self.retain_statuses)
        missing = PROTECTED_RUN_STATUSES - set(statuses)
        if missing:
            raise RetentionError(
                "retention cannot remove recoverable or active statuses: "
                + ", ".join(sorted(missing))
            )
        object.__setattr__(self, "retain_statuses", statuses)

    @property
    def keep_last(self) -> int | None:
        return self.max_runs

    @property
    def max_age(self) -> float | None:
        return self.max_age_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_age_seconds": self.max_age_seconds,
            "max_runs": self.max_runs,
            "retain_statuses": list(self.retain_statuses),
            "remove_workflow_projections": self.remove_workflow_projections,
            "remove_worktrees": self.remove_worktrees,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "RetentionPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise RetentionError("retention policy must be a JSON object")
        raw_statuses = value.get("retain_statuses", PROTECTED_RUN_STATUSES)
        if isinstance(raw_statuses, str):
            raw_statuses = (raw_statuses,)
        return cls(
            max_age_seconds=value.get("max_age_seconds", value.get("max_age")),
            max_runs=value.get("max_runs", value.get("keep_last")),
            retain_statuses=tuple(raw_statuses),
            remove_workflow_projections=bool(value.get("remove_workflow_projections", False)),
            remove_worktrees=bool(value.get("remove_worktrees", False)),
        )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches_repo(run: Mapping[str, Any], repo_path: str | Path | None) -> bool:
    if not repo_path:
        return True
    try:
        return Path(str(run.get("repo_path", ""))).expanduser().resolve() == Path(repo_path).expanduser().resolve()
    except OSError:
        return False


def _retained_item(run: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "run_id": str(run.get("run_id", "")),
        "status": run.get("status"),
        "updated_at": run.get("updated_at"),
        "reason": reason,
        "workflow_id": run.get("workflow_id"),
    }


def _candidate_item(run: Mapping[str, Any], reason: str) -> dict[str, Any]:
    item = _retained_item(run, reason)
    item["worktree_path"] = run.get("worktree_path")
    return item


def preview_retention(
    store: "RunStore",
    policy: RetentionPolicy,
    *,
    repo_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic retention plan without deleting anything."""

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (
        current_time - timedelta(seconds=float(policy.max_age_seconds))
        if policy.max_age_seconds is not None
        else None
    )
    eligible: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for run in store.list_all_runs():
        if not _matches_repo(run, repo_path):
            continue
        status = str(run.get("status", ""))
        if status in PROTECTED_RUN_STATUSES or status in policy.retain_statuses:
            retained.append(_retained_item(run, "protected_status"))
            continue
        if store.worker_alive(run):
            retained.append(_retained_item(run, "active_worker"))
            continue
        if status not in TERMINAL_STATUSES:
            retained.append(_retained_item(run, "non_terminal"))
            continue
        eligible.append(run)

    eligible.sort(key=lambda value: (str(value.get("updated_at", "")), str(value.get("run_id", ""))), reverse=True)
    candidates: list[dict[str, Any]] = []
    for index, run in enumerate(eligible):
        updated = _parse_timestamp(run.get("updated_at"))
        old = cutoff is not None and (updated is None or updated < cutoff)
        exceeds_count = policy.max_runs is not None and index >= policy.max_runs
        if old or exceeds_count:
            reasons = []
            if old:
                reasons.append("max_age")
            if exceeds_count:
                reasons.append("max_runs")
            candidates.append(_candidate_item(run, "+".join(reasons)))
        else:
            retained.append(_retained_item(run, "within_policy"))

    return {
        "policy": policy.to_dict(),
        "candidates": candidates,
        "retained": sorted(retained, key=lambda value: str(value.get("run_id", ""))),
        "candidate_count": len(candidates),
    }


def _remove_directory_contents(directory: Path) -> None:
    """Remove one exact run directory, never an unresolved parent glob."""

    if not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink():
        directory.unlink()
        return
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            path.rmdir()
    directory.rmdir()


def _remove_run_artifacts(
    store: "RunStore",
    run: Mapping[str, Any],
    *,
    remove_workflow_projection: bool,
    remove_worktrees: bool,
) -> list[str]:
    run_id = str(run.get("run_id", ""))
    if not run_id or Path(run_id).name != run_id:
        raise RetentionError(f"Unsafe run ID in retention candidate: {run_id!r}")
    if remove_worktrees and run.get("worktree_path"):
        try:
            assert_run_write_safe(run, store_root=store.root)
            repo_path = Path(str(run["repo_path"])).expanduser().resolve()
            worktree_path = Path(str(run["worktree_path"])).expanduser().resolve()
            remove_worktree(repo_path, worktree_path, str(run.get("branch", "")))
        except (BranchWriteSafetyError, GitError, OSError) as exc:
            raise RetentionError(f"Unable to remove runner worktree for {run_id}: {exc}") from exc
    paths = [
        store.run_path(run_id),
        store.snapshot_path(run_id),
        store.snapshot_path(run_id).with_suffix(store.snapshot_path(run_id).suffix + ".json"),
        store.logs_dir / run_id,
        store.workers_dir / f"{run_id}.heartbeat.json",
        store.workers_dir / f"{run_id}.lock",
        store.workers_dir / f"{run_id}.checkpoint.lock",
    ]
    if remove_workflow_projection:
        paths.append(store.workflow_projection_path(run_id))
    removed: list[str] = []
    for path in paths:
        if path.is_dir():
            _remove_directory_contents(path)
            removed.append(str(path))
        elif path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(str(path))

    if remove_worktrees and run.get("worktree_path"):
        removed.append(str(Path(str(run["worktree_path"])).expanduser().resolve()))
    return removed


def prune_retention(
    store: "RunStore",
    policy: RetentionPolicy,
    *,
    repo_path: str | Path | None = None,
    confirm: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a previewed plan only after explicit confirmation."""

    if not confirm:
        raise RetentionError("Pruning runtime artifacts requires confirm=true")
    plan = preview_retention(store, policy, repo_path=repo_path, now=now)
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for item in plan["candidates"]:
        try:
            run = store.get(str(item["run_id"]))
            removed.extend(
                _remove_run_artifacts(
                    store,
                    run,
                    remove_workflow_projection=policy.remove_workflow_projections,
                    remove_worktrees=policy.remove_worktrees,
                )
            )
            item["removed"] = True
        except (RetentionError, OSError) as exc:
            errors.append({"run_id": str(item["run_id"]), "error": str(exc)})
            item["removed"] = False
    return {
        **plan,
        "removed": [str(item["run_id"]) for item in plan["candidates"] if item.get("removed")],
        "removed_paths": removed,
        "errors": errors,
    }


__all__ = [
    "PROTECTED_RUN_STATUSES",
    "RetentionError",
    "RetentionPolicy",
    "preview_retention",
    "prune_retention",
]
