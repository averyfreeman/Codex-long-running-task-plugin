from __future__ import annotations

import json
from pathlib import Path

import pytest

from long_running_tasks.fixtures import sample_workflow_record
from long_running_tasks.workflow import (
    Provenance,
    WorkflowConflictError,
    WorkflowEdge,
    WorkflowNode,
    WorkflowRecord,
    WorkflowStore,
    WorkflowStoreError,
    WorkflowValidationError,
    digest_file,
    digest_text,
)


FIXTURE_DIR = Path(__file__).parents[1] / "src" / "long_running_tasks" / "fixtures"
FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _node(node_id: str, kind: str = "ticket", status: str = "approved") -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        kind=kind,
        status=status,
        title=node_id,
        provenance=Provenance.from_text(node_id, f"{node_id}\n", captured_at=FIXED_TIME),
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
        approved=status == "approved",
    )


def test_fixture_represents_each_workflow_artifact_kind_and_blocker_edge():
    record = sample_workflow_record()

    assert {node.kind for node in record.nodes} == {"triage", "spec", "ticket", "run"}
    assert record.schema_version == 1
    assert record.automation_mode == "serial"
    assert record.blockers_for("ticket-002") == ("ticket-001",)
    assert record.blockers_for("ticket-002", include_resolved=True) == ("ticket-001",)
    assert not record.is_unblocked("ticket-002")
    assert record.node("run-001").is_recoverable
    assert all(len(node.provenance.digest) == 64 for node in record.nodes)


def test_provenance_digests_are_stable_and_file_backed():
    text = "same artifact\n"
    assert digest_text(text) == digest_text(text)
    assert digest_file(FIXTURE_DIR / "triage.md") == sample_workflow_record().node("triage-001").provenance.digest
    provenance = Provenance.from_file(FIXTURE_DIR / "spec.md", captured_at=FIXED_TIME)
    assert provenance.content_digest == digest_file(FIXTURE_DIR / "spec.md")
    assert provenance.validate() is provenance


def test_validation_rejects_bad_digest_parallel_mode_and_dangling_edges():
    with pytest.raises(WorkflowValidationError, match="SHA-256"):
        Provenance(source_ref="local", digest="not-a-digest").validate()

    with pytest.raises(WorkflowValidationError, match="only serial"):
        WorkflowRecord.create("parallel", "/tmp/repo", automation_mode="parallel")

    record = WorkflowRecord.create("dangling", "/tmp/repo", nodes=[_node("ticket-1")])
    with pytest.raises(WorkflowValidationError, match="unknown node"):
        record.add_edge(WorkflowEdge("ticket-1", "missing", "blocks"))


def test_blocker_cycles_are_rejected():
    first = _node("ticket-1")
    second = _node("ticket-2")
    with pytest.raises(WorkflowValidationError, match="cycle"):
        WorkflowRecord.create(
            "cycle",
            "/tmp/repo",
            nodes=[first, second],
            edges=[WorkflowEdge("ticket-1", "ticket-2"), WorkflowEdge("ticket-2", "ticket-1")],
        )


def test_lifecycle_transitions_are_versioned_and_recoverable():
    record = WorkflowRecord.create("lifecycle", "/tmp/repo", nodes=[_node("run-1", "run", "failed")])

    queued = record.transition_node("run-1", "queued", reason="retry after inspection", now=FIXED_TIME)
    assert queued.revision == 1
    assert queued.node("run-1").status == "queued"
    assert queued.events[-1].reason == "retry after inspection"

    started = queued.transition_node("run-1", "running", now=FIXED_TIME)
    verifying = started.transition_node("run-1", "verifying", now=FIXED_TIME)
    checkpointing = verifying.transition_node("run-1", "checkpointing", now=FIXED_TIME)
    completed = checkpointing.transition_node("run-1", "completed", now=FIXED_TIME)
    assert [event.to_status for event in completed.events] == ["queued", "running", "verifying", "checkpointing", "completed"]
    assert completed.node("run-1").is_recoverable is False

    with pytest.raises(WorkflowValidationError, match="Invalid run node transition"):
        completed.transition_node("run-1", "queued")


def test_workflow_store_round_trips_atomically_and_checks_revisions(tmp_path: Path):
    store = WorkflowStore(tmp_path / "runtime")
    record = WorkflowRecord.create("stored", "/tmp/repo", nodes=[_node("ticket-1")])
    store.create(record)

    path = store.workflow_path("stored")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["record_digest"] == record.digest
    assert store.get("stored") == record
    assert not list(path.parent.glob(".stored.json.*"))

    updated = record.transition("ready", now=FIXED_TIME)
    store.save(updated, expected_revision=record.revision)
    assert store.get("stored").status == "ready"
    with pytest.raises(WorkflowConflictError, match="changed"):
        store.save(record, expected_revision=record.revision)

    raw["status"] = "running"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(WorkflowStoreError, match="Corrupt workflow"):
        store.get("stored")


def test_run_store_exposes_the_same_canonical_workflow_directory(tmp_path: Path):
    from long_running_tasks.store import RunStore

    run_store = RunStore(tmp_path / "runtime")
    record = WorkflowRecord.create("through-run-store", "/tmp/repo")
    run_store.create_workflow(record)
    assert run_store.workflow_path(record.workflow_id).is_file()
    assert run_store.get_workflow(record.workflow_id) == record
