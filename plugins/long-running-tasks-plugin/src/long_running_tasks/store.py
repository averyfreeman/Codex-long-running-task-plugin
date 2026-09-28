from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fcntl

from .state import utc_now

if TYPE_CHECKING:
    from .workflow import WorkflowRecord


class StoreError(RuntimeError):
    """Raised for runtime-store and repository-lock failures."""


def default_data_dir() -> Path:
    configured = os.environ.get("LONG_RUNNING_TASKS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex" / "long-running-tasks").resolve()


def atomic_write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or default_data_dir())
        self.runs_dir = self.root / "runs"
        self.logs_dir = self.root / "logs"
        self.locks_dir = self.root / "locks"
        self.workers_dir = self.root / "workers"
        self.snapshots_dir = self.root / "snapshots"
        self.workflows_dir = self.root / "workflows"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.workflow_projections_dir = self.root / "workflow-projections"

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def log_path(self, run_id: str) -> Path:
        directory = self.logs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "events.jsonl"

    def continuation_path(self, run_id: str, sequence: int) -> Path:
        directory = self.logs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"continuation-{sequence:03d}.md"

    def snapshot_path(self, run_id: str) -> Path:
        return self.snapshots_dir / f"{run_id}.TASK.md"

    def workflow_projection_path(self, run_id: str) -> Path:
        """Return the external immutable projection path for a workflow run."""
        return self.workflow_projections_dir / f"{run_id}.json"

    execution_projection_path = workflow_projection_path

    def write_workflow_projection(self, run_id: str, projection: Any) -> Path:
        """Persist one workflow execution projection without overwriting it.

        The projection is runtime data, not a repository input.  Keeping it
        under the external run store means the base checkout remains untouched
        and a later source-record update cannot rewrite what the run saw.
        """
        path = self.workflow_projection_path(run_id)
        payload = projection.to_dict() if hasattr(projection, "to_dict") else projection
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise StoreError(f"Unable to read workflow execution projection: {path}") from exc
            if existing != encoded:
                raise StoreError(
                    f"Immutable workflow execution projection already exists with different content: {path}"
                )
            return path
        descriptor = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            # A concurrent idempotent writer may have won the race.  It is
            # safe only when the bytes are exactly the same projection.
            if path.read_text(encoding="utf-8") != encoded:
                raise StoreError(
                    f"Immutable workflow execution projection already exists with different content: {path}"
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return path

    write_execution_projection = write_workflow_projection

    def read_workflow_projection(self, run_id: str) -> dict:
        path = self.workflow_projection_path(run_id)
        if not path.is_file():
            raise StoreError(f"Workflow execution projection does not exist: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Corrupt workflow execution projection: {path}") from exc

    read_execution_projection = read_workflow_projection

    def workflow_path(self, workflow_id: str) -> Path:
        """Return the canonical local path for a workflow record."""
        return self.workflows_dir / f"{workflow_id}.json"

    def create_workflow(self, record: "WorkflowRecord") -> "WorkflowRecord":
        """Persist a validated workflow through the workflow store boundary."""
        from .workflow import WorkflowStore

        return WorkflowStore(self.root).create(record)

    def write_workflow(
        self,
        record: "WorkflowRecord",
        *,
        expected_revision: int | None = None,
    ) -> "WorkflowRecord":
        """Atomically update a canonical workflow record."""
        from .workflow import WorkflowStore

        return WorkflowStore(self.root).save(record, expected_revision=expected_revision)

    def get_workflow(self, workflow_id: str) -> "WorkflowRecord":
        """Read and validate a canonical workflow record."""
        from .workflow import WorkflowStore

        return WorkflowStore(self.root).get(workflow_id)

    def list_all_workflows(self) -> list["WorkflowRecord"]:
        """Read all canonical workflow records from the local store."""
        from .workflow import WorkflowStore

        return WorkflowStore(self.root).list_all()

    def list_workflows(
        self,
        repo_path: str | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        """List bounded canonical workflow records using an opaque cursor."""
        from .workflow_control import WorkflowController

        return WorkflowController(self).list(repo_path, limit=limit, cursor=cursor)

    def retention_preview(self, **kwargs: Any) -> dict:
        """Preview a confirmation-gated runtime retention policy."""
        from .retention import RetentionPolicy, preview_retention

        policy = kwargs.pop("policy", None) or RetentionPolicy(
            max_age_seconds=kwargs.pop("max_age_seconds", None),
            max_runs=kwargs.pop("max_runs", None),
        )
        if not isinstance(policy, RetentionPolicy):
            policy = RetentionPolicy.from_dict(policy)
        return preview_retention(self, policy, **kwargs)

    def prune_runs(self, **kwargs: Any) -> dict:
        """Apply runtime retention only when the caller supplies confirm."""
        from .retention import RetentionPolicy, prune_retention

        policy = kwargs.pop("policy", None) or RetentionPolicy(
            max_age_seconds=kwargs.pop("max_age_seconds", None),
            max_runs=kwargs.pop("max_runs", None),
        )
        if not isinstance(policy, RetentionPolicy):
            policy = RetentionPolicy.from_dict(policy)
        return prune_retention(self, policy, **kwargs)

    prune = prune_runs

    def journal_path(self, run_id: str) -> Path:
        directory = self.logs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "checkpoint.json"

    def heartbeat_path(self, run_id: str) -> Path:
        return self.workers_dir / f"{run_id}.heartbeat.json"

    def worker_lock_path(self, run_id: str) -> Path:
        return self.workers_dir / f"{run_id}.lock"

    def checkpoint_lock_path(self, run_id: str) -> Path:
        return self.workers_dir / f"{run_id}.checkpoint.lock"

    @contextmanager
    def checkpoint_guard(self, run_id: str):
        """Serialize cancellation with the checkpoint commit/tag boundary."""
        path = self.checkpoint_lock_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def write(self, run: dict) -> None:
        run = dict(run)
        run["updated_at"] = utc_now()
        atomic_write_json(self.run_path(run["run_id"]), run)

    def create(self, run: dict) -> None:
        path = self.run_path(run["run_id"])
        if path.exists():
            raise StoreError(f"Run already exists: {run['run_id']}")
        self.write(run)

    def list_all_runs(self) -> list[dict]:
        runs: list[dict] = []
        for path in self.runs_dir.glob("*.json"):
            try:
                runs.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return runs

    def get(self, run_id: str) -> dict:
        path = self.run_path(run_id)
        if not path.is_file():
            raise StoreError(f"Unknown run: {run_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Corrupt run record: {path}") from exc

    def append_log(self, run_id: str, event: dict) -> None:
        path = self.log_path(run_id)
        entry = {"sequence": self._next_log_sequence(path), "timestamp": utc_now(), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def write_snapshot(self, run_id: str, text: str, digest: str) -> Path:
        path = self.snapshot_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise StoreError(f"Immutable task snapshot already exists with different content: {path}")
            return path
        path.write_text(text, encoding="utf-8")
        atomic_write_json(path.with_suffix(path.suffix + ".json"), {"run_id": run_id, "digest": digest})
        return path

    def write_journal(self, run_id: str, journal: dict) -> None:
        atomic_write_json(self.journal_path(run_id), journal)

    def read_journal(self, run_id: str) -> dict:
        path = self.journal_path(run_id)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Corrupt checkpoint journal: {path}") from exc

    @staticmethod
    def pid_alive(pid: object) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def worker_alive(self, run: dict) -> bool:
        return self.pid_alive(run.get("worker_pid")) or self.pid_alive(run.get("worker_launch_pid"))

    def write_heartbeat(self, run_id: str, pid: int, token: str) -> None:
        atomic_write_json(
            self.heartbeat_path(run_id),
            {"run_id": run_id, "pid": pid, "token": token, "heartbeat_at": utc_now()},
        )

    def read_heartbeat(self, run_id: str) -> dict | None:
        path = self.heartbeat_path(run_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def claim_worker(self, run_id: str, token: str) -> None:
        path = self.worker_lock_path(run_id)
        owner = {"run_id": run_id, "pid": os.getpid(), "token": token, "created_at": utc_now()}
        for attempt in range(2):
            descriptor = None
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptor = None
                    json.dump(owner, handle)
                break
            except FileExistsError as exc:
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as decode_exc:
                    raise StoreError(f"Worker lock is corrupt: {path}") from decode_exc
                if self.pid_alive(current.get("pid")):
                    raise StoreError(f"Run {run_id} already has an active worker") from exc
                if attempt == 1:
                    raise StoreError(f"Unable to reclaim stale worker lock: {path}") from exc
                path.unlink(missing_ok=True)
            finally:
                if descriptor is not None:
                    os.close(descriptor)

        run = self.get(run_id)
        existing_pid = run.get("worker_pid")
        if existing_pid and int(existing_pid) != os.getpid() and self.pid_alive(existing_pid):
            path.unlink(missing_ok=True)
            raise StoreError(f"Run {run_id} already has an active worker")
        run["worker_pid"] = os.getpid()
        run["worker_token"] = token
        run["worker_started_at"] = run.get("worker_started_at") or utc_now()
        run["worker_launch_pid"] = None
        self.write(run)
        self.write_heartbeat(run_id, os.getpid(), token)

    def record_worker_spawn(self, run_id: str, token: str, pid: int) -> None:
        run = self.get(run_id)
        if run.get("worker_token") != token:
            return
        if not run.get("worker_pid"):
            run["worker_pid"] = pid
            self.write(run)

    def release_worker(self, run_id: str, token: str) -> None:
        path = self.worker_lock_path(run_id)
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
            if current.get("token") == token:
                path.unlink(missing_ok=True)
        self.heartbeat_path(run_id).unlink(missing_ok=True)
        try:
            run = self.get(run_id)
        except StoreError:
            return
        if run.get("worker_token") == token:
            run["worker_pid"] = None
            run["worker_token"] = None
            run["worker_started_at"] = None
            run["worker_launch_pid"] = None
            self.write(run)

    @staticmethod
    def _next_log_sequence(path: Path) -> int:
        if not path.exists():
            return 1
        last = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    last = max(last, int(json.loads(line).get("sequence", 0)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        return last + 1

    def read_logs(self, run_id: str, *, limit: int = 100, cursor: str | None = None) -> dict:
        limit = max(1, min(int(limit), 200))
        start = 0
        if cursor:
            if not cursor.startswith("line:"):
                raise StoreError("Invalid log cursor")
            start = max(0, int(cursor.removeprefix("line:")))
        path = self.log_path(run_id)
        rows: list[dict] = []
        handle_context = path.open("r", encoding="utf-8") if path.exists() else _empty_file()
        with handle_context as handle:
            for index, line in enumerate(handle):
                if index < start:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"sequence": index + 1, "message": line.rstrip("\n")})
                if len(rows) >= limit:
                    break
        next_cursor = f"line:{start + len(rows)}" if len(rows) == limit else None
        return {"run_id": run_id, "entries": rows, "next_cursor": next_cursor}

    def list_runs(self, repo_path: str | None = None, *, limit: int = 20, cursor: str | None = None) -> dict:
        limit = max(1, min(int(limit), 100))
        offset = 0
        if cursor:
            if not cursor.startswith("offset:"):
                raise StoreError("Invalid run cursor")
            offset = max(0, int(cursor.removeprefix("offset:")))
        runs: list[dict] = []
        for run in self.list_all_runs():
            if repo_path and Path(run.get("repo_path", "")).resolve() != Path(repo_path).resolve():
                continue
            runs.append(run)
        runs.sort(key=lambda value: value.get("updated_at", ""), reverse=True)
        page = runs[offset : offset + limit]
        next_cursor = f"offset:{offset + len(page)}" if offset + len(page) < len(runs) else None
        return {"runs": page, "next_cursor": next_cursor}

    def acquire_repo_lock(self, repo_key: str, run_id: str) -> None:
        path = self.locks_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, repo_key)}.json"
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raise StoreError(f"Repository lock is corrupt: {path}")
            if current.get("run_id") == run_id:
                return
            if current.get("run_id") != run_id:
                try:
                    existing = self.get(current.get("run_id", ""))
                except StoreError:
                    existing = {"status": "unknown"}
                if existing.get("status") not in {"completed", "cancelled"}:
                    if self.worker_alive(existing):
                        raise StoreError(f"Repository is already owned by active run {current.get('run_id')}")
                    raise StoreError(
                        f"Repository is owned by stale run {current.get('run_id')}; recover it before starting another run"
                    )
                path.unlink(missing_ok=True)
        descriptor = None
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                json.dump(
                    {"repo_key": repo_key, "run_id": run_id, "owner_pid": os.getpid(), "created_at": utc_now()},
                    handle,
                )
        except FileExistsError as exc:
            raise StoreError(f"Repository is already locked: {repo_key}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def release_repo_lock(self, repo_key: str, run_id: str) -> None:
        path = self.locks_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, repo_key)}.json"
        if not path.exists():
            return
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if current.get("run_id") == run_id:
            path.unlink(missing_ok=True)


class _empty_file:
    def __enter__(self):
        return iter(())

    def __exit__(self, exc_type, exc, tb):
        return False
