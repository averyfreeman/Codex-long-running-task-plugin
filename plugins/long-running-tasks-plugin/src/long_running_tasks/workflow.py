"""The local, versioned workflow record used by the complementary workflow.

The workflow record is intentionally a small serial-first data model.  It is
not a replacement for the direct runner record in :mod:`long_running_tasks.store`;
it is the local coordination record that can point at source artifacts and run
artifacts without taking ownership of those systems.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .state import TRANSITIONS as RUN_TRANSITIONS
from .state import utc_now
from .store import atomic_write_json, default_data_dir


WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_RECORD_VERSION = WORKFLOW_SCHEMA_VERSION


class WorkflowError(RuntimeError):
    """Base error for invalid or unavailable workflow records."""


class WorkflowValidationError(ValueError, WorkflowError):
    """Raised when a workflow record cannot satisfy its data contract."""


class WorkflowTransitionError(WorkflowValidationError):
    """Raised when a lifecycle transition is not allowed."""


class UnsupportedAutomationModeError(WorkflowValidationError):
    """Raised when a workflow requests an automation mode not implemented here."""


class UnsupportedParallelSchedulingError(UnsupportedAutomationModeError):
    """Raised because parallel scheduling is intentionally not implemented."""


UnsupportedParallelModeError = UnsupportedParallelSchedulingError


class WorkflowStoreError(WorkflowError):
    """Raised when a workflow record cannot be read or persisted safely."""


class WorkflowConflictError(WorkflowStoreError):
    """Raised when a caller tries to write an older workflow revision."""


NODE_KINDS = frozenset({"triage", "spec", "ticket", "run"})
EDGE_KINDS = frozenset({"blocks", "derived_from", "produces", "executes", "supersedes", "references"})


class AutomationMode(str, Enum):
    """Explicit orchestration modes understood by the workflow interface."""

    SERIAL = "serial"
    PARALLEL = "parallel"

WORKFLOW_STATUSES = frozenset(
    {
        "draft",
        "queued",
        "ready",
        "running",
        "blocked",
        "stale",
        "paused",
        "failed",
        "interrupted",
        "needs_recovery",
        "completed",
        "cancelled",
    }
)

# The workflow lifecycle is separate from the run lifecycle.  In particular,
# failed and interrupted records remain valid records and can be moved back to
# a runnable state after an explicit review.
WORKFLOW_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"queued", "ready", "blocked", "stale", "cancelled"},
    "queued": {"running", "blocked", "stale", "failed", "interrupted", "cancelled", "needs_recovery"},
    "ready": {"queued", "running", "blocked", "stale", "failed", "interrupted", "cancelled"},
    "running": {"running", "blocked", "stale", "paused", "failed", "interrupted", "needs_recovery", "completed", "cancelled"},
    "blocked": {"ready", "queued", "stale", "cancelled"},
    "stale": {"draft", "ready", "queued", "blocked", "cancelled"},
    "paused": {"ready", "queued", "running", "needs_recovery", "cancelled"},
    "failed": {"ready", "queued", "running", "interrupted", "needs_recovery", "cancelled"},
    "interrupted": {"ready", "queued", "running", "failed", "needs_recovery", "cancelled"},
    "needs_recovery": {"ready", "queued", "running", "failed", "interrupted", "cancelled"},
    "completed": set(),
    "cancelled": {"draft", "ready", "queued"},
}

SOURCE_NODE_STATUSES = frozenset(
    {
        "draft",
        "proposed",
        "needs-triage",
        "needs-more-information",
        "ready-for-afk",
        "needs-human",
        "not-doing",
        "approved",
        "current",
        "ready",
        "blocked",
        "running",
        "completed",
        "failed",
        "cancelled",
        "stale",
        "rejected",
        "superseded",
    }
)
RUN_NODE_STATUSES = frozenset(set(RUN_TRANSITIONS) | {"interrupted"})
NODE_STATUSES: dict[str, frozenset[str]] = {
    "triage": SOURCE_NODE_STATUSES,
    "spec": SOURCE_NODE_STATUSES,
    "ticket": SOURCE_NODE_STATUSES,
    "run": RUN_NODE_STATUSES,
}

APPROVED_STATUSES = frozenset({"approved", "current", "ready"})
RESOLVED_BLOCKER_STATUSES = frozenset({"completed"})

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for all provenance hashes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    """Return a bare lower-case SHA-256 digest for bytes."""

    return hashlib.sha256(value).hexdigest()


def digest_text(value: str) -> str:
    """Return a SHA-256 digest for UTF-8 text."""

    return digest_bytes(value.encode("utf-8"))


def digest_json(value: Any) -> str:
    """Return a SHA-256 digest for a canonical JSON value."""

    return digest_text(canonical_json(value))


def digest_file(path: Path) -> str:
    """Return a SHA-256 digest for a local file without changing it."""

    return digest_bytes(Path(path).read_bytes())


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowValidationError("metadata must be a JSON object")
    copied = copy.deepcopy(dict(value))
    try:
        json.dumps(copied)
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError("metadata must contain JSON values") from exc
    return copied


def _validate_timestamp(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError(f"{field_name} must be a non-empty ISO timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowValidationError(f"{field_name} must be an ISO timestamp") from exc


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Provenance:
    """The content identity and local source reference for one artifact."""

    source_ref: str
    digest: str
    algorithm: str = "sha256"
    captured_at: str = field(default_factory=utc_now)
    source_revision: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    immutable: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.source_ref, Path):
            object.__setattr__(self, "source_ref", str(self.source_ref))
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))

    @classmethod
    def from_text(
        cls,
        source_ref: str,
        text: str,
        *,
        captured_at: str | None = None,
        source_revision: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Provenance":
        return cls(
            source_ref=source_ref,
            digest=digest_text(text),
            captured_at=captured_at or utc_now(),
            source_revision=source_revision,
            metadata=metadata or {},
        )

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        source_ref: str | None = None,
        captured_at: str | None = None,
        source_revision: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Provenance":
        path = Path(path)
        return cls(
            source_ref=source_ref or str(path),
            digest=digest_file(path),
            captured_at=captured_at or utc_now(),
            source_revision=source_revision,
            metadata=metadata or {},
        )

    @property
    def content_digest(self) -> str:
        """Compatibility name for callers that distinguish content from record digests."""

        return self.digest

    def validate(self) -> "Provenance":
        _require_text(self.source_ref, "provenance.source_ref")
        if self.algorithm != "sha256":
            raise WorkflowValidationError("provenance.algorithm must be sha256")
        if not isinstance(self.digest, str) or not _DIGEST_RE.fullmatch(self.digest):
            raise WorkflowValidationError("provenance.digest must be a 64-character SHA-256 hex digest")
        _validate_timestamp(self.captured_at, "provenance.captured_at")
        if self.source_revision is not None:
            _require_text(self.source_revision, "provenance.source_revision")
        if not isinstance(self.immutable, bool):
            raise WorkflowValidationError("provenance.immutable must be boolean")
        _copy_mapping(self.metadata)
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "source_ref": self.source_ref,
            "digest": self.digest,
            "algorithm": self.algorithm,
            "captured_at": self.captured_at,
            "source_revision": self.source_revision,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "immutable": self.immutable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Provenance":
        if not isinstance(value, Mapping):
            raise WorkflowValidationError("provenance must be a JSON object")
        source_ref = value.get("source_ref", value.get("source_path", value.get("source")))
        digest = value.get("digest", value.get("content_digest"))
        result = cls(
            source_ref=source_ref,
            digest=digest,
            algorithm=value.get("algorithm", "sha256"),
            captured_at=value.get("captured_at", utc_now()),
            source_revision=value.get("source_revision"),
            metadata=value.get("metadata", {}),
            immutable=value.get("immutable", True),
        )
        return result.validate()


@dataclass(frozen=True)
class WorkflowNode:
    """One versioned triage, spec, ticket, or run artifact."""

    node_id: str
    kind: str
    status: str
    title: str
    provenance: Provenance
    artifact: str | None = None
    current: bool = True
    approved: bool = False
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.artifact, Path):
            object.__setattr__(self, "artifact", str(self.artifact))
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))

    @property
    def id(self) -> str:
        return self.node_id

    @property
    def is_current(self) -> bool:
        return self.current and self.status not in {"stale", "superseded", "rejected"}

    @property
    def is_approved(self) -> bool:
        return self.approved or self.status in APPROVED_STATUSES

    @property
    def is_stale(self) -> bool:
        return self.status == "stale"

    @property
    def node_type(self) -> str:
        return self.kind

    @property
    def provenance_digest(self) -> str:
        return self.provenance.digest

    @property
    def is_recoverable(self) -> bool:
        return self.kind == "run" and self.status in {
            "failed",
            "interrupted",
            "needs_recovery",
            "paused",
            "cancelled",
        }

    def validate(self) -> "WorkflowNode":
        _require_text(self.node_id, "node.node_id")
        _require_text(self.kind, "node.kind")
        if self.kind not in NODE_KINDS:
            raise WorkflowValidationError(f"Unsupported workflow node kind: {self.kind}")
        if self.status not in NODE_STATUSES[self.kind]:
            raise WorkflowValidationError(f"Unsupported {self.kind} node status: {self.status}")
        _require_text(self.title, "node.title")
        if not isinstance(self.provenance, Provenance):
            raise WorkflowValidationError(f"{self.node_id} must have provenance")
        self.provenance.validate()
        if self.artifact is not None:
            _require_text(self.artifact, "node.artifact")
        if not isinstance(self.current, bool):
            raise WorkflowValidationError(f"{self.node_id}.current must be boolean")
        if not isinstance(self.approved, bool):
            raise WorkflowValidationError(f"{self.node_id}.approved must be boolean")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise WorkflowValidationError(f"{self.node_id}.revision must be a non-negative integer")
        _validate_timestamp(self.created_at, f"{self.node_id}.created_at")
        _validate_timestamp(self.updated_at, f"{self.node_id}.updated_at")
        _copy_mapping(self.metadata)
        return self

    def transition(self, new_status: str, *, now: str | None = None) -> "WorkflowNode":
        self.validate()
        if new_status not in NODE_STATUSES[self.kind]:
            raise WorkflowTransitionError(f"Unsupported {self.kind} node status: {new_status}")
        if new_status == self.status:
            return self

        if self.kind == "run":
            allowed = set(RUN_TRANSITIONS.get(self.status, set()))
            if self.status == "interrupted":
                allowed = {"queued", "running", "failed", "needs_recovery", "paused", "cancelled"}
        else:
            allowed = {
                "draft": {"proposed", "needs-triage", "approved", "rejected", "stale", "superseded"},
                "proposed": {"approved", "rejected", "stale", "superseded"},
                "needs-triage": {"needs-more-information", "ready-for-afk", "needs-human", "not-doing", "approved", "stale"},
                "needs-more-information": {"needs-triage", "ready-for-afk", "needs-human", "approved", "stale"},
                "ready-for-afk": {"approved", "needs-human", "stale", "superseded"},
                "needs-human": {"approved", "rejected", "stale", "superseded"},
                "not-doing": {"draft", "superseded"},
                "approved": {"current", "ready", "blocked", "running", "completed", "failed", "stale", "superseded"},
                "current": {"approved", "ready", "blocked", "running", "completed", "failed", "stale", "superseded"},
                "ready": {"approved", "current", "blocked", "running", "completed", "failed", "stale", "superseded"},
                "blocked": {"approved", "current", "ready", "stale", "superseded"},
                "running": {"completed", "failed", "cancelled", "stale", "interrupted"},
                "completed": {"stale", "superseded"},
                "failed": {"approved", "current", "ready", "running", "stale", "superseded"},
                "cancelled": {"approved", "current", "ready", "stale", "superseded"},
                "stale": {"draft", "proposed", "approved", "current", "ready", "blocked", "superseded"},
                "rejected": {"draft", "proposed", "superseded"},
                "superseded": set(),
            }.get(self.status, set())
        if new_status not in allowed:
            raise WorkflowTransitionError(f"Invalid {self.kind} node transition {self.status} -> {new_status}")
        return replace(self, status=new_status, revision=self.revision + 1, updated_at=now or utc_now())

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "status": self.status,
            "title": self.title,
            "artifact": self.artifact,
            "current": self.current,
            "approved": self.approved,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": self.provenance.to_dict(),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, default_kind: str | None = None) -> "WorkflowNode":
        if not isinstance(value, Mapping):
            raise WorkflowValidationError("workflow node must be a JSON object")
        provenance_value = value.get("provenance")
        if isinstance(provenance_value, str):
            provenance_value = {"source_ref": value.get("artifact", value.get("node_id", "artifact")), "digest": provenance_value}
        node = cls(
            node_id=value.get("node_id", value.get("id")),
            kind=value.get("kind", value.get("node_type", default_kind)),
            status=value.get("status", "draft"),
            title=value.get("title", value.get("name", value.get("node_id", value.get("id", "")))),
            provenance=Provenance.from_dict(provenance_value or {}),
            artifact=value.get("artifact", value.get("artifact_ref")),
            current=value.get("current", True),
            approved=value.get("approved", False),
            revision=value.get("revision", 0),
            created_at=value.get("created_at", utc_now()),
            updated_at=value.get("updated_at", utc_now()),
            metadata=value.get("metadata", {}),
        )
        return node.validate()


@dataclass(frozen=True)
class WorkflowEdge:
    """A typed relationship between two workflow nodes.

    For ``blocks`` edges, ``source`` is the blocker and ``target`` is the
    blocked node.  The direction is explicit so the record remains useful to
    a later scheduler without duplicating blocker lists on every node.
    """

    source: str
    target: str
    kind: str = "blocks"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))

    @property
    def from_node(self) -> str:
        return self.source

    @property
    def to_node(self) -> str:
        return self.target

    @property
    def relation(self) -> str:
        return self.kind

    def validate(self) -> "WorkflowEdge":
        _require_text(self.source, "edge.source")
        _require_text(self.target, "edge.target")
        if self.source == self.target:
            raise WorkflowValidationError(f"Edge cannot point from {self.source} to itself")
        if self.kind not in EDGE_KINDS:
            raise WorkflowValidationError(f"Unsupported workflow edge kind: {self.kind}")
        _copy_mapping(self.metadata)
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowEdge":
        if not isinstance(value, Mapping):
            raise WorkflowValidationError("workflow edge must be a JSON object")
        edge = cls(
            source=value.get("source", value.get("from", value.get("from_node"))),
            target=value.get("target", value.get("to", value.get("to_node"))),
            kind=value.get("kind", value.get("relation", "blocks")),
            metadata=value.get("metadata", {}),
        )
        return edge.validate()


@dataclass(frozen=True)
class LifecycleEvent:
    """An append-only transition event retained with the workflow record."""

    entity_id: str
    entity_kind: str
    from_status: str
    to_status: str
    timestamp: str = field(default_factory=utc_now)
    reason: str | None = None

    def validate(self) -> "LifecycleEvent":
        _require_text(self.entity_id, "event.entity_id")
        _require_text(self.entity_kind, "event.entity_kind")
        _require_text(self.from_status, "event.from_status")
        _require_text(self.to_status, "event.to_status")
        _validate_timestamp(self.timestamp, "event.timestamp")
        if self.reason is not None:
            _require_text(self.reason, "event.reason")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LifecycleEvent":
        if not isinstance(value, Mapping):
            raise WorkflowValidationError("workflow event must be a JSON object")
        return cls(
            entity_id=value.get("entity_id", value.get("node_id", "workflow")),
            entity_kind=value.get("entity_kind", value.get("kind", "workflow")),
            from_status=value.get("from_status", ""),
            to_status=value.get("to_status", ""),
            timestamp=value.get("timestamp", utc_now()),
            reason=value.get("reason"),
        ).validate()


WorkflowEvent = LifecycleEvent
ArtifactProvenance = Provenance
BlockerEdge = WorkflowEdge


@dataclass(frozen=True)
class WorkflowRecord:
    """The canonical local workflow record.

    Records are immutable at the Python API boundary: lifecycle operations
    return a new record with an incremented revision.  Persistence is handled
    by :class:`WorkflowStore`, which writes the serialized record atomically.
    """

    workflow_id: str
    repo_path: str
    nodes: tuple[WorkflowNode, ...] = field(default_factory=tuple)
    edges: tuple[WorkflowEdge, ...] = field(default_factory=tuple)
    status: str = "draft"
    automation_mode: str = "serial"
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    events: tuple[LifecycleEvent, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = WORKFLOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.repo_path, Path):
            object.__setattr__(self, "repo_path", str(self.repo_path))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "metadata", _copy_mapping(self.metadata))

    @property
    def version(self) -> int:
        return self.schema_version

    @property
    def record_version(self) -> int:
        return self.schema_version

    @property
    def repository_path(self) -> str:
        return self.repo_path

    @property
    def digest(self) -> str:
        return digest_json(self._payload())

    @property
    def record_digest(self) -> str:
        return self.digest

    @property
    def history(self) -> tuple[LifecycleEvent, ...]:
        return self.events

    @classmethod
    def create(
        cls,
        workflow_id: str,
        repo_path: str,
        *,
        nodes: Iterable[WorkflowNode] = (),
        edges: Iterable[WorkflowEdge] = (),
        status: str = "draft",
        automation_mode: str = "serial",
        metadata: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> "WorkflowRecord":
        timestamp = now or utc_now()
        record = cls(
            workflow_id=workflow_id,
            repo_path=repo_path,
            nodes=tuple(nodes),
            edges=tuple(edges),
            status=status,
            automation_mode=automation_mode,
            metadata=metadata or {},
            created_at=timestamp,
            updated_at=timestamp,
        )
        return record.validate()

    def validate(self) -> "WorkflowRecord":
        _require_text(self.workflow_id, "workflow.workflow_id")
        _require_text(self.repo_path, "workflow.repo_path")
        if "://" in self.repo_path or self.repo_path.startswith("git@"):
            raise WorkflowValidationError("workflow.repo_path must be a local path")
        if self.status not in WORKFLOW_STATUSES:
            raise WorkflowValidationError(f"Unsupported workflow status: {self.status}")
        try:
            automation_mode = AutomationMode(self.automation_mode)
        except (TypeError, ValueError) as exc:
            raise UnsupportedAutomationModeError(
                f"Unsupported automation mode {self.automation_mode!r}; only serial scheduling is supported"
            ) from exc
        if automation_mode is not AutomationMode.SERIAL:
            raise UnsupportedParallelSchedulingError(
                f"Unsupported automation mode {self.automation_mode!r}; only serial scheduling is supported"
            )
        if not isinstance(self.schema_version, int) or self.schema_version != WORKFLOW_SCHEMA_VERSION:
            raise WorkflowValidationError(
                f"Unsupported workflow schema version: {self.schema_version}; expected {WORKFLOW_SCHEMA_VERSION}"
            )
        if not isinstance(self.revision, int) or self.revision < 0:
            raise WorkflowValidationError("workflow.revision must be a non-negative integer")
        _validate_timestamp(self.created_at, "workflow.created_at")
        _validate_timestamp(self.updated_at, "workflow.updated_at")
        _copy_mapping(self.metadata)

        node_ids: set[str] = set()
        for node in self.nodes:
            if not isinstance(node, WorkflowNode):
                raise WorkflowValidationError("workflow.nodes must contain WorkflowNode values")
            node.validate()
            if node.node_id in node_ids:
                raise WorkflowValidationError(f"Duplicate workflow node ID: {node.node_id}")
            node_ids.add(node.node_id)

        edge_keys: set[tuple[str, str, str]] = set()
        blockers: dict[str, set[str]] = {}
        for edge in self.edges:
            if not isinstance(edge, WorkflowEdge):
                raise WorkflowValidationError("workflow.edges must contain WorkflowEdge values")
            edge.validate()
            if edge.source not in node_ids or edge.target not in node_ids:
                raise WorkflowValidationError(
                    f"Workflow edge {edge.source!r} -> {edge.target!r} references an unknown node"
                )
            key = (edge.source, edge.target, edge.kind)
            if key in edge_keys:
                raise WorkflowValidationError(f"Duplicate workflow edge: {key}")
            edge_keys.add(key)
            if edge.kind == "blocks":
                blockers.setdefault(edge.source, set()).add(edge.target)

        # A blocker cycle can never make progress, so reject it at the record
        # boundary instead of leaving a permanently ambiguous local record.
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise WorkflowValidationError("Workflow blocker edges contain a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for blocked_id in blockers.get(node_id, set()):
                visit(blocked_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_ids:
            visit(node_id)

        for event in self.events:
            if not isinstance(event, LifecycleEvent):
                raise WorkflowValidationError("workflow.events must contain LifecycleEvent values")
            event.validate()
            if event.entity_id != self.workflow_id and event.entity_id not in node_ids:
                raise WorkflowValidationError(f"Workflow event references an unknown entity: {event.entity_id}")
        return self

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "repo_path": self.repo_path,
            "status": self.status,
            "automation_mode": self.automation_mode,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "events": [event.to_dict() for event in self.events],
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    def to_dict(self, *, include_record_digest: bool = True) -> dict[str, Any]:
        self.validate()
        payload = self._payload()
        if include_record_digest:
            payload["record_digest"] = self.digest
        return payload

    def node(self, node_id: str) -> WorkflowNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise WorkflowValidationError(f"Unknown workflow node: {node_id}")

    def blockers_for(self, node_id: str, *, include_resolved: bool = False) -> tuple[str, ...]:
        self.node(node_id)
        result: list[str] = []
        for edge in self.edges:
            if edge.kind != "blocks" or edge.target != node_id:
                continue
            if include_resolved or self.node(edge.source).status not in RESOLVED_BLOCKER_STATUSES:
                result.append(edge.source)
        return tuple(result)

    def is_unblocked(self, node_id: str) -> bool:
        return not self.blockers_for(node_id)

    @property
    def stale(self) -> bool:
        return self.status == "stale" or any(
            node.is_stale
            for node in self.nodes
        )

    @property
    def requires_reconciliation(self) -> bool:
        return self.stale

    @property
    def stale_source_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(
            node
            for node in self.nodes
            if node.kind in {"triage", "spec", "ticket"}
            and node.is_stale
        )

    def propagate_stale(
        self,
        *,
        now: str | None = None,
        reason: str = "stale source artifact requires reconciliation",
    ) -> "WorkflowRecord":
        """Return a copy with stale source dependencies propagated downstream."""

        from .scheduler import propagate_stale

        return propagate_stale(self, now=now, reason=reason)

    def select_frontier(
        self,
        *,
        automation_mode: str | AutomationMode | None = None,
    ) -> tuple[WorkflowNode, ...]:
        """Select the next serial frontier ticket without mutating this record."""

        from .scheduler import select_frontier

        return select_frontier(self, automation_mode=automation_mode)

    def _with_update(
        self,
        *,
        nodes: Iterable[WorkflowNode] | None = None,
        edges: Iterable[WorkflowEdge] | None = None,
        status: str | None = None,
        events: Iterable[LifecycleEvent] | None = None,
        revision_increment: bool = True,
        now: str | None = None,
    ) -> "WorkflowRecord":
        return replace(
            self,
            nodes=tuple(self.nodes if nodes is None else nodes),
            edges=tuple(self.edges if edges is None else edges),
            status=self.status if status is None else status,
            events=tuple(self.events if events is None else events),
            revision=self.revision + (1 if revision_increment else 0),
            updated_at=now or utc_now(),
        ).validate()

    def add_node(self, node: WorkflowNode, *, now: str | None = None) -> "WorkflowRecord":
        self.validate()
        node.validate()
        if any(existing.node_id == node.node_id for existing in self.nodes):
            raise WorkflowValidationError(f"Duplicate workflow node ID: {node.node_id}")
        return self._with_update(nodes=(*self.nodes, node), now=now)

    def add_edge(self, edge: WorkflowEdge, *, now: str | None = None) -> "WorkflowRecord":
        self.validate()
        edge.validate()
        return self._with_update(edges=(*self.edges, edge), now=now)

    def transition(self, new_status: str, *, reason: str | None = None, now: str | None = None) -> "WorkflowRecord":
        self.validate()
        if new_status not in WORKFLOW_STATUSES:
            raise WorkflowTransitionError(f"Unsupported workflow status: {new_status}")
        if new_status == self.status:
            return self
        if new_status not in WORKFLOW_TRANSITIONS[self.status]:
            raise WorkflowTransitionError(f"Invalid workflow transition {self.status} -> {new_status}")
        timestamp = now or utc_now()
        event = LifecycleEvent(
            entity_id=self.workflow_id,
            entity_kind="workflow",
            from_status=self.status,
            to_status=new_status,
            timestamp=timestamp,
            reason=reason,
        )
        return self._with_update(status=new_status, events=(*self.events, event), now=timestamp)

    def transition_node(
        self,
        node_id: str,
        new_status: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> "WorkflowRecord":
        self.validate()
        current = self.node(node_id)
        timestamp = now or utc_now()
        updated = current.transition(new_status, now=timestamp)
        if updated == current:
            return self
        nodes = tuple(updated if node.node_id == node_id else node for node in self.nodes)
        event = LifecycleEvent(
            entity_id=node_id,
            entity_kind=current.kind,
            from_status=current.status,
            to_status=updated.status,
            timestamp=timestamp,
            reason=reason,
        )
        return self._with_update(nodes=nodes, events=(*self.events, event), now=timestamp)

    def transition_workflow(self, new_status: str, *, reason: str | None = None, now: str | None = None) -> "WorkflowRecord":
        """Method-form alias for callers that want to name the aggregate explicitly."""

        return self.transition(new_status, reason=reason, now=now)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowRecord":
        if not isinstance(value, Mapping):
            raise WorkflowValidationError("workflow record must be a JSON object")
        raw_nodes = value.get("nodes", [])
        node_values: list[WorkflowNode] = []
        if isinstance(raw_nodes, Mapping):
            for default_kind, group in raw_nodes.items():
                if isinstance(group, list):
                    node_values.extend(WorkflowNode.from_dict(item, default_kind=str(default_kind)) for item in group)
                elif isinstance(group, Mapping):
                    item = dict(group)
                    if "node_id" not in item and "id" not in item:
                        item["node_id"] = default_kind
                    node_values.append(WorkflowNode.from_dict(item, default_kind=str(default_kind)))
                else:
                    raise WorkflowValidationError("workflow.nodes mapping values must be objects or arrays")
        elif isinstance(raw_nodes, list):
            node_values = [WorkflowNode.from_dict(item) for item in raw_nodes]
        else:
            raise WorkflowValidationError("workflow.nodes must be an array or object")

        raw_edges = value.get("edges", value.get("relationships", []))
        if not isinstance(raw_edges, list):
            raise WorkflowValidationError("workflow.edges must be an array")
        raw_events = value.get("events", value.get("history", []))
        if not isinstance(raw_events, list):
            raise WorkflowValidationError("workflow.events must be an array")

        schema_version = value.get("schema_version", value.get("record_version", value.get("version", WORKFLOW_SCHEMA_VERSION)))
        record = cls(
            workflow_id=value.get("workflow_id", value.get("id")),
            repo_path=value.get("repo_path", value.get("repository_path", value.get("repository"))),
            nodes=tuple(node_values),
            edges=tuple(WorkflowEdge.from_dict(item) for item in raw_edges),
            status=value.get("status", "draft"),
            automation_mode=value.get("automation_mode", "serial"),
            revision=value.get("revision", 0),
            created_at=value.get("created_at", utc_now()),
            updated_at=value.get("updated_at", utc_now()),
            events=tuple(LifecycleEvent.from_dict(item) for item in raw_events),
            metadata=value.get("metadata", {}),
            schema_version=schema_version,
        )
        record.validate()
        recorded_digest = value.get("record_digest")
        if recorded_digest is not None and recorded_digest != record.digest:
            raise WorkflowValidationError("workflow.record_digest does not match the canonical record")
        return record

    @classmethod
    def from_json(cls, text: str) -> "WorkflowRecord":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowValidationError("workflow record is not valid JSON") from exc
        return cls.from_dict(value)

    def to_json(self) -> str:
        """Serialize the record using the same stable shape as the store."""

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    from_mapping = from_dict


def validate_workflow_record(value: WorkflowRecord | Mapping[str, Any]) -> WorkflowRecord:
    """Validate either a model instance or a serialized workflow mapping."""

    record = value if isinstance(value, WorkflowRecord) else WorkflowRecord.from_dict(value)
    return record.validate()


def transition_workflow(
    record: WorkflowRecord,
    new_status: str,
    *,
    reason: str | None = None,
    now: str | None = None,
) -> WorkflowRecord:
    return record.transition(new_status, reason=reason, now=now)


def transition_node(
    record: WorkflowRecord,
    node_id: str,
    new_status: str,
    *,
    reason: str | None = None,
    now: str | None = None,
) -> WorkflowRecord:
    return record.transition_node(node_id, new_status, reason=reason, now=now)


def select_frontier(
    record: WorkflowRecord | Mapping[str, Any],
    *,
    automation_mode: str | AutomationMode | None = None,
) -> tuple[WorkflowNode, ...]:
    """Lazy workflow-module entry point for serial frontier selection."""

    from .scheduler import select_frontier as _select_frontier

    return _select_frontier(record, automation_mode=automation_mode)


def propagate_stale(
    record: WorkflowRecord | Mapping[str, Any],
    *,
    now: str | None = None,
    reason: str = "stale source artifact requires reconciliation",
) -> WorkflowRecord:
    """Lazy workflow-module entry point for stale dependency propagation."""

    from .scheduler import propagate_stale as _propagate_stale

    return _propagate_stale(record, now=now, reason=reason)


class WorkflowStore:
    """Atomic local JSON persistence for canonical workflow records."""

    def __init__(self, root: Path | None = None, *, directory: Path | None = None):
        self.root = Path(root or default_data_dir())
        self.workflows_dir = Path(directory) if directory is not None else self.root / "workflows"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)

    def workflow_path(self, workflow_id: str) -> Path:
        _require_text(workflow_id, "workflow_id")
        if Path(workflow_id).name != workflow_id or workflow_id.endswith("."):
            raise WorkflowStoreError(f"Unsafe workflow ID: {workflow_id}")
        return self.workflows_dir / f"{workflow_id}.json"

    record_path = workflow_path

    def create(self, record: WorkflowRecord) -> WorkflowRecord:
        record = validate_workflow_record(record)
        path = self.workflow_path(record.workflow_id)
        if path.exists():
            raise WorkflowStoreError(f"Workflow already exists: {record.workflow_id}")
        atomic_write_json(path, record.to_dict())
        return record

    def save(self, record: WorkflowRecord, *, expected_revision: int | None = None) -> WorkflowRecord:
        record = validate_workflow_record(record)
        path = self.workflow_path(record.workflow_id)
        current: WorkflowRecord | None = None
        if path.exists():
            current = self.get(record.workflow_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise WorkflowConflictError(
                    f"Workflow {record.workflow_id} changed from revision {expected_revision} to {current.revision}"
                )
            if record.revision < current.revision:
                raise WorkflowConflictError(
                    f"Workflow {record.workflow_id} revision {record.revision} is older than {current.revision}"
                )
        elif expected_revision is not None:
            raise WorkflowConflictError(f"Workflow does not exist: {record.workflow_id}")
        atomic_write_json(path, record.to_dict())
        return record

    write = save

    def get(self, workflow_id: str) -> WorkflowRecord:
        path = self.workflow_path(workflow_id)
        if not path.is_file():
            raise WorkflowStoreError(f"Unknown workflow: {workflow_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return WorkflowRecord.from_dict(raw)
        except (OSError, json.JSONDecodeError, WorkflowValidationError) as exc:
            raise WorkflowStoreError(f"Corrupt workflow record: {path}") from exc

    load = get
    read = get

    def list_all(self) -> list[WorkflowRecord]:
        records: list[WorkflowRecord] = []
        for path in sorted(self.workflows_dir.glob("*.json")):
            try:
                records.append(WorkflowRecord.from_json(path.read_text(encoding="utf-8")))
            except (OSError, WorkflowValidationError, json.JSONDecodeError) as exc:
                raise WorkflowStoreError(f"Corrupt workflow record: {path}") from exc
        return records

    list_records = list_all

    def transition(
        self,
        workflow_id: str,
        new_status: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> WorkflowRecord:
        current = self.get(workflow_id)
        updated = current.transition(new_status, reason=reason, now=now)
        return self.save(updated, expected_revision=current.revision)

    def transition_node(
        self,
        workflow_id: str,
        node_id: str,
        new_status: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> WorkflowRecord:
        current = self.get(workflow_id)
        updated = current.transition_node(node_id, new_status, reason=reason, now=now)
        return self.save(updated, expected_revision=current.revision)

    create_record = create
    save_record = save
    get_record = get
    list_workflows = list_all


WorkflowRecordStore = WorkflowStore
WorkflowPersistenceError = WorkflowStoreError
