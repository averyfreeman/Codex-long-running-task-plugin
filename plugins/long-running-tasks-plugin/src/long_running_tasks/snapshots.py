"""Immutable Markdown snapshots for local workflow source artifacts.

Snapshots are execution inputs, not working copies.  The renderer preserves
the exact UTF-8 Markdown body and prefixes it with a deterministic identity
header.  The store creates a snapshot once, permits an identical retry, and
refuses every conflicting rewrite.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .store import default_data_dir
from .workflow import (
    WorkflowNode,
    canonical_json,
    digest_text,
)


SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_MARKER = "<!-- long-running-tasks markdown snapshot:"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HEADER_RE = re.compile(
    r"^<!-- long-running-tasks markdown snapshot:\s*(?P<metadata>\{.*\}) -->$"
)
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SOURCE_KINDS = frozenset({"triage", "spec", "ticket"})


class SnapshotError(RuntimeError):
    """Base error for Markdown snapshot failures."""


class SnapshotConflictError(SnapshotError):
    """Raised when an immutable snapshot path contains different content."""


class SnapshotValidationError(SnapshotError, ValueError):
    """Raised when a snapshot header or source projection is invalid."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotValidationError(f"{field_name} must be a non-empty string")
    return value


def _validate_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise SnapshotValidationError(
            f"{field_name} must be a 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True)
class MarkdownSnapshot:
    """The rendered body and immutable source identity for one artifact."""

    node_id: str
    kind: str
    title: str
    source_ref: str
    source_digest: str
    source_revision: str | None
    body: str
    text: str
    digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def snapshot_digest(self) -> str:
        return self.digest

    @property
    def content(self) -> str:
        return self.text

    def validate(self) -> "MarkdownSnapshot":
        _require_text(self.node_id, "snapshot.node_id")
        if self.kind not in _SOURCE_KINDS:
            raise SnapshotValidationError(
                f"Markdown snapshots support only source kinds: {sorted(_SOURCE_KINDS)}"
            )
        _require_text(self.title, "snapshot.title")
        _require_text(self.source_ref, "snapshot.source_ref")
        _validate_digest(self.source_digest, "snapshot.source_digest")
        if self.source_revision is not None:
            _require_text(self.source_revision, "snapshot.source_revision")
        if not isinstance(self.body, str):
            raise SnapshotValidationError("snapshot.body must be text")
        if not isinstance(self.text, str):
            raise SnapshotValidationError("snapshot.text must be text")
        if not self.text.startswith(SNAPSHOT_MARKER):
            raise SnapshotValidationError("snapshot.text is missing its identity header")
        first_newline = self.text.find("\n")
        header = self.text[:first_newline].rstrip("\r") if first_newline >= 0 else ""
        header_match = _HEADER_RE.fullmatch(header)
        if not header_match:
            raise SnapshotValidationError("snapshot.text has an invalid identity header")
        try:
            header_metadata = json.loads(header_match.group("metadata"))
        except json.JSONDecodeError as exc:
            raise SnapshotValidationError(
                "snapshot.text identity header is not valid JSON"
            ) from exc
        if not isinstance(header_metadata, Mapping):
            raise SnapshotValidationError("snapshot.text identity header must be an object")
        if header_metadata.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotValidationError("snapshot.text has an unsupported schema version")
        for field_name in (
            "node_id",
            "kind",
            "title",
            "source_ref",
            "source_digest",
            "source_revision",
        ):
            if header_metadata.get(field_name) != getattr(self, field_name):
                raise SnapshotValidationError(
                    f"snapshot identity header does not match {field_name}"
                )
        if digest_text(self.body) != self.source_digest:
            raise SnapshotValidationError(
                "snapshot.body does not match snapshot.source_digest"
            )
        if digest_text(self.text) != self.digest:
            raise SnapshotValidationError("snapshot.digest does not match snapshot.text")
        if not isinstance(self.metadata, Mapping):
            raise SnapshotValidationError("snapshot.metadata must be an object")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "node_id": self.node_id,
            "kind": self.kind,
            "title": self.title,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "source_revision": self.source_revision,
            "body": self.body,
            "text": self.text,
            "digest": self.digest,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_markdown(cls, text: str) -> "MarkdownSnapshot":
        if not isinstance(text, str):
            raise SnapshotValidationError("snapshot text must be text")
        first_newline = text.find("\n")
        if first_newline < 0:
            raise SnapshotValidationError("snapshot is missing its Markdown body")
        first_line = text[:first_newline].rstrip("\r")
        match = _HEADER_RE.fullmatch(first_line)
        if not match:
            raise SnapshotValidationError("snapshot has an invalid identity header")
        if text[first_newline + 1 : first_newline + 2] != "\n":
            raise SnapshotValidationError("snapshot header must be followed by a blank line")
        try:
            metadata = json.loads(match.group("metadata"))
        except json.JSONDecodeError as exc:
            raise SnapshotValidationError("snapshot identity header is not valid JSON") from exc
        if not isinstance(metadata, Mapping):
            raise SnapshotValidationError("snapshot identity header must contain an object")
        if metadata.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotValidationError(
                f"Unsupported snapshot schema version: {metadata.get('schema_version')}"
            )
        body = text[first_newline + 2 :]
        snapshot = cls(
            node_id=metadata.get("node_id"),
            kind=metadata.get("kind"),
            title=metadata.get("title"),
            source_ref=metadata.get("source_ref"),
            source_digest=metadata.get("source_digest"),
            source_revision=metadata.get("source_revision"),
            body=body,
            text=text,
            digest=digest_text(text),
            metadata=dict(metadata),
        )
        return snapshot.validate()


class MarkdownSnapshotRenderer:
    """Render a source node or adapted local artifact deterministically."""

    def render(
        self,
        source: WorkflowNode | Any,
        body: str | None = None,
    ) -> MarkdownSnapshot:
        artifact_body = getattr(source, "text", None)
        node = getattr(source, "node", source)
        if not isinstance(node, WorkflowNode):
            raise SnapshotValidationError(
                "snapshot source must be a WorkflowNode or LocalMattArtifact"
            )
        if body is None:
            body = artifact_body
        if body is None:
            raise SnapshotValidationError(
                "body is required when rendering a WorkflowNode snapshot"
            )
        if not isinstance(body, str):
            raise SnapshotValidationError("snapshot body must be text")
        node.validate()
        if node.kind not in _SOURCE_KINDS:
            raise SnapshotValidationError(
                f"Markdown snapshots do not support node kind: {node.kind}"
            )
        if digest_text(body) != node.provenance.digest:
            raise SnapshotValidationError(
                "snapshot body does not match the node provenance digest"
            )
        metadata = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "node_id": node.node_id,
            "kind": node.kind,
            "title": node.title,
            "source_ref": node.provenance.source_ref,
            "source_digest": node.provenance.digest,
            "source_revision": node.provenance.source_revision,
        }
        text = f"{SNAPSHOT_MARKER} {canonical_json(metadata)} -->\n\n{body}"
        snapshot = MarkdownSnapshot(
            node_id=node.node_id,
            kind=node.kind,
            title=node.title,
            source_ref=node.provenance.source_ref,
            source_digest=node.provenance.digest,
            source_revision=node.provenance.source_revision,
            body=body,
            text=text,
            digest=digest_text(text),
            metadata=metadata,
        )
        return snapshot.validate()


