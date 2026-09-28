from __future__ import annotations

from pathlib import Path

import pytest

from long_running_tasks.artifacts import (
    ArtifactReferenceError,
    ArtifactValidationError,
    LocalMattArtifactCatalog,
    load_spec_artifact,
    load_ticket_artifact,
)
from long_running_tasks.snapshots import (
    MarkdownSnapshot,
    MarkdownSnapshotRenderer,
    MarkdownSnapshotStore,
    SnapshotConflictError,
    SnapshotValidationError,
)
from long_running_tasks.workflow import digest_file


FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _write_source_tree(root: Path) -> None:
    feature = root / ".scratch" / "demo-feature"
    issues = feature / "issues"
    issues.mkdir(parents=True)
    (feature / "triage.md").write_text(
        "---\nstatus: ready-for-human\napproved: false\n---\n"
        "# Triage\n\n## Decision\n\nNeeds human review.\n",
        encoding="utf-8",
    )
    (feature / "spec.md").write_text(
        "# Spec\n\n## Goal\n\nKeep the local source immutable.\n",
        encoding="utf-8",
    )
    (issues / "01-foundation.md").write_text(
        "# 01: Foundation\n\n**Status:** ready-for-agent\n"
        "**Acceptance:**\n\n- [ ] Keep the digest.\n",
        encoding="utf-8",
    )
    (issues / "02-runner.md").write_text(
        "# 02: Runner\n\n## Blocked by\n\n- 01: Foundation\n\n"
        "## Status\n\nready-for-agent\n",
        encoding="utf-8",
    )


def test_local_catalog_adapts_matt_markdown_and_resolves_ticket_blockers(tmp_path: Path):
    _write_source_tree(tmp_path)
    catalog = LocalMattArtifactCatalog(tmp_path)

    artifacts = catalog.discover()
    assert {artifact.kind for artifact in artifacts} == {"triage", "spec", "ticket"}
    by_id = {artifact.node_id: artifact for artifact in artifacts}
    assert by_id["triage-demo-feature"].node.status == "needs-human"
    assert by_id["triage-demo-feature"].node.approved is False
    assert by_id["spec-demo-feature"].node.status == "current"
    assert by_id["spec-demo-feature"].section("goal") == "Keep the local source immutable."
    assert by_id["ticket-demo-feature-01-foundation"].node.status == "ready"
    assert by_id["ticket-demo-feature-01-foundation"].node.approved is True
    assert by_id["ticket-demo-feature-02-runner"].blocker_refs == ("01: Foundation",)
    assert (
        by_id["ticket-demo-feature-01-foundation"].node.provenance.digest
        == digest_file(tmp_path / ".scratch/demo-feature/issues/01-foundation.md")
    )
    assert by_id["ticket-demo-feature-01-foundation"].node.metadata["source_status"] == (
        "ready-for-agent"
    )

    nodes, edges = catalog.workflow_parts()
    assert len(nodes) == 4
    assert len(edges) == 1
    assert edges[0].source == "ticket-demo-feature-01-foundation"
    assert edges[0].target == "ticket-demo-feature-02-runner"
    record = catalog.as_workflow_record("local-demo", tmp_path, now=FIXED_TIME)
    assert record.node("spec-demo-feature").provenance.source_ref == ".scratch/demo-feature/spec.md"
    assert record.blockers_for("ticket-demo-feature-02-runner") == (
        "ticket-demo-feature-01-foundation",
    )


def test_adapter_marks_stale_source_and_never_writes_to_checkout(tmp_path: Path):
    source = tmp_path / ".scratch" / "feature" / "spec.md"
    source.parent.mkdir(parents=True)
    original = "# Spec\n\n**Status:** stale\n"
    source.write_text(original, encoding="utf-8")

    artifact = load_spec_artifact(source, root=tmp_path, captured_at=FIXED_TIME)
    assert artifact.node.is_stale
    assert artifact.node.is_current is False
    assert source.read_text(encoding="utf-8") == original
    assert artifact.node.updated_at == FIXED_TIME

    with pytest.raises(ArtifactValidationError, match="outside"):
        load_spec_artifact(tmp_path.parent / "outside.md", root=tmp_path)


def test_catalog_rejects_unknown_blocker_references(tmp_path: Path):
    _write_source_tree(tmp_path)
    ticket = tmp_path / ".scratch/demo-feature/issues/02-runner.md"
    ticket.write_text(
        "# 02: Runner\n\n**Status:** ready-for-agent\n**Blocked by:** missing\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactReferenceError, match="does not match"):
        LocalMattArtifactCatalog(tmp_path).edges()


def test_markdown_snapshot_round_trips_exact_source_and_is_idempotent(tmp_path: Path):
    source = tmp_path / ".scratch" / "feature" / "spec.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# Spec\r\n\r\nbody without a final newline")
    artifact = load_spec_artifact(source, root=tmp_path, captured_at=FIXED_TIME)
    snapshot = MarkdownSnapshotRenderer().render(artifact)

    assert snapshot.body == source.read_bytes().decode("utf-8")
    assert MarkdownSnapshot.from_markdown(snapshot.text) == snapshot
    store = MarkdownSnapshotStore(tmp_path / "snapshots")
    path = store.write(snapshot, "workflow-1")
    assert store.write(snapshot, "workflow-1") == path
    assert store.read(path) == snapshot

    path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SnapshotConflictError, match="different content"):
        store.write(snapshot, "workflow-1")


def test_snapshot_renderer_rejects_changed_workflow_body(tmp_path: Path):
    source = tmp_path / ".scratch" / "feature" / "spec.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Spec\n", encoding="utf-8")
    node = load_spec_artifact(source, root=tmp_path, captured_at=FIXED_TIME).node

    with pytest.raises(SnapshotValidationError, match="provenance digest"):
        MarkdownSnapshotRenderer().render(node, "# Changed\n")

    with pytest.raises(SnapshotValidationError, match="outside"):
        MarkdownSnapshotStore(tmp_path / "snapshots").read(tmp_path / "not-a-snapshot.md")
