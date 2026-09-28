"""Workflow-facing inputs for the existing isolated run manager.

The canonical workflow record is a coordination record.  The runner must not
execute against a record that can change underneath it, so a workflow start
captures the validated record and the selected ticket in this immutable
projection.  The projection is stored beside the runner's external runtime
data and is only copied by value into a run binding; it is never written to
the target checkout.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .workflow import (
    WorkflowNode,
    WorkflowRecord,
    WorkflowValidationError,
    digest_json,
    validate_workflow_record,
)


WORKFLOW_EXECUTION_PROJECTION_VERSION = 1


class WorkflowExecutionError(WorkflowValidationError):
    """Raised when a workflow cannot be bound to an isolated run."""


class WorkflowExecutionValidationError(WorkflowExecutionError):
    """Raised when a persisted execution projection is invalid."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowExecutionValidationError(f"{field_name} must be a non-empty string")
    return value


def _copy_json(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(dict(value))
    try:
        json.dumps(copied)
    except (TypeError, ValueError) as exc:
        raise WorkflowExecutionValidationError("projection metadata must contain JSON values") from exc
    return copied


@dataclass(frozen=True)
class WorkflowExecutionProjection:
    """A frozen input snapshot for one ticket execution.

    ``workflow`` is cloned during construction.  This matters even though
    :class:`WorkflowRecord` is a frozen dataclass: its metadata mappings are
    intentionally JSON-friendly mappings and callers can otherwise retain a
    mutable reference to one of them.  The projection therefore owns its own
    validated copy and its digest is stable for the lifetime of the run.
    """

    workflow: WorkflowRecord
    ticket_node_id: str
    run_id: str | None = None
    captured_at: str | None = None
    schema_version: int = WORKFLOW_EXECUTION_PROJECTION_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, WorkflowRecord):
            raise WorkflowExecutionValidationError("projection.workflow must be a WorkflowRecord")
        # Round-trip through the public serializer to take a true value copy
        # of nodes, edges, events, and nested metadata mappings.
        cloned = WorkflowRecord.from_dict(self.workflow.to_dict())
        object.__setattr__(self, "workflow", cloned)
        object.__setattr__(self, "metadata", _copy_json(self.metadata))
        if self.captured_at is None:
            from .state import utc_now

            object.__setattr__(self, "captured_at", utc_now())

    @classmethod
    def from_workflow(
        cls,
        record: WorkflowRecord | Mapping[str, Any],
        ticket_node_id: str | None = None,
        *,
        run_id: str | None = None,
        captured_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "WorkflowExecutionProjection":
        """Capture the selected serial frontier ticket from ``record``.

        Selection is deliberately performed at capture time.  A later source
        edit or stale propagation may change the canonical record, but it
        cannot silently change the inputs of an already-started run.
        """

        validated = (
            validate_workflow_record(record)
            if isinstance(record, WorkflowRecord)
            else WorkflowRecord.from_dict(record)
        )
        from .scheduler import select_frontier

        candidates = select_frontier(validated)
        if ticket_node_id is None:
            if not candidates:
                raise WorkflowExecutionError(
                    f"Workflow {validated.workflow_id} has no current, approved, unblocked ticket frontier"
                )
            selected = candidates[0]
        else:
            selected = validated.node(ticket_node_id)
            if selected.kind != "ticket":
                raise WorkflowExecutionError(
                    f"Workflow execution target must be a ticket node: {ticket_node_id}"
                )
            if all(candidate.node_id != ticket_node_id for candidate in candidates):
                raise WorkflowExecutionError(
                    f"Ticket {ticket_node_id} is not in the current approved unblocked serial frontier"
                )

        return cls(
            workflow=validated,
            ticket_node_id=selected.node_id,
            run_id=run_id,
            captured_at=captured_at,
            metadata=metadata or {},
        ).validate()

    capture = from_workflow
    from_record = from_workflow
    from_workflow_record = from_workflow

    @property
    def workflow_id(self) -> str:
        return self.workflow.workflow_id

    @property
    def workflow_revision(self) -> int:
        return self.workflow.revision

    @property
    def workflow_digest(self) -> str:
        return self.workflow.digest

    @property
    def record_digest(self) -> str:
        """Compatibility spelling for the captured workflow digest."""

        return self.workflow_digest

    @property
    def repository_path(self) -> str:
        return self.workflow.repo_path

    @property
    def repo_path(self) -> str:
        return self.repository_path

    @property
    def ticket_id(self) -> str:
        """Short alias used by runner callers."""

        return self.ticket_node_id

    @property
    def selected_ticket_id(self) -> str:
        return self.ticket_node_id

    @property
    def workflow_record(self) -> WorkflowRecord:
        return self.workflow

    @property
    def ticket(self) -> WorkflowNode:
        return self.workflow.node(self.ticket_node_id)

    @property
    def source_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(node for node in self.workflow.nodes if node.kind in {"triage", "spec", "ticket"})

    @property
    def source_node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.source_nodes)

    @property
    def source_digests(self) -> dict[str, str]:
        return {node.node_id: node.provenance.digest for node in self.source_nodes}

    @property
    def blocker_node_ids(self) -> tuple[str, ...]:
        return self.workflow.blockers_for(self.ticket_node_id, include_resolved=True)

    @property
    def execution_input(self) -> dict[str, Any]:
        """Return a detached JSON value suitable for a runner prompt/status."""

        return copy.deepcopy(self.to_dict(include_projection_digest=True))

    @property
    def input_digest(self) -> str:
        return self.digest

    @property
    def digest(self) -> str:
        return digest_json(self._payload())

    @property
    def projection_digest(self) -> str:
        return self.digest

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "workflow_revision": self.workflow_revision,
            "workflow_digest": self.workflow_digest,
            "repo_path": self.repository_path,
            "ticket_node_id": self.ticket_node_id,
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "source_node_ids": list(self.source_node_ids),
            "source_digests": self.source_digests,
            "source_provenance_digests": self.source_digests,
            "blocker_node_ids": list(self.blocker_node_ids),
            "ticket": self.ticket.to_dict(),
            "nodes": [node.to_dict() for node in self.workflow.nodes],
            "edges": [edge.to_dict() for edge in self.workflow.edges],
            "workflow_record": self.workflow.to_dict(),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    def to_dict(self, *, include_projection_digest: bool = True) -> dict[str, Any]:
        self.validate()
        payload = self._payload()
        if include_projection_digest:
            payload["projection_digest"] = self.digest
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def validate(self) -> "WorkflowExecutionProjection":
        if self.schema_version != WORKFLOW_EXECUTION_PROJECTION_VERSION:
            raise WorkflowExecutionValidationError(
                f"Unsupported workflow execution projection version: {self.schema_version}"
            )
        self.workflow.validate()
        _require_text(self.ticket_node_id, "projection.ticket_node_id")
        ticket = self.workflow.node(self.ticket_node_id)
        if ticket.kind != "ticket":
            raise WorkflowExecutionValidationError("projection.ticket_node_id must identify a ticket node")
        if not ticket.is_current or not ticket.is_approved:
            raise WorkflowExecutionValidationError(
                "projection.ticket_node_id must identify a current approved ticket"
            )
        if not self.workflow.is_unblocked(self.ticket_node_id):
            raise WorkflowExecutionValidationError(
                "projection.ticket_node_id must identify an unblocked ticket"
            )
        if self.run_id is not None:
            _require_text(self.run_id, "projection.run_id")
        _require_text(self.repository_path, "projection.repo_path")
        if "://" in self.repository_path or self.repository_path.startswith("git@"):
            raise WorkflowExecutionValidationError("projection.repo_path must be a local path")
        _require_text(self.captured_at, "projection.captured_at")
        try:
            from datetime import datetime

            datetime.fromisoformat(self.captured_at)
        except (TypeError, ValueError) as exc:
            raise WorkflowExecutionValidationError("projection.captured_at must be an ISO timestamp") from exc
        _copy_json(self.metadata)
        return self

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowExecutionProjection":
        if not isinstance(value, Mapping):
            raise WorkflowExecutionValidationError("workflow execution projection must be a JSON object")
        record_value = value.get("workflow_record", value.get("record", value.get("workflow")))
        if not isinstance(record_value, Mapping):
            raise WorkflowExecutionValidationError("projection.workflow_record must be a JSON object")
        projection = cls(
            workflow=WorkflowRecord.from_dict(record_value),
            ticket_node_id=value.get("ticket_node_id", value.get("ticket_id")),
            run_id=value.get("run_id"),
            captured_at=value.get("captured_at"),
            schema_version=value.get("schema_version", WORKFLOW_EXECUTION_PROJECTION_VERSION),
            metadata=value.get("metadata", {}),
        ).validate()
        if value.get("workflow_id") not in {None, projection.workflow_id}:
            raise WorkflowExecutionValidationError("projection.workflow_id does not match workflow_record")
        if value.get("workflow_digest", value.get("record_digest")) not in {None, projection.workflow_digest}:
            raise WorkflowExecutionValidationError("projection.workflow_digest does not match workflow_record")
        if value.get("workflow_revision") not in {None, projection.workflow_revision}:
            raise WorkflowExecutionValidationError("projection.workflow_revision does not match workflow_record")
        selected_ticket = value.get("ticket")
        if selected_ticket is not None:
            if not isinstance(selected_ticket, Mapping) or selected_ticket != projection.ticket.to_dict():
                raise WorkflowExecutionValidationError("projection.ticket does not match workflow_record")
        source_digests = value.get("source_digests", value.get("source_provenance_digests"))
        if source_digests is not None and source_digests != projection.source_digests:
            raise WorkflowExecutionValidationError("projection.source_digests do not match workflow_record")
        if value.get("nodes") is not None:
            expected_nodes = [node.to_dict() for node in projection.workflow.nodes]
            if value.get("nodes") != expected_nodes:
                raise WorkflowExecutionValidationError("projection.nodes do not match workflow_record")
        if value.get("edges") is not None:
            expected_edges = [edge.to_dict() for edge in projection.workflow.edges]
            if value.get("edges") != expected_edges:
                raise WorkflowExecutionValidationError("projection.edges do not match workflow_record")
        if value.get("projection_digest") not in {None, projection.digest}:
            raise WorkflowExecutionValidationError("projection.projection_digest does not match the canonical projection")
        return projection

    @classmethod
    def from_json(cls, text: str) -> "WorkflowExecutionProjection":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowExecutionValidationError("workflow execution projection is not valid JSON") from exc
        return cls.from_dict(value)


# The shorter names make the runner API read naturally while retaining the
# explicit class name for callers that need to distinguish it from a source
# Markdown snapshot.
ExecutionProjection = WorkflowExecutionProjection
WorkflowRunInput = WorkflowExecutionProjection
WorkflowRunProjection = WorkflowExecutionProjection


__all__ = [
    "ExecutionProjection",
    "WORKFLOW_EXECUTION_PROJECTION_VERSION",
    "WorkflowExecutionError",
    "WorkflowExecutionProjection",
    "WorkflowExecutionValidationError",
    "WorkflowRunInput",
    "WorkflowRunProjection",
]
