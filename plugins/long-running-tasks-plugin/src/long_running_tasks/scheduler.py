"""Deterministic, serial-first scheduling for canonical workflow records.

The scheduler is deliberately a read-only module. It validates a workflow,
refuses records that require source reconciliation, and returns at most one
ticket from the current serial frontier. It does not transition nodes, create
runs, or write a checkout.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable

from .state import utc_now
from .workflow import (
    AutomationMode,
    WorkflowError,
    WorkflowNode,
    WorkflowRecord,
    WorkflowTransitionError,
    WorkflowValidationError,
    UnsupportedAutomationModeError,
    UnsupportedParallelSchedulingError,
    validate_workflow_record,
)


SOURCE_NODE_KINDS = frozenset({"triage", "spec", "ticket"})
FRONTIER_TICKET_STATUSES = frozenset(
    {"approved", "current", "ready", "ready-for-afk"}
)
STALE_PROPAGATION_EDGE_KINDS = frozenset(
    {"blocks", "derived_from", "produces", "references"}
)


class WorkflowSchedulingError(WorkflowError):
    """Base error for automatic workflow scheduling failures."""


class StaleWorkflowError(WorkflowSchedulingError):
    """Raised when automatic advancement requires source reconciliation."""

    def __init__(self, node_ids: Iterable[str] = (), *, workflow_stale: bool = False):
        self.node_ids = tuple(node_ids)
        self.workflow_stale = workflow_stale
        if workflow_stale and self.node_ids:
            detail = f"workflow and source nodes {', '.join(self.node_ids)} are stale"
        elif workflow_stale:
            detail = "workflow is stale"
        elif not self.node_ids:
            detail = "source artifacts are stale"
        else:
            detail = f"source nodes {', '.join(self.node_ids)} are stale"
        super().__init__(
            f"Automatic scheduling is paused; reconciliation is required ({detail})"
        )


def _coerce_record(record: WorkflowRecord | Mapping[str, Any]) -> WorkflowRecord:
    if isinstance(record, WorkflowRecord):
        return validate_workflow_record(record)
    if isinstance(record, Mapping):
        return WorkflowRecord.from_dict(record)
    raise WorkflowValidationError("scheduler requires a WorkflowRecord or workflow mapping")


def _requested_mode(
    record: WorkflowRecord,
    automation_mode: str | AutomationMode | None,
) -> AutomationMode:
    requested = record.automation_mode if automation_mode is None else automation_mode
    try:
        mode = AutomationMode(requested)
    except (TypeError, ValueError) as exc:
        raise UnsupportedAutomationModeError(
            f"Unsupported automation mode {requested!r}; only serial scheduling is supported"
        ) from exc
    if mode is not AutomationMode.SERIAL:
        raise UnsupportedParallelSchedulingError(
            "Parallel scheduling is not supported; use serial automation mode"
        )
    return mode


def _stale_source_ids(record: WorkflowRecord) -> tuple[str, ...]:
    return tuple(
        node.node_id
        for node in record.nodes
        if node.kind in SOURCE_NODE_KINDS
        and node.is_stale
    )


def _ensure_automatic_advancement_allowed(record: WorkflowRecord) -> None:
    stale_ids = _stale_source_ids(record)
    if record.status == "stale" or stale_ids:
        raise StaleWorkflowError(
            stale_ids,
            workflow_stale=record.status == "stale",
        )


def _ticket_sort_key(node: WorkflowNode) -> tuple[str, int, str, str]:
    feature = str(node.metadata.get("feature", ""))
    raw_number = node.metadata.get("ticket_number")
    try:
        number = int(str(raw_number))
    except (TypeError, ValueError):
        number = 2**31 - 1
    return feature, number, str(raw_number or ""), node.node_id


def eligible_ticket_nodes(
    record: WorkflowRecord | Mapping[str, Any],
    *,
    automation_mode: str | AutomationMode | None = None,
) -> tuple[WorkflowNode, ...]:
    """Return every runnable ticket in the current serial frontier.

    The result is read-only and deterministic. A ticket must be current,
    approved, in a runnable source status, and free of unresolved blocker
    edges. Stale source artifacts fail closed instead of being silently
    skipped.
    """

    validated = _coerce_record(record)
    _requested_mode(validated, automation_mode)
    _ensure_automatic_advancement_allowed(validated)
    candidates = [
        node
        for node in validated.nodes
        if node.kind == "ticket"
        and node.status in FRONTIER_TICKET_STATUSES
        and node.is_current
        and node.is_approved
        and validated.is_unblocked(node.node_id)
    ]
    return tuple(sorted(candidates, key=_ticket_sort_key))


def select_frontier(
    record: WorkflowRecord | Mapping[str, Any],
    *,
    automation_mode: str | AutomationMode | None = None,
    mode: str | AutomationMode | None = None,
) -> tuple[WorkflowNode, ...]:
    """Select the next serial ticket, returning zero or one node.

    mode is a compatibility spelling for callers that treat the selected
    automation mode as an invocation option. Supplying both names with
    different values is rejected explicitly.
    """

    if automation_mode is not None and mode is not None:
        try:
            if AutomationMode(automation_mode) is not AutomationMode(mode):
                raise UnsupportedAutomationModeError(
                    "automation_mode and mode must specify the same value"
                )
        except (TypeError, ValueError) as exc:
            raise UnsupportedAutomationModeError(
                "automation_mode and mode must specify the same supported value"
            ) from exc
    requested = automation_mode if automation_mode is not None else mode
    candidates = eligible_ticket_nodes(record, automation_mode=requested)
    return candidates[:1]


def select_serial_frontier(
    record: WorkflowRecord | Mapping[str, Any],
) -> tuple[WorkflowNode, ...]:
    """Select the next ticket using the only supported automation mode."""

    return select_frontier(record, automation_mode=AutomationMode.SERIAL)


def _propagates_stale(edge_kind: str) -> bool:
    return edge_kind in STALE_PROPAGATION_EDGE_KINDS


def propagate_stale(
    record: WorkflowRecord | Mapping[str, Any],
    *,
    now: str | None = None,
    reason: str = "stale source artifact requires reconciliation",
) -> WorkflowRecord:
    """Return a new record with stale source dependencies marked downstream.

    Propagation follows source/dependency edges only. Run nodes are not
    rewritten: an existing run remains inspectable and recoverable even when
    its source ticket later becomes stale.
    """

    updated = _coerce_record(record)
    timestamp = now or utc_now()
    stale_ids = list(_stale_source_ids(updated))
    if updated.status != "stale":
        try:
            updated = updated.transition("stale", reason=reason, now=timestamp)
        except WorkflowTransitionError:
            # A failed, interrupted, or terminal workflow keeps its lifecycle
            # status for inspection; its stale source nodes still gate future
            # automatic advancement.
            pass

    queue = list(stale_ids)
    seen = set(stale_ids)
    for node_id in tuple(stale_ids):
        node = updated.node(node_id)
        if node.status == "stale":
            continue
        try:
            updated = updated.transition_node(
                node_id,
                "stale",
                reason=reason,
                now=timestamp,
            )
        except WorkflowTransitionError:
            # Rejected or superseded nodes already require reconciliation and
            # cannot be moved backwards into the stale status.
            pass

    while queue:
        source_id = queue.pop(0)
        for edge in updated.edges:
            if edge.source != source_id or not _propagates_stale(edge.kind):
                continue
            target = updated.node(edge.target)
            if target.kind not in SOURCE_NODE_KINDS:
                continue
            if target.node_id in seen:
                continue
            seen.add(target.node_id)
            queue.append(target.node_id)
            if target.status == "stale":
                continue
            try:
                updated = updated.transition_node(
                    target.node_id,
                    "stale",
                    reason=reason,
                    now=timestamp,
                )
            except WorkflowTransitionError:
                # A terminal source artifact remains unchanged but is still
                # part of the reconciliation frontier represented by seen.
                pass
    return updated


class WorkflowScheduler:
    """Small scheduling seam with a serial-first implementation."""

    def __init__(
        self,
        automation_mode: str | AutomationMode | None = None,
        *,
        mode: str | AutomationMode | None = None,
    ) -> None:
        if automation_mode is not None and mode is not None:
            self.automation_mode = automation_mode
            self._alternate_mode = mode
        else:
            self.automation_mode = (
                automation_mode if automation_mode is not None else mode
            )
            self._alternate_mode = None

    def select_frontier(
        self,
        record: WorkflowRecord | Mapping[str, Any],
    ) -> tuple[WorkflowNode, ...]:
        return select_frontier(
            record,
            automation_mode=self.automation_mode,
            mode=self._alternate_mode,
        )

    def select(
        self,
        record: WorkflowRecord | Mapping[str, Any],
    ) -> tuple[WorkflowNode, ...]:
        return self.select_frontier(record)


Scheduler = WorkflowScheduler
frontier = select_frontier
schedule_next = select_frontier
ParallelSchedulingUnsupportedError = UnsupportedParallelSchedulingError
UnsupportedParallelModeError = UnsupportedParallelSchedulingError


__all__ = [
    "FRONTIER_TICKET_STATUSES",
    "ParallelSchedulingUnsupportedError",
    "SOURCE_NODE_KINDS",
    "STALE_PROPAGATION_EDGE_KINDS",
    "StaleWorkflowError",
    "UnsupportedAutomationModeError",
    "UnsupportedParallelModeError",
    "UnsupportedParallelSchedulingError",
    "WorkflowScheduler",
    "WorkflowSchedulingError",
    "Scheduler",
    "eligible_ticket_nodes",
    "frontier",
    "propagate_stale",
    "schedule_next",
    "select_frontier",
    "select_serial_frontier",
]