def _safe_segment(value: str, field_name: str) -> str:
    _require_text(value, field_name)
    if not _SAFE_SEGMENT_RE.fullmatch(value) or value in {".", ".."}:
        raise SnapshotValidationError(
            f"{field_name} must be a safe filename segment"
        )
    return value


class MarkdownSnapshotStore:
    """Create and read immutable source snapshots below a local data root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (default_data_dir() / "snapshots" / "workflows")).resolve()

    def snapshot_path(
        self,
        workflow_id: str,
        node_id: str,
        kind: str,
    ) -> Path:
        _safe_segment(workflow_id, "workflow_id")
        _safe_segment(kind, "kind")
        _safe_segment(node_id, "node_id")
        if kind not in _SOURCE_KINDS:
            raise SnapshotValidationError(f"Unsupported snapshot kind: {kind}")
        return self.root / workflow_id / kind / f"{node_id}.md"

    def _validate_path(self, path: str | Path) -> Path:
        selected = Path(path).resolve()
        try:
            selected.relative_to(self.root)
        except ValueError as exc:
            raise SnapshotValidationError(
                f"Snapshot path is outside the snapshot store: {selected}"
            ) from exc
        return selected

    @staticmethod
    def _same_file(path: Path, text: str) -> bool:
        try:
            return path.read_bytes() == text.encode("utf-8")
        except FileNotFoundError:
            return False

    def write(
        self,
        snapshot: MarkdownSnapshot,
        workflow_id: str,
        *,
        path: str | Path | None = None,
    ) -> Path:
        if not isinstance(snapshot, MarkdownSnapshot):
            raise SnapshotValidationError("snapshot must be a MarkdownSnapshot")
        snapshot.validate()
        target = (
            self.snapshot_path(workflow_id, snapshot.node_id, snapshot.kind)
            if path is None
            else self._validate_path(path)
        )
        target = self._validate_path(target)
        encoded = snapshot.text.encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            if self._same_file(target, snapshot.text):
                return target
            raise SnapshotConflictError(
                f"Immutable Markdown snapshot already exists with different content: {target}"
            )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            raise
        return target

    def read(self, path: str | Path) -> MarkdownSnapshot:
        selected = self._validate_path(path)
        try:
            text = selected.read_bytes().decode("utf-8")
        except FileNotFoundError as exc:
            raise SnapshotError(f"Markdown snapshot does not exist: {selected}") from exc
        except UnicodeDecodeError as exc:
            raise SnapshotValidationError(
                f"Markdown snapshot is not valid UTF-8: {selected}"
            ) from exc
        try:
            return MarkdownSnapshot.from_markdown(text)
        except SnapshotValidationError as exc:
            raise SnapshotValidationError(
                f"Invalid Markdown snapshot {selected}: {exc}"
            ) from exc

    def get(
        self,
        workflow_id: str,
        node_id: str,
        kind: str,
    ) -> MarkdownSnapshot | None:
        path = self.snapshot_path(workflow_id, node_id, kind)
        if not path.is_file():
            return None
        return self.read(path)

    def write_artifact(
        self,
        artifact: Any,
        workflow_id: str,
    ) -> Path:
        return self.write(MarkdownSnapshotRenderer().render(artifact), workflow_id)

    save = write


def render_markdown_snapshot(
    source: WorkflowNode | Any,
    body: str | None = None,
) -> MarkdownSnapshot:
    return MarkdownSnapshotRenderer().render(source, body)


def write_markdown_snapshot(
    source: WorkflowNode | Any,
    workflow_id: str,
    *,
    store: MarkdownSnapshotStore | None = None,
    body: str | None = None,
) -> Path:
    snapshot = MarkdownSnapshotRenderer().render(source, body)
    return (store or MarkdownSnapshotStore()).write(snapshot, workflow_id)


SnapshotRenderer = MarkdownSnapshotRenderer
SnapshotStore = MarkdownSnapshotStore
MarkdownArtifactSnapshot = MarkdownSnapshot


__all__ = [
    "MarkdownArtifactSnapshot",
    "MarkdownSnapshot",
    "MarkdownSnapshotRenderer",
    "MarkdownSnapshotStore",
    "SNAPSHOT_MARKER",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotRenderer",
    "SnapshotStore",
    "SnapshotValidationError",
    "render_markdown_snapshot",
    "write_markdown_snapshot",
]
