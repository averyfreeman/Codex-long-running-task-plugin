from __future__ import annotations

from pathlib import Path

import pytest

from long_running_tasks.scheduler import (
    StaleWorkflowError,
    UnsupportedParallelSchedulingError,
    WorkflowScheduler,
    eligible_ticket_nodes,
    propagate_stale,
    select_frontier,
)
from long_running_tasks.workflow import (
    AutomationMode,
    Provenance,
    WorkflowEdge,
    WorkflowRecord,
    WorkflowValidationError,
)


FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _node(
    node_id: str,
    kind: str = "ticket",
    status: str = "approved",
    *,
    current: bool = True,
    approved: bool = True,
    ticket_number: str | None = None,
) -> object:
    from long_running_tasks.workflow import WorkflowNode

    metadata = {"feature": "demo"}
    if ticket_number is not None:
        metadata["ticket_number"] = ticket_number
    return WorkflowNode(
        node_id=node_id,
        kind=kind,
        status=status,
        title=node_id,
        provenance=Provenance.from_text(node_id, f"{node_id}\n", captured_at=FIXED_TIME),
        current=current,
        approved=approved,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
        metadata=metadata,
    )


def test_serial_frontier_selects_only_one_current_approved_unblocked_ticket():
    first = _node("ticket-01", ticket_number="01")
    second = _node("ticket-02", status="ready", ticket_number="02")
    historical = _node(
        "ticket-old",
        status="superseded",
        current=False,
        approved=False,
        ticket_number="00",
    )
    unapproved = _node("ticket-draft", status="draft", approved=False, ticket_number="04")
    running = _node("ticket-running", status="running", ticket_number="05")
    record = WorkflowRecord.create(
        "frontier",
        Path("/tmp/repo"),
        nodes=[first, second, historical, unapproved, running],
        edges=[WorkflowEdge("ticket-01", "ticket-02", "blocks")],
        now=FIXED_TIME,
    )

    assert tuple(node.node_id for node in eligible_ticket_nodes(record)) == ("ticket-01",)
    assert tuple(node.node_id for node in select_frontier(record)) == ("ticket-01",)
    assert tuple(node.node_id for node in record.select_frontier()) == ("ticket-01",)
    assert record.node("ticket-02").status == "ready"

    completed = record.transition_node("ticket-01", "completed", now=FIXED_TIME)
    assert tuple(node.node_id for node in select_frontier(completed)) == ("ticket-02",)


def test_parallel_automation_is_explicitly_rejected():
    record = WorkflowRecord.create("serial-only", "/tmp/repo", now=FIXED_TIME)

    with pytest.raises(UnsupportedParallelSchedulingError, match="not supported"):
        select_frontier(record, automation_mode=AutomationMode.PARALLEL)
    with pytest.raises(UnsupportedParallelSchedulingError, match="only serial"):
        WorkflowRecord.create("parallel-record", "/tmp/repo", automation_mode="parallel")
    with pytest.raises(UnsupportedParallelSchedulingError, match="not supported"):
        WorkflowScheduler(mode="parallel").select(record)

    with pytest.raises(WorkflowValidationError):
        WorkflowRecord.create("unknown-mode", "/tmp/repo", automation_mode="batch")


def test_stale_sources_pause_selection_and_propagate_without_mutating_input():
    triage = _node("triage-1", "triage", "stale", approved=True)
    spec = _node("spec-1", "spec", "approved")
    first = _node("ticket-01", ticket_number="01")
    second = _node("ticket-02", status="ready", ticket_number="02")
    run = _node("run-1", "run", "failed", approved=False)
    record = WorkflowRecord.create(
        "stale-chain",
        "/tmp/repo",
        nodes=[triage, spec, first, second, run],
        edges=[
            WorkflowEdge("triage-1", "spec-1", "produces"),
            WorkflowEdge("spec-1", "ticket-01", "produces"),
            WorkflowEdge("ticket-01", "ticket-02", "blocks"),
            WorkflowEdge("ticket-01", "run-1", "executes"),
        ],
        now=FIXED_TIME,
    )
    original_digest = record.digest

    with pytest.raises(StaleWorkflowError, match="reconciliation"):
        select_frontier(record)

    updated = propagate_stale(record, now=FIXED_TIME)
    assert record.digest == original_digest
    assert record.status == "draft"
    assert updated.status == "stale"
    assert updated.node("triage-1").status == "stale"
    assert updated.node("spec-1").status == "stale"
    assert updated.node("ticket-01").status == "stale"
    assert updated.node("ticket-02").status == "stale"
    assert updated.node("run-1").status == "failed"
    assert updated.node("run-1").is_recoverable
    assert updated.requires_reconciliation


def test_stale_failed_workflow_keeps_failed_lifecycle_status_inspectable():
    triage = _node("triage-1", "triage", "stale", approved=True)
    run = _node("run-1", "run", "failed", approved=False)
    record = WorkflowRecord.create(
        "failed-stale",
        "/tmp/repo",
        status="failed",
        nodes=[triage, run],
        now=FIXED_TIME,
    )

    updated = propagate_stale(record, now=FIXED_TIME)
    assert updated.status == "failed"
    assert updated.node("run-1").status == "failed"
    assert updated.node("run-1").is_recoverable
