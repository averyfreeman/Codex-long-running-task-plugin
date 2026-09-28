"""Public operations over the local canonical workflow store.

This module is the shared seam for MCP and CLI callers.  It owns no source
repository writes: reconciliation reads source artifacts and updates only the
external canonical workflow record, while run creation remains delegated to
the isolated runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .retention import RetentionPolicy, preview_retention, prune_retention
from .safety import BranchWriteSafetyError, assert_run_write_safe
from .scheduler import propagate_stale, select_frontier
from .store import RunStore, StoreError
from .workflow import (
    WorkflowError,
    WorkflowRecord,
    WorkflowStoreError,
    WorkflowTransitionError,
    WorkflowValidationError,
    digest_file,
    validate_workflow_record,
)


class WorkflowOperationError(WorkflowError):
    """Raised when a local workflow operation cannot be completed safely."""


class ReconciliationConfirmationRequired(WorkflowOperationError):
    """Raised when a caller explicitly requires confirmation to reconcile."""


@dataclass(frozen=True)
class WorkflowReconciliationResult:
    """The read-only audit and optional applied update from reconciliation."""

    workflow: WorkflowRecord
    persisted_workflow: WorkflowRecord
    checked: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unsafe: tuple[str, ...] = ()
    applied: bool = False
    requires_confirmation: bool = False

    @property
    def requires_reconciliation(self) -> bool:
        return bool(self.changed or self.missing or self.unsafe)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow.to_dict(),
            "persisted_workflow": self.persisted_workflow.to_dict(),
            "checked": list(self.checked),
            "unchanged": list(self.unchanged),
            "changed": list(self.changed),
            "missing": list(self.missing),
            "unsafe": list(self.unsafe),
            "applied": self.applied,
            "requires_confirmation": self.requires_confirmation,
            "requires_reconciliation": self.requires_reconciliation,
        }


def _record(value: WorkflowRecord | Mapping[str, Any]) -> WorkflowRecord:
    if isinstance(value, WorkflowRecord):
        return validate_workflow_record(value)
    if isinstance(value, Mapping):
        return WorkflowRecord.from_dict(value)
    raise WorkflowValidationError("workflow must be a WorkflowRecord or JSON object")


def _local_artifact_path(record: WorkflowRecord, node: Any) -> tuple[Path | None, str | None]:
    source = node.artifact or node.provenance.source_ref
    if not source:
        return None, "artifact reference is empty"
    source_text = str(source).split("#", 1)[0].strip()
    if not source_text or "://" in source_text or source_text.startswith("git@"):
        return None, f"artifact reference is not a local path: {source_text!r}"
    root = Path(record.repo_path).expanduser().resolve()
    candidate = Path(source_text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        return None, f"artifact path escapes workflow repository: {source_text!r}"
    return resolved, None


class WorkflowController:
    """Serial-first local workflow operation facade."""

    def __init__(self, store: RunStore | None = None):
        self.store = store or RunStore()

    def create(self, workflow: WorkflowRecord | Mapping[str, Any]) -> WorkflowRecord:
        record = _record(workflow)
        try:
            existing = self.store.get_workflow(record.workflow_id)
        except (StoreError, WorkflowStoreError):
            return self.store.create_workflow(record)
        if existing.digest != record.digest:
            raise WorkflowOperationError(
                f"Workflow {record.workflow_id} already exists with different content"
            )
        return existing

    create_workflow = create

    def get(self, workflow_id: str) -> WorkflowRecord:
        return self.store.get_workflow(workflow_id)

    get_workflow = get

    def list(
        self,
        repo_path: str | Path | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        offset = 0
        if cursor:
            if not cursor.startswith("offset:"):
                raise WorkflowOperationError("Invalid workflow cursor")
            try:
                offset = max(0, int(cursor.removeprefix("offset:")))
            except ValueError as exc:
                raise WorkflowOperationError("Invalid workflow cursor") from exc
        records = self.store.list_all_workflows()
        if repo_path:
            expected = Path(repo_path).expanduser().resolve()
            records = [
                record
                for record in records
                if Path(record.repo_path).expanduser().resolve() == expected
            ]
        records.sort(key=lambda record: (record.updated_at, record.workflow_id), reverse=True)
        page = records[offset : offset + limit]
        next_cursor = f"offset:{offset + len(page)}" if offset + len(page) < len(records) else None
        return {
            "workflows": [record.to_dict() for record in page],
            "next_cursor": next_cursor,
        }

    list_workflows = list

    def schedule(
        self,
        workflow_id: str,
        *,
        automation_mode: str | None = None,
    ) -> dict[str, Any]:
        record = self.get(workflow_id)
        frontier = select_frontier(record, automation_mode=automation_mode)
        return {
            "workflow_id": record.workflow_id,
            "workflow_revision": record.revision,
            "automation_mode": record.automation_mode,
            "frontier": [node.to_dict() for node in frontier],
            "selected_ticket": frontier[0].to_dict() if frontier else None,
            "requires_reconciliation": record.requires_reconciliation,
        }

    schedule_workflow = schedule
    select_frontier = schedule

    def reconcile(
        self,
        workflow_id: str,
        *,
        confirm: bool = False,
        require_confirmation: bool = False,
    ) -> WorkflowReconciliationResult:
        """Audit local source files and optionally persist stale transitions.

        A changed or unverifiable source is marked stale; its provenance is
        not silently replaced.  That forces a later explicit adapter/source
        update before scheduling can advance again.
        """

        record = self.get(workflow_id)
        updated = record
        checked: list[str] = []
        unchanged: list[str] = []
        changed: list[str] = []
        missing: list[str] = []
        unsafe: list[str] = []
        reason = "source artifact changed or could not be verified; reconciliation required"

        for node in record.nodes:
            if node.kind not in {"triage", "spec", "ticket"}:
                continue
            checked.append(node.node_id)
            path, path_error = _local_artifact_path(record, node)
            if path_error:
                unsafe.append(node.node_id)
                mismatch = True
            elif path is None or not path.is_file():
                missing.append(node.node_id)
                mismatch = True
            else:
                try:
                    mismatch = digest_file(path) != node.provenance.digest
                except OSError:
                    mismatch = True
                if mismatch:
                    changed.append(node.node_id)
                else:
                    unchanged.append(node.node_id)
            if mismatch and node.status != "stale":
                try:
                    updated = updated.transition_node(node.node_id, "stale", reason=reason)
                except WorkflowTransitionError:
                    # A superseded/rejected source remains inspectable; the
                    # mismatch is still reported and therefore fail-closed.
                    pass
            elif not mismatch and node.status == "stale":
                try:
                    updated = updated.transition_node(
                        node.node_id,
                        "current",
                        reason="source artifact revalidated",
                    )
                except WorkflowTransitionError:
                    pass

        if changed or missing or unsafe:
            updated = propagate_stale(updated, reason=reason)
        elif updated.status == "stale" and not updated.stale_source_nodes:
            try:
                updated = updated.transition("ready", reason="source artifacts revalidated")
            except WorkflowTransitionError:
                pass

        needs_apply = updated != record
        needs_confirmation = needs_apply and not confirm
        if needs_confirmation and require_confirmation:
            raise ReconciliationConfirmationRequired(
                f"Workflow {workflow_id} requires explicit reconciliation confirmation"
            )
        persisted = record
        applied = False
        if confirm and needs_apply:
            persisted = self.store.write_workflow(updated, expected_revision=record.revision)
            applied = True
        return WorkflowReconciliationResult(
            workflow=updated,
            persisted_workflow=persisted,
            checked=tuple(checked),
            unchanged=tuple(unchanged),
            changed=tuple(changed),
            missing=tuple(missing),
            unsafe=tuple(unsafe),
            applied=applied,
            requires_confirmation=needs_confirmation,
        )

    reconcile_workflow = reconcile

    def retention_preview(
        self,
        *,
        repo_path: str | Path | None = None,
        policy: RetentionPolicy | Mapping[str, Any] | None = None,
        max_age_seconds: float | None = None,
        max_runs: int | None = None,
    ) -> dict[str, Any]:
        selected = policy or RetentionPolicy(
            max_age_seconds=max_age_seconds,
            max_runs=max_runs,
        )
        if not isinstance(selected, RetentionPolicy):
            selected = RetentionPolicy.from_dict(selected)
        return preview_retention(self.store, selected, repo_path=repo_path)

    def prune_runs(
        self,
        *,
        repo_path: str | Path | None = None,
        policy: RetentionPolicy | Mapping[str, Any] | None = None,
        max_age_seconds: float | None = None,
        max_runs: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        selected = policy or RetentionPolicy(
            max_age_seconds=max_age_seconds,
            max_runs=max_runs,
        )
        if not isinstance(selected, RetentionPolicy):
            selected = RetentionPolicy.from_dict(selected)
        return prune_retention(self.store, selected, repo_path=repo_path, confirm=confirm)


__all__ = [
    "BranchWriteSafetyError",
    "ReconciliationConfirmationRequired",
    "WorkflowController",
    "WorkflowOperationError",
    "WorkflowReconciliationResult",
]
