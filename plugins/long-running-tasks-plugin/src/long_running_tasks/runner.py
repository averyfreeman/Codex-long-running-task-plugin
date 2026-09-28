from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .codex_exec import (
    APPROVAL_MODES,
    CodexError,
    CodexExecutor,
    CodexResult,
    build_exec_command,
    build_resume_command,
    preflight_command,
)
from .git_ops import (
    GitError,
    changed_paths,
    clean,
    commit_subject,
    create_annotated_tag,
    create_worktree,
    current_branch,
    current_sha,
    git,
    has_changes,
    repository_key,
    repository_root,
    remove_worktree,
    stage_and_commit,
    tag_target,
)
from .state import RESTARTABLE_STATUSES, TERMINAL_STATUSES, initial_state, transition, utc_now
from .store import RunStore, StoreError, atomic_write_json
from .safety import BranchWriteSafetyError, assert_run_write_safe
from .task_spec import PlanSpec, TaskSpec, TaskSpecError, mark_item_complete, parse_plan, parse_task
from .workflow import (
    Provenance,
    WorkflowEdge,
    WorkflowNode,
    WorkflowRecord,
    WorkflowStoreError,
    WorkflowTransitionError,
    WorkflowValidationError,
    validate_workflow_record,
)
from .workflow_run import WorkflowExecutionProjection


class RunError(RuntimeError):
    """Raised when a run cannot be started or resumed safely."""


RISK_PATH_PARTS = {
    ".github",
    ".gitlab",
    "infra",
    "terraform",
    "deploy",
    "deployment",
    "migrations",
    "secrets",
    "auth",
}
RISK_SUFFIXES = {".env", ".pem", ".key", ".p12"}
APPROVAL_SIGNAL_RE = re.compile(r"(?:RUNNER_STATUS|CODEX_RUNNER)\s*[:=]\s*(?:NEEDS_APPROVAL|AMBIGUOUS)", re.IGNORECASE)
RISK_COMMAND_RE = re.compile(
    r"(?:\bgit\s+(?:push|merge|rebase|reset|clean)\b|"
    r"\b(?:rm|sudo|chmod|chown)\s+|"
    r"\b(?:terraform\s+destroy|kubectl\s+delete|drop\s+(?:database|table))\b|"
    r"(?<![\w-])(?:publish|release)(?=\s|$|[;&|]))",
    re.IGNORECASE,
)
RISK_OUTPUT_RE = re.compile(
    r"(?:\bgit\s+(?:push|merge|rebase|reset|clean)\b|"
    r"\b(?:rm|sudo|chmod|chown)\s+|"
    r"\b(?:terraform\s+destroy|kubectl\s+delete|drop\s+(?:database|table))\b)",
    re.IGNORECASE,
)
ACTIVE_STATUSES = {"queued", "running", "verifying", "checkpointing", "cancelling"}
MAX_RESUME_ATTEMPTS = 3


def _plan_shape_digest(text: str) -> str:
    normalized = re.sub(r"^(\- \[) [xX](\])", r"\1 \2", text, flags=re.MULTILINE)
    normalized = re.sub(r"^(\- \[)[xX](\])", r"\1 \2", normalized, flags=re.MULTILINE)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state_path(run: dict) -> Path:
    return Path(run["state_path"])


def _load_state(run: dict) -> dict:
    path = _state_path(run)
    if not path.is_file():
        raise RunError(f"Missing versioned state file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunError(f"Corrupt versioned state file: {path}") from exc


def _write_state(run: dict, state: dict) -> None:
    atomic_write_json(_state_path(run), state)


def _implementation_paths(worktree: Path) -> list[tuple[str, str]]:
    return [(code, path) for code, path in changed_paths(worktree) if path != ".codex-loop/state.json"]


def _relative_paths(paths: list[tuple[str, str]]) -> list[str]:
    values: set[str] = set()
    for _, path in paths:
        normalized = path.replace("\\", "/").strip()
        if not normalized or normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise RunError(f"Unsafe repository path in checkpoint manifest: {path}")
        values.add(normalized)
    return sorted(values)


def _risk_flags(worktree: Path, result: CodexResult | None = None) -> list[str]:
    flags: list[str] = []
    for code, path in changed_paths(worktree):
        normalized = path.replace("\\", "/")
        parts = set(normalized.split("/"))
        suffix = Path(normalized).suffix.casefold()
        if "D" in code:
            flags.append(f"deleted:{normalized}")
        if code == "?" and normalized.startswith(".git/"):
            flags.append(f"git-internal:{normalized}")
        if parts & RISK_PATH_PARTS:
            flags.append(f"sensitive-path:{normalized}")
        if suffix in RISK_SUFFIXES or Path(normalized).name.startswith(".env"):
            flags.append(f"secret-like-path:{normalized}")
    if result:
        if APPROVAL_SIGNAL_RE.search(result.stdout) or APPROVAL_SIGNAL_RE.search(result.stderr):
            flags.append("explicit-runner-approval-signal")
        if RISK_OUTPUT_RE.search(result.stdout) or RISK_OUTPUT_RE.search(result.stderr):
            flags.append("risky-command-intent")
        for event in result.events:
            item = event.get("item") if isinstance(event, dict) else None
            command = item.get("command") if isinstance(item, dict) else None
            if isinstance(command, str) and RISK_COMMAND_RE.search(command):
                flags.append("risky-tool-event")
    return sorted(set(flags))


def _verify(
    worktree: Path,
    command: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str, str, bool]:
    process = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    deadline = time.monotonic() + timeout_seconds
    cancelled = False
    timed_out = False
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.05)
    if cancelled or timed_out:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=5)
    return process.returncode if not timed_out else 124, stdout, stderr, timed_out


