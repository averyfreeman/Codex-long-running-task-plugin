"""Fail-closed guards for writes that belong to an isolated runner branch.

The runner is allowed to write its own worktree and external runtime store.
It is never allowed to use the base checkout as a write target.  Keeping this
check in a small dependency-free module gives the CLI, MCP boundary, and
runner the same ownership rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping


RUNNER_BRANCH_PREFIX = "codex/long-running/"


class BranchWriteSafetyError(RuntimeError):
    """Raised when a requested write is outside a runner-owned worktree."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def assert_branch_write_safe(
    *,
    repo_path: str | Path,
    worktree_path: str | Path,
    branch: str,
    store_root: str | Path | None = None,
    target_path: str | Path | None = None,
) -> Path:
    """Validate one branch-owned write target and return its resolved path.

    ``repo_path`` is the base checkout and ``worktree_path`` is the isolated
    checkout.  A runner branch must use the runner namespace and, when the
    runtime store is supplied, its worktree must live below the store's
    ``worktrees`` directory.  Symlink resolution is intentional: a path that
    appears inside the worktree but resolves outside it is rejected.
    """

    base = _resolved(repo_path)
    worktree = _resolved(worktree_path)
    if worktree == base:
        raise BranchWriteSafetyError(
            "branch-owned writes cannot target the base checkout"
        )
    if not isinstance(branch, str) or not branch.startswith(RUNNER_BRANCH_PREFIX):
        raise BranchWriteSafetyError(
            f"branch-owned writes require a runner-owned branch under {RUNNER_BRANCH_PREFIX!r}"
        )
    if store_root is not None:
        owned_root = (_resolved(store_root) / "worktrees").resolve()
        if not worktree.is_relative_to(owned_root):
            raise BranchWriteSafetyError(
                "branch-owned writes require a runner-owned worktree in the runtime store"
            )

    target = worktree if target_path is None else _resolved(target_path)
    if target != worktree and not target.is_relative_to(worktree):
        raise BranchWriteSafetyError(
            "branch-owned write target must remain inside the runner worktree"
        )
    return target


def assert_run_write_safe(
    run: Mapping[str, object],
    *,
    store_root: str | Path | None = None,
    target_path: str | Path | None = None,
) -> Path:
    """Apply :func:`assert_branch_write_safe` to a persisted run mapping."""

    missing = [field for field in ("repo_path", "worktree_path", "branch") if not run.get(field)]
    if missing:
        raise BranchWriteSafetyError(
            f"run is missing branch-write ownership fields: {', '.join(missing)}"
        )
    return assert_branch_write_safe(
        repo_path=str(run["repo_path"]),
        worktree_path=str(run["worktree_path"]),
        branch=str(run["branch"]),
        store_root=store_root,
        target_path=target_path,
    )


class BranchWriteGuard:
    """Reusable guard for code that performs more than one worktree write."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        worktree_path: str | Path,
        branch: str,
        store_root: str | Path | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.worktree_path = worktree_path
        self.branch = branch
        self.store_root = store_root
        assert_branch_write_safe(
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch=branch,
            store_root=store_root,
        )

    def target(self, target_path: str | Path) -> Path:
        return assert_branch_write_safe(
            repo_path=self.repo_path,
            worktree_path=self.worktree_path,
            branch=self.branch,
            store_root=self.store_root,
            target_path=target_path,
        )


validate_runner_write_target = assert_branch_write_safe
ensure_branch_owned_write = assert_branch_write_safe


__all__ = [
    "BranchWriteGuard",
    "BranchWriteSafetyError",
    "RUNNER_BRANCH_PREFIX",
    "assert_branch_write_safe",
    "assert_run_write_safe",
    "ensure_branch_owned_write",
    "validate_runner_write_target",
]
