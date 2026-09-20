from __future__ import annotations

import os
import subprocess
from pathlib import Path
from collections.abc import Iterable


class GitError(RuntimeError):
    """Raised when a Git precondition or operation fails."""


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed ({result.returncode}): {message}")
    return result


def repository_root(path: Path) -> Path:
    result = git(Path(path), "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def repository_key(path: Path) -> str:
    result = git(Path(path), "rev-parse", "--git-common-dir")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (Path(path) / common).resolve()
    return str(common.resolve())


def current_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def current_branch(repo: Path) -> str:
    return git(repo, "branch", "--show-current").stdout.strip() or "(detached)"


def clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain=v1").stdout == ""


def status_lines(repo: Path) -> list[str]:
    return [line for line in git(repo, "status", "--porcelain=v1").stdout.splitlines() if line]


def create_worktree(repo: Path, worktree: Path, branch: str, base_sha: str) -> None:
    if worktree.exists():
        raise GitError(f"Worktree path already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)


def remove_worktree(repo: Path, worktree: Path, branch: str | None = None) -> None:
    """Remove one runner-owned worktree and, optionally, its runner-owned branch."""
    result = git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    if result.returncode != 0 and worktree.exists():
        raise GitError(f"Unable to remove retained worktree: {worktree}")
    if branch:
        git(repo, "branch", "-D", branch, check=False)


def stage_and_commit(repo: Path, message: str, paths: Iterable[str]) -> str:
    """Stage only the supplied relative paths and create a checkpoint commit."""
    normalized = sorted({str(path).replace("\\", "/") for path in paths if str(path)})
    if not normalized:
        raise GitError("No paths available for checkpoint commit")
    git(repo, "add", "--", *normalized)
    staged = git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        raise GitError("No changes available for checkpoint commit")
    git(repo, "commit", "-m", message)
    return current_sha(repo)


def commit_subject(repo: Path, revision: str = "HEAD") -> str:
    return git(repo, "show", "-s", "--format=%s", revision).stdout.strip()


def tag_target(repo: Path, tag_name: str) -> str | None:
    result = git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag_name}^{{}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def create_annotated_tag(repo: Path, tag_name: str, message: str, target: str) -> None:
    if tag_target(repo, tag_name) is not None:
        raise GitError(f"Tag already exists: {tag_name}")
    git(repo, "tag", "-a", tag_name, target, "-m", message)


def changed_paths(repo: Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for line in status_lines(repo):
        code = line[:2].strip() or "?"
        path = line[3:] if len(line) > 3 else ""
        results.append((code, path))
    return results


def has_changes(repo: Path) -> bool:
    return bool(status_lines(repo))