class RunManager:
    def __init__(
        self,
        *,
        store: RunStore | None = None,
        executor_factory: Callable[[], CodexExecutor] | None = None,
        worker_backend: str | None = None,
        verification_timeout_seconds: float = 900,
        codex_timeout_seconds: float = 1800,
        context_budget_tokens: int = 100_000,
        context_threshold: float = 0.80,
        heartbeat_interval_seconds: float = 2.0,
        resume_attempt_limit: int = MAX_RESUME_ATTEMPTS,
    ):
        self.store = store or RunStore()
        self.worker_backend = worker_backend or ("inline" if executor_factory else "process")
        self._preflight_enabled = executor_factory is None
        if self.worker_backend not in {"inline", "process"}:
            raise ValueError(f"Unsupported worker backend: {self.worker_backend}")
        self.executor_factory = executor_factory or (
            lambda: CodexExecutor(context_budget_tokens=context_budget_tokens, threshold=context_threshold)
        )
        self.verification_timeout_seconds = verification_timeout_seconds
        self.codex_timeout_seconds = codex_timeout_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.resume_attempt_limit = max(1, int(resume_attempt_limit))
        self._workers: dict[str, threading.Thread] = {}
        self._worker_processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._executors: dict[str, CodexExecutor] = {}
        self._lock = threading.RLock()
        if self.worker_backend == "process":
            self._reconcile_active_runs()

    def _reconcile_active_runs(self) -> None:
        for run in self.store.list_all_runs():
            if run.get("status") not in ACTIVE_STATUSES:
                continue
            if self.store.worker_alive(run):
                continue
            reason = "worker process is no longer alive; inspect the retained worktree and recover the run"
            try:
                self._set_status(run["run_id"], "needs_recovery", reason=reason)
                self.store.append_log(run["run_id"], {"kind": "recovery_required", "message": reason})
            except (KeyError, RunError, StoreError):
                continue

    def start(
        self,
        repo_path: str,
        *,
        max_iterations: int | None = None,
        approval_mode: str = "policy",
        model: str | None = None,
        workflow: WorkflowRecord | Mapping[str, Any] | str | None = None,
        workflow_id: str | None = None,
        ticket_id: str | None = None,
        workflow_record: WorkflowRecord | Mapping[str, Any] | str | None = None,
        workflow_ticket_id: str | None = None,
        _workflow_record: WorkflowRecord | None = None,
        _workflow_ticket_id: str | None = None,
    ) -> dict:
        # ``start_run`` remains the direct runner contract.  Workflow callers
        # can opt into the complementary binding through this convenience
        # spelling, while the normal path below stays unchanged for existing
        # callers and tests.
        if workflow_record is not None:
            if workflow is not None or workflow_id is not None:
                raise RunError("Provide only one of workflow, workflow_id, or workflow_record")
            workflow = workflow_record
        if workflow_ticket_id is not None:
            if ticket_id is not None:
                raise RunError("Provide only one of ticket_id or workflow_ticket_id")
            ticket_id = workflow_ticket_id
        if workflow is not None or workflow_id is not None:
            if workflow is not None and workflow_id is not None:
                raise RunError("Provide either workflow or workflow_id, not both")
            if _workflow_record is not None or _workflow_ticket_id is not None:
                raise RunError("Internal workflow binding arguments cannot be combined with workflow")
            selected_workflow = workflow if workflow is not None else workflow_id
            return self.start_workflow(
                repo_path,
                selected_workflow,
                ticket_id=ticket_id,
                max_iterations=max_iterations,
                approval_mode=approval_mode,
                model=model,
            )
        if ticket_id is not None:
            raise RunError("ticket_id requires a workflow binding")
        if (_workflow_record is None) != (_workflow_ticket_id is None):
            raise RunError("Internal workflow binding requires both a workflow record and ticket ID")
        if approval_mode not in APPROVAL_MODES:
            raise RunError(f"Unsupported approval mode: {approval_mode}")
        if max_iterations is not None and max_iterations < 1:
            raise RunError("max_iterations must be at least 1")
        root = repository_root(Path(repo_path).expanduser())
        if not clean(root):
            raise RunError("Target repository must have a clean working tree")
        task = parse_task(root / "TASK.md")
        plan = parse_plan(root / "PLAN.md")
        if plan.next_item is None:
            raise RunError("PLAN.md is already complete")
        self._preflight_codex(
            worktree=root,
            approval_mode=approval_mode,
            model=model,
            session_id=None,
        )
        repo_key = repository_key(root)
        run_id = uuid.uuid4().hex[:12]
        projection: WorkflowExecutionProjection | None = None
        projection_path: Path | None = None
        workflow_node_id: str | None = None
        if _workflow_record is not None:
            projection = WorkflowExecutionProjection.from_workflow(
                _workflow_record,
                _workflow_ticket_id,
                run_id=run_id,
            )
            projection_path = self.store.write_workflow_projection(run_id, projection)
            workflow_node_id = f"run-{run_id}"
        snapshot_path = self.store.write_snapshot(run_id, task.text, task.digest)
        self.store.acquire_repo_lock(repo_key, run_id)
        worktree = self.store.root / "worktrees" / run_id
        branch = f"codex/long-running/{run_id}"
        worktree_created = False
        try:
            base_sha = current_sha(root)
            create_worktree(root, worktree, branch, base_sha)
            worktree_created = True
            try:
                assert_run_write_safe(
                    {
                        "repo_path": str(root),
                        "worktree_path": str(worktree),
                        "branch": branch,
                    },
                    store_root=self.store.root,
                )
            except BranchWriteSafetyError as exc:
                raise RunError(f"Runner worktree failed its branch-write safety gate: {exc}") from exc
            state_path = worktree / ".codex-loop" / "state.json"
            state = initial_state(
                run_id=run_id,
                repo_path=str(root),
                worktree_path=str(worktree),
                branch=branch,
                task_digest=task.digest,
                plan_digest=plan.digest,
                approval_mode=approval_mode,
                base_sha=base_sha,
                task_snapshot_path=str(snapshot_path),
                workflow_id=projection.workflow_id if projection else None,
                workflow_node_id=workflow_node_id,
                workflow_ticket_id=projection.ticket_id if projection else None,
                workflow_projection_path=str(projection_path) if projection_path else None,
                workflow_projection_digest=projection.digest if projection else None,
            )
            _write_state({"state_path": str(state_path)}, state)
            run = {
                **state,
                "repo_key": repo_key,
                "base_branch": current_branch(root),
                "state_path": str(state_path),
                "task_snapshot_path": str(snapshot_path),
                "checkpoint_journal_path": str(self.store.journal_path(run_id)),
                "max_iterations": max_iterations,
                "model": model,
                "log_path": str(self.store.log_path(run_id)),
                "pending_checkpoint": None,
                "last_checkpoint": None,
                "cancel_requested": False,
                "worker_pid": None,
                "worker_token": None,
                "worker_launch_pid": None,
                "worker_started_at": None,
            }
            if projection is not None and projection_path is not None and workflow_node_id is not None:
                run.update(
                    {
                        "workflow_id": projection.workflow_id,
                        "workflow_node_id": workflow_node_id,
                        "workflow_run_node_id": workflow_node_id,
                        "workflow_ticket_id": projection.ticket_id,
                        "workflow_ticket_node_id": projection.ticket_id,
                        "workflow_projection_path": str(projection_path),
                        "execution_projection_path": str(projection_path),
                        "workflow_projection_digest": projection.digest,
                        "execution_projection_digest": projection.digest,
                        "workflow_record_digest": projection.workflow_digest,
                        "workflow_record_revision": projection.workflow_revision,
                        "workflow_projection": {
                            "workflow_id": projection.workflow_id,
                            "ticket_id": projection.ticket_id,
                            "digest": projection.digest,
                            "path": str(projection_path),
                        },
                    }
                )
                # Bind the canonical run node before a worker can advance the
                # status.  This update is external runtime data and never
                # touches the target repository checkout.
                updated_workflow = self._attach_workflow_run(
                    _workflow_record,
                    projection,
                    run_id=run_id,
                    run_node_id=workflow_node_id,
                    projection_path=projection_path,
                )
                run["workflow_record_revision"] = updated_workflow.revision
            self.store.create(run)
        except Exception:
            if worktree_created:
                try:
                    remove_worktree(root, worktree, branch)
                except GitError:
                    pass
            self.store.release_repo_lock(repo_key, run_id)
            raise
        self._start_worker(run_id)
        return self.store.get(run_id)

    def start_workflow(
        self,
        repo_path: str,
        workflow: WorkflowRecord | Mapping[str, Any] | str | None = None,
        *,
        workflow_id: str | None = None,
        ticket_id: str | None = None,
        workflow_record: WorkflowRecord | Mapping[str, Any] | str | None = None,
        ticket_node_id: str | None = None,
        max_iterations: int | None = None,
        approval_mode: str = "policy",
        model: str | None = None,
    ) -> dict:
        """Start the existing isolated runner from a canonical workflow.

        The workflow record is validated and persisted in the external run
        store before the worker starts.  The selected ticket is captured into
        a :class:`WorkflowExecutionProjection`; subsequent status updates
        project onto a new workflow-record revision and never rewrite source
        artifacts or the base checkout.
        """
        if workflow_record is not None:
            if workflow is not None or workflow_id is not None:
                raise RunError("Provide only one of workflow, workflow_id, or workflow_record")
            workflow = workflow_record
        if ticket_node_id is not None:
            if ticket_id is not None:
                raise RunError("Provide only one of ticket_id or ticket_node_id")
            ticket_id = ticket_node_id
        if workflow is not None and workflow_id is not None:
            raise RunError("Provide either workflow or workflow_id, not both")
        selected_workflow = workflow if workflow is not None else workflow_id
        if selected_workflow is None:
            raise RunError("start_workflow requires a workflow record or workflow_id")

        if isinstance(selected_workflow, str):
            try:
                record = self.store.get_workflow(selected_workflow)
            except (StoreError, WorkflowStoreError, WorkflowValidationError) as exc:
                raise RunError(f"Unable to load workflow {selected_workflow}: {exc}") from exc
        elif isinstance(selected_workflow, WorkflowRecord):
            record = validate_workflow_record(selected_workflow)
        elif isinstance(selected_workflow, Mapping):
            try:
                record = WorkflowRecord.from_dict(selected_workflow)
            except WorkflowValidationError as exc:
                raise RunError(f"Invalid workflow record: {exc}") from exc
        else:
            raise RunError("workflow must be a WorkflowRecord, mapping, or workflow ID")

        root = repository_root(Path(repo_path).expanduser())
        if Path(record.repo_path).expanduser().resolve() != root.resolve():
            raise RunError(
                f"Workflow {record.workflow_id} belongs to {record.repo_path}, not {root}"
            )

        # Fail closed on stale source artifacts and an empty frontier before
        # creating any local canonical-record or runner state.
        WorkflowExecutionProjection.from_workflow(record, ticket_id)

        # An object/mapping supplied by a caller becomes the canonical record
        # for this manager.  An ID always resolves to the already-persisted
        # record above.  The digest check prevents accidentally projecting a
        # different revision over an existing local workflow.
        try:
            stored = self.store.get_workflow(record.workflow_id)
        except (StoreError, WorkflowStoreError, WorkflowValidationError):
            self.store.create_workflow(record)
            stored = record
        if stored.digest != record.digest:
            raise RunError(
                f"Workflow {record.workflow_id} differs from the canonical local record"
            )
        record = stored

        # Re-capture from the canonical stored revision.  The internal start
        # call repeats this once more with the generated run_id so the
        # persisted projection is tied to exactly one run.
        projection = WorkflowExecutionProjection.from_workflow(record, ticket_id)
        return self.start(
            str(root),
            max_iterations=max_iterations,
            approval_mode=approval_mode,
            model=model,
            _workflow_record=record,
            _workflow_ticket_id=projection.ticket_id,
        )

    start_workflow_run = start_workflow
    start_from_workflow = start_workflow

    def get_workflow_projection(self, run_id: str) -> WorkflowExecutionProjection:
        """Read and validate the immutable projection bound to a workflow run."""
        run = self.store.get(run_id)
        if not run.get("workflow_projection_path") and not run.get("execution_projection_path"):
            raise RunError(f"Run {run_id} is a direct run without a workflow projection")
        try:
            return WorkflowExecutionProjection.from_dict(self.store.read_workflow_projection(run_id))
        except (OSError, StoreError, WorkflowValidationError) as exc:
            raise RunError(f"Unable to read workflow projection for run {run_id}: {exc}") from exc

    read_workflow_projection = get_workflow_projection

    def get(self, run_id: str) -> dict:
        return self.store.get(run_id)

    def list(self, repo_path: str | None = None, *, limit: int = 20, cursor: str | None = None) -> dict:
        return self.store.list_runs(repo_path, limit=limit, cursor=cursor)

    def logs(self, run_id: str, *, limit: int = 100, cursor: str | None = None) -> dict:
        self.store.get(run_id)
        return self.store.read_logs(run_id, limit=limit, cursor=cursor)

    def create_workflow(self, workflow: WorkflowRecord | Mapping[str, Any]) -> dict:
        """Create or idempotently register a canonical local workflow."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).create(workflow).to_dict()

    def get_workflow(self, workflow_id: str) -> dict:
        """Read one canonical local workflow record."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).get(workflow_id).to_dict()

    read_workflow = get_workflow

    def list_workflows(
        self,
        repo_path: str | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        """List bounded canonical workflows using an opaque cursor."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).list(repo_path, limit=limit, cursor=cursor)

    def schedule_workflow(
        self,
        workflow_id: str,
        *,
        automation_mode: str | None = None,
    ) -> dict:
        """Return the read-only serial frontier for a canonical workflow."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).schedule(
            workflow_id,
            automation_mode=automation_mode,
        )

    select_workflow_frontier = schedule_workflow

    def reconcile_workflow(
        self,
        workflow_id: str,
        *,
        confirm: bool = False,
        require_confirmation: bool = False,
    ) -> dict:
        """Audit source artifacts and optionally persist a stale transition."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).reconcile(
            workflow_id,
            confirm=confirm,
            require_confirmation=require_confirmation,
        ).to_dict()

    def retention_preview(
        self,
        *,
        repo_path: str | None = None,
        max_age_seconds: float | None = None,
        max_runs: int | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict:
        """Preview retention without deleting runtime artifacts."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).retention_preview(
            repo_path=repo_path,
            max_age_seconds=max_age_seconds,
            max_runs=max_runs,
            policy=policy,
        )

    def prune_runs(
        self,
        *,
        repo_path: str | None = None,
        max_age_seconds: float | None = None,
        max_runs: int | None = None,
        policy: Mapping[str, Any] | None = None,
        confirm: bool = False,
    ) -> dict:
        """Prune only explicitly confirmed, terminal runtime artifacts."""
        from .workflow_control import WorkflowController

        return WorkflowController(self.store).prune_runs(
            repo_path=repo_path,
            max_age_seconds=max_age_seconds,
            max_runs=max_runs,
            policy=policy,
            confirm=confirm,
        )

    prune = prune_runs

    def assert_branch_write_safe(
        self,
        run: str | Mapping[str, Any],
        *,
        target_path: str | Path | None = None,
    ) -> str:
        """Validate a persisted run's branch-owned write target."""
        selected = self.store.get(run) if isinstance(run, str) else run
        try:
            target = assert_run_write_safe(
                selected,
                store_root=self.store.root,
                target_path=target_path,
            )
        except BranchWriteSafetyError:
            raise
        return str(target)

    def _validate_run_worktree(
        self,
        run: Mapping[str, Any],
        *,
        target_path: str | Path | None = None,
    ) -> Path:
        try:
            return assert_run_write_safe(
                run,
                store_root=self.store.root,
                target_path=target_path,
            )
        except BranchWriteSafetyError as exc:
            raise RunError(f"Run branch-write safety gate rejected the target: {exc}") from exc

    def _attach_workflow_run(
        self,
        record: WorkflowRecord,
        projection: WorkflowExecutionProjection,
        *,
        run_id: str,
        run_node_id: str,
        projection_path: Path,
    ) -> WorkflowRecord:
        """Add the external run node and execution edge to a record."""
        if run_node_id in {node.node_id for node in record.nodes}:
            raise RunError(f"Workflow run node already exists: {run_node_id}")

        captured = projection.to_json()
        timestamp = projection.captured_at or utc_now()
        run_node = WorkflowNode(
            node_id=run_node_id,
            kind="run",
            status="queued",
            title=f"Run {run_id}: {projection.ticket.title}",
            artifact=str(projection_path),
            current=True,
            approved=False,
            created_at=timestamp,
            updated_at=timestamp,
            provenance=Provenance.from_text(
                f"workflow://{projection.workflow_id}/runs/{run_id}",
                captured,
                captured_at=timestamp,
                source_revision=str(projection.workflow_revision),
                metadata={
                    "projection_digest": projection.digest,
                    "workflow_id": projection.workflow_id,
                    "ticket_id": projection.ticket_id,
                },
            ),
            metadata={
                "run_id": run_id,
                "workflow_id": projection.workflow_id,
                "ticket_id": projection.ticket_id,
                "projection_path": str(projection_path),
                "projection_digest": projection.digest,
                "recoverable": True,
            },
        )
        updated = record.add_node(run_node, now=timestamp)
        if not any(
            edge.source == projection.ticket_id
            and edge.target == run_node_id
            and edge.kind == "executes"
            for edge in updated.edges
        ):
            updated = updated.add_edge(
                WorkflowEdge(
                    source=projection.ticket_id,
                    target=run_node_id,
                    kind="executes",
                    metadata={"projection_digest": projection.digest},
                ),
                now=timestamp,
            )
        if updated.status != "queued":
            try:
                updated = updated.transition(
                    "queued",
                    reason=f"workflow run {run_id} queued",
                    now=timestamp,
                )
            except WorkflowTransitionError as exc:
                raise RunError(
                    f"Workflow {record.workflow_id} cannot enter queued status from {record.status}"
                ) from exc
        try:
            return self.store.write_workflow(updated, expected_revision=record.revision)
        except (StoreError, WorkflowStoreError) as exc:
            raise RunError(f"Unable to bind run {run_id} to workflow {record.workflow_id}: {exc}") from exc

    @staticmethod
    def _workflow_status_for_run(run_status: str) -> str:
        """Map detailed runner phases onto the aggregate workflow lifecycle."""
        if run_status == "queued":
            return "queued"
        if run_status in {"running", "verifying", "checkpointing", "cancelling"}:
            return "running"
        if run_status in {"needs_approval", "paused"}:
            return "paused"
        if run_status == "failed":
            return "failed"
        if run_status == "interrupted":
            return "interrupted"
        if run_status == "needs_recovery":
            return "needs_recovery"
        if run_status == "completed":
            return "completed"
        if run_status == "cancelled":
            return "cancelled"
        raise RunError(f"Unsupported workflow projection status: {run_status}")

    def _project_workflow_status(
        self,
        run_id: str,
        run_status: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Project a runner status onto its canonical run node.

        Projection failures are logged but do not alter the direct runner's
        state machine.  The retained run record and worktree therefore remain
        inspectable/recoverable even if a concurrent workflow reconciliation
        wins the optimistic revision check.
        """
        try:
            run = self.store.get(run_id)
            workflow_id = run.get("workflow_id")
            run_node_id = run.get("workflow_node_id")
            if not workflow_id or not run_node_id:
                return
            workflow = self.store.get_workflow(str(workflow_id))
            current_node = workflow.node(str(run_node_id))
            aggregate_status = self._workflow_status_for_run(run_status)
            updated = workflow
            timestamp = utc_now()
            if current_node.status != run_status:
                try:
                    updated = updated.transition_node(
                        str(run_node_id),
                        run_status,
                        reason=reason,
                        now=timestamp,
                    )
                except WorkflowTransitionError as exc:
                    self.store.append_log(
                        run_id,
                        {
                            "kind": "workflow_projection_error",
                            "message": f"Cannot project run status {current_node.status} -> {run_status}: {exc}",
                        },
                    )
                    return
            if updated.status != aggregate_status:
                try:
                    updated = updated.transition(
                        aggregate_status,
                        reason=reason or f"run {run_id} projected {run_status}",
                        now=timestamp,
                    )
                except WorkflowTransitionError:
                    # A workflow may be terminal or explicitly stale while a
                    # retained run is being inspected.  The run node can still
                    # be projected without forcing an aggregate transition.
                    pass
            if updated != workflow:
                self.store.write_workflow(updated, expected_revision=workflow.revision)
        except (StoreError, WorkflowStoreError, WorkflowValidationError, RunError) as exc:
            try:
                self.store.append_log(
                    run_id,
                    {"kind": "workflow_projection_error", "message": str(exc)},
                )
            except StoreError:
                pass

    def project_workflow_status(
        self,
        run_id: str,
        status: str | None = None,
        *,
        reason: str | None = None,
    ) -> WorkflowRecord:
        """Synchronize one retained run's status and return its workflow."""
        run = self.store.get(run_id)
        if not run.get("workflow_id"):
            raise RunError(f"Run {run_id} is a direct run without a workflow projection")
        self._project_workflow_status(run_id, status or str(run["status"]), reason=reason)
        return self.store.get_workflow(str(run["workflow_id"]))

    sync_workflow_status = project_workflow_status
    project_run_status = project_workflow_status

    def _preflight_codex(
        self,
        *,
        worktree: Path,
        approval_mode: str,
        model: str | None,
        session_id: str | None,
    ) -> None:
        """Reject unsupported Codex commands before persistent run mutation."""
        if not self._preflight_enabled:
            return
        output_file = self.store.root / "preflight.last-message.txt"
        if session_id:
            command = build_resume_command(
                session_id=session_id,
                worktree=worktree,
                output_file=output_file,
                approval_mode=approval_mode,
                model=model,
            )
        else:
            command = build_exec_command(
                worktree=worktree,
                output_file=output_file,
                approval_mode=approval_mode,
                model=model,
            )
        try:
            preflight_command(
                command,
                approval_mode=approval_mode,
                resume=session_id is not None,
            )
        except (CodexError, OSError, subprocess.SubprocessError) as exc:
            phase = "resume" if session_id else "initial"
            raise RunError(
                f"Codex {phase} command preflight failed for approval_mode={approval_mode!r}: {exc}"
            ) from exc

    def _ensure_task_snapshot(self, run: dict) -> None:
        """Reconstruct a missing legacy snapshot only after digest verification."""
        snapshot_value = run.get("task_snapshot_path")
        snapshot_path = Path(snapshot_value) if snapshot_value else None
        if snapshot_path and snapshot_path.is_file():
            if _text_digest(snapshot_path.read_text(encoding="utf-8")) != run["task_digest"]:
                raise RunError("Immutable TASK.md snapshot is changed; refusing resume")
            return

        task_path = Path(run["worktree_path"]) / "TASK.md"
        if not task_path.is_file():
            raise RunError("Immutable TASK.md snapshot is missing and retained TASK.md is unavailable")
        task_text = task_path.read_text(encoding="utf-8")
        if _text_digest(task_text) != run["task_digest"]:
            raise RunError("Immutable TASK.md snapshot is missing and retained TASK.md does not match the recorded digest")

        snapshot_path = self.store.write_snapshot(run["run_id"], task_text, run["task_digest"])
        run["task_snapshot_path"] = str(snapshot_path)
        self.store.write(run)
        state = _load_state(run)
        state["task_snapshot_path"] = str(snapshot_path)
        state["updated_at"] = utc_now()
        _write_state(run, state)
        self.store.append_log(
            run["run_id"],
            {"kind": "recovery", "message": "Reconstructed missing legacy TASK.md snapshot after digest verification"},
        )

    def resume(self, run_id: str, *, approval_mode: str | None = None) -> dict:
        run = self.store.get(run_id)
        self._validate_run_worktree(run)
        if run["status"] not in RESTARTABLE_STATUSES:
            raise RunError(f"Run {run_id} is not resumable from status {run['status']}")
        if self.store.worker_alive(run):
            raise RunError(f"Run {run_id} already has an active worker")
        if approval_mode is not None:
            if approval_mode not in APPROVAL_MODES:
                raise RunError(f"Unsupported approval mode: {approval_mode}")
            run["approval_mode"] = approval_mode
        self._preflight_codex(
            worktree=Path(run["worktree_path"]),
            approval_mode=run["approval_mode"],
            model=run.get("model"),
            session_id=run.get("session_id"),
        )
        self._ensure_task_snapshot(run)
        self.store.acquire_repo_lock(run["repo_key"], run_id)
        state = _load_state(run)
        if run["status"] in RESTARTABLE_STATUSES and run.get("max_iterations"):
            max_iterations = int(run["max_iterations"])
            if max_iterations and int(run.get("iteration", 0)) >= max_iterations:
                run["iteration"] = max(0, int(run.get("iteration", 0)) - 1)
        state["approval_mode"] = run["approval_mode"]
        transition(state, "queued")
        _write_state(run, state)
        run["status"] = "queued"
        run["reason"] = None
        run["cancel_requested"] = False
        run["worker_pid"] = None
        run["worker_token"] = None
        run["worker_launch_pid"] = None
        self.store.write(run)
        self._project_workflow_status(run_id, "queued", reason="workflow run resumed")
        self._start_worker(run_id)
        return self.store.get(run_id)

    def cancel(self, run_id: str, *, confirm: bool = False) -> dict:
        if not confirm:
            raise RunError("Cancellation requires confirm=true")
        with self.store.checkpoint_guard(run_id):
            run = self.store.get(run_id)
            self._validate_run_worktree(run)
            if run["status"] in TERMINAL_STATUSES:
                return run
            run["cancel_requested"] = True
            if run["status"] not in {"checkpointing", "cancelling"}:
                self._set_status(run_id, "cancelling", reason="cancellation requested")
            else:
                self.store.write(run)
            self.store.append_log(run_id, {"kind": "cancellation_requested", "message": "Cancellation requested by user"})
        event = self._cancel_events.get(run_id)
        if event:
            event.set()
        executor = self._executors.get(run_id)
        if executor:
            cancel = getattr(executor, "cancel", None)
            if callable(cancel):
                cancel()
        worker_pid = run.get("worker_pid")
        if self.worker_backend == "process" and worker_pid and self.store.pid_alive(worker_pid):
            try:
                os.kill(int(worker_pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        worker = self._workers.get(run_id)
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = self.store.get(run_id)
            if run["status"] in TERMINAL_STATUSES or run["status"] == "needs_recovery":
                break
            if not self.store.worker_alive(run) and not self._worker_is_alive(run_id):
                self._set_status(run_id, "cancelled", reason="cancelled by user")
                self.store.release_repo_lock(run["repo_key"], run_id)
                break
            time.sleep(0.05)
        return self.store.get(run_id)

    def recover(self, run_id: str, *, confirm: bool = False) -> dict:
        if not confirm:
            raise RunError("Recovery requires confirm=true")
        run = self.store.get(run_id)
        self._validate_run_worktree(run)
        if self._worker_is_alive(run_id) or self.store.worker_alive(run):
            raise RunError("Run still has an active worker; cancel it or wait for it to exit before recovery")
        worktree = Path(run["worktree_path"])
        if not worktree.is_dir():
            raise RunError(f"Retained worktree is missing: {worktree}")
        pending = dict(run.get("pending_checkpoint") or {})
        journal = self.store.read_journal(run_id)
        if pending or journal.get("phase") != "complete":
            pending.update(journal)
        if not pending.get("task_id") or not pending.get("tag"):
            if run["status"] not in ACTIVE_STATUSES | {"needs_recovery"}:
                raise RunError("No recoverable checkpoint boundary is recorded")
            self._set_status(run_id, "paused", reason="Worker stopped before a checkpoint boundary; inspect the retained worktree")
            run = self.store.get(run_id)
            run["cancel_requested"] = False
            self.store.write(run)
            self._project_workflow_status(
                run_id,
                "paused",
                reason="Worker stopped before a checkpoint boundary; inspect the retained worktree",
            )
            self.store.release_repo_lock(run["repo_key"], run_id)
            self.store.append_log(run_id, {"kind": "recovery", "message": "Paused safely before checkpoint boundary"})
            return self.store.get(run_id)

        self._reconcile_pending_checkpoint(run_id, pending)
        run = self.store.get(run_id)
        if run["status"] == "completed":
            self.store.release_repo_lock(run["repo_key"], run_id)
        else:
            run["status"] = "paused"
            run["reason"] = "checkpoint boundary reconciled; continuing automatically"
            self.store.write(run)
            self._project_workflow_status(run_id, "paused", reason=run["reason"])
            state = _load_state(run)
            if state.get("status") != "paused":
                state["status"] = "paused"
                state["reason"] = run["reason"]
                state["updated_at"] = utc_now()
                _write_state(run, state)
            self.store.acquire_repo_lock(run["repo_key"], run_id)
            self._start_worker(run_id)
        return self.store.get(run_id)

    def _reconcile_pending_checkpoint(self, run_id: str, pending: dict) -> None:
        run = self.store.get(run_id)
        worktree = Path(run["worktree_path"])
        task_id = str(pending.get("task_id"))
        tag_name = str(pending.get("tag"))
        allowed_paths = sorted(set(pending.get("allowed_paths") or []))
        if not allowed_paths:
            raise RunError("Checkpoint journal has no explicit change manifest; refusing broad recovery")
        if any(path.startswith("/") or path == ".." or path.startswith("../") for path in allowed_paths):
            raise RunError("Checkpoint journal contains an unsafe change manifest")
        if parse_task(worktree / "TASK.md").digest != run["task_digest"]:
            raise RunError("TASK.md changed; refusing automatic recovery")
        plan = parse_plan(worktree / "PLAN.md")
        item = next((candidate for candidate in plan.items if candidate.task_id == task_id), None)
        if item is None or not item.checked:
            raise RunError("Recovery cannot infer a completed plan item safely")
        unexpected = sorted(set(_relative_paths(changed_paths(worktree))) - set(allowed_paths))
        if unexpected:
            raise RunError("Unrelated worktree changes prevent automatic recovery: " + ", ".join(unexpected))
        if _risk_flags(worktree):
            raise RunError("Risk flags prevent automatic recovery; inspect the retained worktree")

        target = tag_target(worktree, tag_name)
        state_commit = pending.get("state_commit")
        implementation_commit = pending.get("implementation_commit")
        expected_message = f"codex: complete {task_id}"
        current = current_sha(worktree)

        if target is not None:
            if not state_commit and target == current and commit_subject(worktree) == f"codex: finalize {task_id} checkpoint state":
                state_commit = target
                pending["state_commit"] = target
                self._write_pending(run_id, pending)
            if not state_commit or target != state_commit or current != target:
                raise RunError(f"Existing tag {tag_name} does not match the exact recorded checkpoint commit")
            if has_changes(worktree):
                raise RunError("Retained worktree is dirty after the checkpoint tag; inspect it before recovery")
            self._finish_checkpoint(run_id, pending, target, recovered=True)
            return

        if not state_commit and current != pending.get("before_sha") and commit_subject(worktree) == f"codex: finalize {task_id} checkpoint state":
            state_commit = current
            pending["state_commit"] = state_commit
            if not implementation_commit:
                parent = git(worktree, "rev-parse", f"{current}^").stdout.strip()
                if commit_subject(worktree, parent) == expected_message:
                    implementation_commit = parent
                    pending["implementation_commit"] = parent
            pending["phase"] = "state_committed"
            self._write_pending(run_id, pending)

        if not implementation_commit:
            if current != pending.get("before_sha") and commit_subject(worktree) == expected_message:
                implementation_commit = current
            elif has_changes(worktree):
                implementation_commit = stage_and_commit(worktree, expected_message, allowed_paths)
            else:
                raise RunError("Recovery found neither a checkpoint commit nor staged changes")
            pending["implementation_commit"] = implementation_commit
            pending["phase"] = "implementation_committed"
            self._write_pending(run_id, pending)
        elif current != implementation_commit and not state_commit:
            raise RunError("Retained worktree HEAD does not match the recorded implementation commit")

        if not state_commit:
            state = _load_state(run)
            state["status"] = "completed" if pending.get("all_complete") else "running"
            state["current_item"] = None
            state["pending_checkpoint"] = None
            state["last_checkpoint"] = {"task_id": task_id, "tag": tag_name, "status": "complete"}
            state["updated_at"] = utc_now()
            _write_state(run, state)
            state_commit = stage_and_commit(worktree, f"codex: finalize {task_id} checkpoint state", [".codex-loop/state.json"])
            pending["state_commit"] = state_commit
            pending["phase"] = "state_committed"
            self._write_pending(run_id, pending)

        existing = tag_target(worktree, tag_name)
        if existing is not None and existing != state_commit:
            raise RunError(f"Conflicting existing tag will not be overwritten: {tag_name}")
        if existing is None:
            create_annotated_tag(worktree, tag_name, f"Codex checkpoint for {task_id}", state_commit)
        pending["tag_target"] = state_commit
        pending["phase"] = "tagged"
        self._write_pending(run_id, pending)
        self._finish_checkpoint(run_id, pending, state_commit, recovered=True)

    def _write_pending(self, run_id: str, pending: dict) -> None:
        run = self.store.get(run_id)
        run["pending_checkpoint"] = dict(pending)
        self.store.write(run)
        self.store.write_journal(run_id, dict(pending))

    def _finish_checkpoint(self, run_id: str, pending: dict, commit_sha: str, *, recovered: bool = False) -> None:
        run = self.store.get(run_id)
        all_complete = bool(pending.get("all_complete"))
        run["last_checkpoint"] = {
            "task_id": pending["task_id"],
            "tag": pending["tag"],
            "commit": commit_sha,
            "recovered_at" if recovered else "completed_at": utc_now(),
        }
        run["pending_checkpoint"] = None
        run["status"] = "completed" if all_complete else "running"
        run["reason"] = None
        self.store.write(run)
        self._project_workflow_status(run_id, run["status"], reason="checkpoint reconciled")
        self.store.write_journal(run_id, {**pending, "phase": "complete", "tag_target": commit_sha})
        self.store.append_log(
            run_id,
            {"kind": "checkpoint", "task_id": pending["task_id"], "commit": commit_sha, "tag": pending["tag"], "recovered": recovered},
        )
        if all_complete:
            self.store.release_repo_lock(run["repo_key"], run_id)

    def _start_worker(self, run_id: str) -> None:
        run = self.store.get(run_id)
        self._validate_run_worktree(run)
        if self.store.worker_alive(run) or self._worker_is_alive(run_id):
            return
        with self._lock:
            existing = self._workers.get(run_id)
            if existing and existing.is_alive():
                return
            if self.worker_backend == "process":
                token = uuid.uuid4().hex
                run = self.store.get(run_id)
                run["worker_token"] = token
                run["worker_launch_pid"] = os.getpid()
                run["worker_started_at"] = utc_now()
                self.store.write(run)
                environment = {
                    **os.environ,
                    "LONG_RUNNING_TASKS_DATA_DIR": str(self.store.root),
                    "PYTHONPATH": os.pathsep.join(
                        [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]
                    ).rstrip(os.pathsep),
                }
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "long_running_tasks.worker", run_id, token],
                        cwd=Path(__file__).resolve().parents[2],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        env=environment,
                        text=True,
                    )
                except OSError as exc:
                    failed_run = self.store.get(run_id)
                    failed_run["worker_launch_pid"] = None
                    failed_run["worker_token"] = None
                    failed_run["worker_started_at"] = None
                    self.store.write(failed_run)
                    self._set_status(run_id, "needs_recovery", reason=f"Unable to launch worker process: {exc}")
                    raise RunError(f"Unable to launch worker process: {exc}") from exc
                self.store.record_worker_spawn(run_id, token, process.pid)
                self._worker_processes[run_id] = process
                return
            cancel_event = self._cancel_events.setdefault(run_id, threading.Event())
            cancel_event.clear()
            worker = threading.Thread(target=self._worker_entry, args=(run_id,), name=f"long-run-{run_id}", daemon=True)
            self._workers[run_id] = worker
            worker.start()

    def _worker_is_alive(self, run_id: str) -> bool:
        worker = self._workers.get(run_id)
        return bool(worker and worker.is_alive())

    def _set_status(self, run_id: str, status: str, *, reason: str | None = None) -> None:
        run = self.store.get(run_id)
        run["status"] = status
        run["reason"] = reason
        self.store.write(run)
        if status == "needs_recovery" and (run.get("pending_checkpoint") or {}).get("state_commit"):
            self._project_workflow_status(run_id, status, reason=reason)
            return
        state = _load_state(run)
        current = state.get("status")
        try:
            if current != status:
                transition(state, status, reason=reason)
            elif reason:
                state["reason"] = reason
                state["updated_at"] = utc_now()
        except ValueError:
            if status not in {"needs_recovery", "failed", "cancelled", "cancelling"}:
                raise
            state["status"] = status
            state["reason"] = reason
            state["updated_at"] = utc_now()
        _write_state(run, state)
        self._project_workflow_status(run_id, status, reason=reason)

    def _worker_entry(self, run_id: str) -> None:
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        run = self.store.get(run_id)
        try:
            self._validate_run_worktree(run)
        except RunError as exc:
            # A tampered or legacy run must never cause the error path to
            # write into the base checkout.  Keep the durable run record
            # inspectable while leaving its versioned worktree state alone.
            run["status"] = "needs_recovery"
            run["reason"] = str(exc)
            self.store.write(run)
            self.store.append_log(run_id, {"kind": "safety_gate", "message": str(exc)})
            return
        token = run.get("worker_token")
        if token and self.worker_backend == "inline":
            try:
                self.store.claim_worker(run_id, token)
            except StoreError:
                return
        if token:
            def heartbeat() -> None:
                while not heartbeat_stop.wait(self.heartbeat_interval_seconds):
                    try:
                        self.store.write_heartbeat(run_id, os.getpid(), token)
                    except StoreError:
                        return

            heartbeat_thread = threading.Thread(target=heartbeat, name=f"heartbeat-{run_id}", daemon=True)
            heartbeat_thread.start()
        try:
            self._worker(run_id)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self.store.append_log(run_id, {"kind": "error", "message": str(exc)})
            try:
                self._set_status(run_id, "needs_recovery", reason=str(exc))
            except Exception:
                pass
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=2)
            if token:
                self.store.release_worker(run_id, token)

    def _worker(self, run_id: str) -> None:
        run = self.store.get(run_id)
        self._validate_run_worktree(run)
        worktree = Path(run["worktree_path"])
        cancel_event = self._cancel_events.setdefault(run_id, threading.Event())
        executor = self.executor_factory()
        self._executors[run_id] = executor
        try:
            while True:
                run = self.store.get(run_id)
                if cancel_event.is_set() or run.get("cancel_requested") or run["status"] in {"cancelled", "cancelling"}:
                    self._cancel_worker(run_id, "cancelled by user")
                    return
                task = parse_task(worktree / "TASK.md")
                plan = parse_plan(worktree / "PLAN.md")
                if task.digest != run["task_digest"]:
                    self._set_status(run_id, "needs_recovery", reason="TASK.md changed after run start")
                    return
                snapshot_path = Path(run.get("task_snapshot_path", ""))
                if not snapshot_path.is_file() or _text_digest(snapshot_path.read_text(encoding="utf-8")) != run["task_digest"]:
                    self._set_status(run_id, "needs_recovery", reason="Immutable TASK.md snapshot is missing or changed")
                    return
                if plan.complete:
                    self._set_status(run_id, "completed", reason="PLAN.md is complete")
                    self.store.release_repo_lock(run["repo_key"], run_id)
                    return
                item = plan.next_item
                assert item is not None
                if run.get("max_iterations") and run.get("iteration", 0) >= run["max_iterations"]:
                    self._set_status(run_id, "paused", reason="max_iterations reached")
                    return
                self._set_status(run_id, "running", reason=f"working on {item.task_id}")
                if cancel_event.is_set() or self.store.get(run_id).get("cancel_requested"):
                    self._cancel_worker(run_id, "cancelled by user")
                    return
                run = self.store.get(run_id)
                run["current_item"] = item.task_id
                run["iteration"] = int(run.get("iteration", 0)) + 1
                run["plan_shape_digest"] = _plan_shape_digest(plan.text)
                run["before_sha"] = current_sha(worktree)
                self.store.write(run)
                state = _load_state(run)
                state["status"] = "running"
                state["current_item"] = item.task_id
                state["iteration"] = run["iteration"]
                state["plan_digest"] = plan.digest
                state["updated_at"] = utc_now()
                _write_state(run, state)
                before_paths = _implementation_paths(worktree)
                prompt = self._prompt(run, task, plan, item)
                continuation_summary: Path | None = None
                resume_attempts = 0
                while True:
                    result = self._run_codex_turn(run, executor, prompt, cancel_event)
                    run = self.store.get(run_id)
                    if result.cancelled or cancel_event.is_set() or run.get("cancel_requested"):
                        self._cancel_worker(run_id, "Codex process cancelled")
                        return
                    if result.context_pressure:
                        if not result.session_id:
                            self._set_status(run_id, "needs_recovery", reason="Context pressure occurred without a resumable session ID")
                            return
                        if resume_attempts >= self.resume_attempt_limit:
                            self._set_status(run_id, "needs_recovery", reason="Native context resume retry limit exceeded")
                            return
                        run["session_id"] = result.session_id
                        run["continuation_count"] = int(run.get("continuation_count", 0)) + 1
                        if run["continuation_count"] > 8:
                            self._set_status(run_id, "needs_recovery", reason="Context continuation limit exceeded")
                            return
                        resume_attempts += 1
                        run["resume_attempts"] = resume_attempts
                        continuation_summary = self._write_continuation_summary(run, task, plan, item, result)
                        run["reason"] = f"context pressure; resuming from {continuation_summary}"
                        self.store.write(run)
                        state = _load_state(run)
                        state["continuation_count"] = run["continuation_count"]
                        state["session_id"] = result.session_id
                        state["resume_attempts"] = resume_attempts
                        state["context_pressure"] = {"ratio": result.context_ratio, "summary": str(continuation_summary)}
                        state["updated_at"] = utc_now()
                        _write_state(run, state)
                        prompt = self._continuation_prompt(continuation_summary, run, item)
                        continue
                    if resume_attempts and (result.timed_out or result.returncode != 0):
                        if resume_attempts >= self.resume_attempt_limit:
                            self._set_status(run_id, "needs_recovery", reason="Native context resume failed after three attempts")
                            return
                        self.store.append_log(
                            run_id,
                            {"kind": "resume_retry", "attempt": resume_attempts, "returncode": result.returncode, "timed_out": result.timed_out},
                        )
                        resume_attempts += 1
                        run["resume_attempts"] = resume_attempts
                        self.store.write(run)
                        if continuation_summary is None:
                            self._set_status(run_id, "needs_recovery", reason="Native resume failed without a continuation summary")
                            return
                        prompt = self._continuation_prompt(continuation_summary, run, item)
                        continue
                    if result.approval_requested or APPROVAL_SIGNAL_RE.search(f"{result.stdout}\n{result.stderr}"):
                        self._set_status(run_id, "needs_approval", reason="Codex requested approval or reported ambiguity")
                        return
                    if result.timed_out:
                        self._set_status(run_id, "failed", reason="Codex execution timed out")
                        return
                    if result.returncode != 0:
                        self._set_status(run_id, "failed", reason=f"Codex exited with code {result.returncode}")
                        return
                    break
                after_task = parse_task(worktree / "TASK.md")
                after_plan = parse_plan(worktree / "PLAN.md")
                if after_task.digest != run["task_digest"] or _plan_shape_digest(after_plan.text) != run["plan_shape_digest"]:
                    self._set_status(run_id, "needs_recovery", reason="Codex modified TASK.md or PLAN.md outside the runner contract")
                    return
                after_paths = _implementation_paths(worktree)
                # A resumed run may already contain retained implementation changes
                # from a worker that stopped before reaching its checkpoint boundary.
                if after_paths == before_paths and not after_paths:
                    self._set_status(run_id, "failed", reason="Codex completed without implementation changes")
                    return
                self._set_status(run_id, "verifying", reason=f"running verification for {item.task_id}")
                exit_code, stdout, stderr, timed_out = _verify(
                    worktree,
                    task.verification_command,
                    self.verification_timeout_seconds,
                    cancel_event,
                )
                self.store.append_log(run_id, {"kind": "verification", "task_id": item.task_id, "command": task.verification_command, "exit_code": exit_code, "timed_out": timed_out, "stdout": stdout[-8000:], "stderr": stderr[-8000:]})
                run = self.store.get(run_id)
                if cancel_event.is_set() or run.get("cancel_requested") or run["status"] == "cancelling":
                    self._cancel_worker(run_id, "cancelled during verification")
                    return
                if timed_out or exit_code != 0:
                    self._set_status(run_id, "failed", reason="verification failed; no checkpoint created")
                    return
                risks = _risk_flags(worktree, result)
                if risks:
                    self.store.append_log(run_id, {"kind": "approval", "task_id": item.task_id, "flags": risks})
                    self._set_status(run_id, "needs_approval", reason="risk policy requires approval: " + ", ".join(risks))
                    return
                if cancel_event.is_set() or self.store.get(run_id).get("cancel_requested"):
                    self._cancel_worker(run_id, "cancelled before checkpoint")
                    return
                allowed_paths = sorted(set(_relative_paths(after_paths)) | {"PLAN.md", ".codex-loop/state.json"})
                self._checkpoint(run_id, task, after_plan, item.task_id, allowed_paths)
                run = self.store.get(run_id)
                if run["status"] in TERMINAL_STATUSES or run["status"] == "paused":
                    return
        finally:
            self._executors.pop(run_id, None)

    def _cancel_worker(self, run_id: str, reason: str) -> None:
        run = self.store.get(run_id)
        self._set_status(run_id, "cancelled", reason=reason)
        run = self.store.get(run_id)
        run["cancel_requested"] = True
        self.store.write(run)
        self.store.release_repo_lock(run["repo_key"], run_id)

    def _run_codex_turn(self, run: dict, executor: CodexExecutor, prompt: str, cancel_event: threading.Event) -> CodexResult:
        run_id = run["run_id"]
        output_file = self.store.logs_dir / run_id / f"codex-{int(time.time() * 1000)}.last-message.txt"

        def event_callback(event: dict) -> None:
            self.store.append_log(run_id, {"kind": "codex_event", "event": event})

        result = executor.run(
            prompt=prompt,
            worktree=Path(run["worktree_path"]),
            output_file=output_file,
            approval_mode=run["approval_mode"],
            model=run.get("model"),
            session_id=run.get("session_id"),
            timeout_seconds=self.codex_timeout_seconds,
            cancel_event=cancel_event,
            event_callback=event_callback,
        )
        run = self.store.get(run_id)
        if result.session_id:
            run["session_id"] = result.session_id
        self.store.write(run)
        self.store.append_log(run_id, {"kind": "codex_result", "returncode": result.returncode, "session_id": result.session_id, "context_ratio": result.context_ratio, "context_pressure": result.context_pressure, "approval_requested": result.approval_requested, "stdout": result.stdout[-8000:], "stderr": result.stderr[-8000:]})
        return result

    def _prompt(self, run: dict, task: TaskSpec, plan: PlanSpec, item) -> str:
        criteria = "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
        workflow_context = ""
        if run.get("workflow_id"):
            source_digests: dict[str, str] = {}
            try:
                projection = self.store.read_workflow_projection(run["run_id"])
                source_digests = dict(projection.get("source_digests", {}))
            except StoreError:
                # The immutable projection is checked at start time.  Keep the
                # prompt useful for a retained legacy run even if its external
                # file was removed; status projection will record the issue.
                source_digests = {}
            workflow_context = f"""

Workflow execution projection (immutable input; do not edit it):
- Workflow: {run.get('workflow_id')}
- Selected ticket: {run.get('workflow_ticket_id')}
- Projection digest: {run.get('workflow_projection_digest')}
- Source provenance digests: {json.dumps(source_digests, sort_keys=True)}
- External projection path: {run.get('workflow_projection_path')}
"""
        return f"""You are implementing one bounded slice of an orchestrated repository task.

Repository worktree: {run['worktree_path']}
Branch: {run['branch']}
Task item: {item.task_id}: {item.description}

Goal:
{task.goal}

Acceptance criteria:
{criteria}
{workflow_context}

Rules:
- Read the repository's AGENTS.md instructions before editing.
- Implement only the selected plan item and keep the change coherent.
- Do not edit TASK.md or PLAN.md; the runner owns those files.
- Do not commit, tag, merge, push, publish, or change the base branch.
- Run the verification command only when the implementation is ready; the runner will run it authoritatively.
- If requirements are ambiguous, destructive, or request merge/push/publish/release actions, finish with `RUNNER_STATUS: NEEDS_APPROVAL` and explain the risk.
- Finish with a concise summary of changes, files, tests, and remaining concerns.

Do actual repository work; do not merely describe code.
""".strip()

    def _continuation_prompt(self, summary: Path, run: dict, item) -> str:
        return f"""Context-pressure continuation for run {run['run_id']} and plan item {item.task_id}.

Read the continuation summary at {summary} and the current repository state.
Resume the same slice in the same worktree and session. Preserve completed work,
do not edit TASK.md or PLAN.md, do not commit or tag, and finish with the required
summary or RUNNER_STATUS: NEEDS_APPROVAL if requirements are ambiguous.
""".strip()

    def _write_continuation_summary(self, run: dict, task: TaskSpec, plan: PlanSpec, item, result: CodexResult) -> Path:
        path = self.store.continuation_path(run["run_id"], int(run.get("continuation_count", 0)))
        changed = "\n".join(f"- {code} {name}" for code, name in changed_paths(Path(run["worktree_path"]))) or "- none"
        content = f"""# Continuation summary

- Run: `{run['run_id']}`
- Worktree: `{run['worktree_path']}`
- Branch: `{run['branch']}`
- Plan item: `{item.task_id}: {item.description}`
- Session: `{result.session_id}`
- Context ratio: `{result.context_ratio:.3f}`
- Verification command: `{task.verification_command}`

## Changed paths

{changed}

## Latest Codex output

{result.stdout[-6000:]}

## Next action

Resume the same Codex session, inspect the current files, and complete only the
selected plan item. The runner still owns verification, PLAN.md, commit, and tag.
"""
        path.write_text(content, encoding="utf-8")
        self.store.append_log(run["run_id"], {"kind": "context_pressure", "summary": str(path), "ratio": result.context_ratio})
        return path

    def _checkpoint(self, run_id: str, task: TaskSpec, plan: PlanSpec, task_id: str, allowed_paths: list[str]) -> None:
        with self.store.checkpoint_guard(run_id):
            self._checkpoint_locked(run_id, task, plan, task_id, allowed_paths)

    def _checkpoint_locked(self, run_id: str, task: TaskSpec, plan: PlanSpec, task_id: str, allowed_paths: list[str]) -> None:
        run = self.store.get(run_id)
        self._validate_run_worktree(run)
        worktree = Path(run["worktree_path"])
        tag_name = f"codex-run/{run_id}/{task_id}"
        existing = tag_target(worktree, tag_name)
        if existing is not None:
            self._set_status(run_id, "needs_recovery", reason=f"checkpoint tag already exists: {tag_name}")
            raise RunError(f"Checkpoint tag already exists: {tag_name}")
        if any(path.startswith("/") or path == ".." or path.startswith("../") for path in allowed_paths):
            raise RunError("Unsafe checkpoint manifest")
        updated_plan = mark_item_complete(plan, task_id)
        (worktree / "PLAN.md").write_text(updated_plan, encoding="utf-8")
        updated_plan_spec = parse_plan(worktree / "PLAN.md")
        all_complete = updated_plan_spec.complete
        state = _load_state(run)
        pending = {
            "task_id": task_id,
            "tag": tag_name,
            "before_sha": run.get("before_sha"),
            "allowed_paths": sorted(set(allowed_paths)),
            "all_complete": all_complete,
            "implementation_commit": None,
            "state_commit": None,
            "tag_target": None,
            "phase": "prepared",
        }
        state["status"] = "checkpointing"
        state["current_item"] = None
        state["plan_digest"] = updated_plan_spec.digest
        state["plan_shape_digest"] = _plan_shape_digest(updated_plan_spec.text)
        state["pending_checkpoint"] = pending
        state["updated_at"] = utc_now()
        _write_state(run, state)
        run["status"] = "checkpointing"
        run["pending_checkpoint"] = pending
        self.store.write(run)
        self._project_workflow_status(run_id, "checkpointing", reason=f"checkpointing {task_id}")
        self.store.write_journal(run_id, pending)
        self.store.append_log(run_id, {"kind": "checkpointing", "task_id": task_id, "tag": tag_name})
        try:
            implementation_commit = stage_and_commit(worktree, f"codex: complete {task_id}", pending["allowed_paths"])
            pending["implementation_commit"] = implementation_commit
            pending["phase"] = "implementation_committed"
            self._write_pending(run_id, pending)

            state = _load_state(self.store.get(run_id))
            state["status"] = "completed" if all_complete else "running"
            state["current_item"] = None
            state["pending_checkpoint"] = None
            state["last_checkpoint"] = {"task_id": task_id, "tag": tag_name, "status": "complete"}
            state["updated_at"] = utc_now()
            _write_state(self.store.get(run_id), state)
            state_commit = stage_and_commit(worktree, f"codex: finalize {task_id} checkpoint state", [".codex-loop/state.json"])
            pending["state_commit"] = state_commit
            pending["phase"] = "state_committed"
            self._write_pending(run_id, pending)

            existing = tag_target(worktree, tag_name)
            if existing is not None and existing != state_commit:
                raise RunError(f"Conflicting existing tag will not be overwritten: {tag_name}")
            if existing is None:
                create_annotated_tag(worktree, tag_name, f"Codex checkpoint for {task_id}", state_commit)
            pending["tag_target"] = state_commit
            pending["phase"] = "tagged"
            self._write_pending(run_id, pending)
        except Exception as exc:
            self._set_status(run_id, "needs_recovery", reason=f"checkpoint boundary interrupted: {exc}")
            raise
        self._finish_checkpoint(run_id, pending, pending["state_commit"])
        run = self.store.get(run_id)
        if not all_complete and run.get("max_iterations") and run.get("iteration", 0) >= run["max_iterations"]:
            self._set_status(run_id, "paused", reason="max_iterations reached")
