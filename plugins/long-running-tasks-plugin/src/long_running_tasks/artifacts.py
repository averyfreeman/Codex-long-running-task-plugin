"""Read-only adapters for the local Markdown artifacts used by Matt tools.

The separate git-bbq-matt-skills repository defines a small local convention:
triage and specs live below .scratch/<feature>/ and tickets live below
.scratch/<feature>/issues/.  This module translates that convention into the
validated workflow model without importing the source repository, invoking
Git, or writing to the checkout.

The adapter keeps both the source label and the normalized workflow status.
That makes the workflow useful to a scheduler while retaining enough
information to reconcile a source artifact when its local convention changes.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .state import utc_now
from .workflow import Provenance, WorkflowEdge, WorkflowNode, WorkflowRecord


MATT_ARTIFACT_KINDS = frozenset({"triage", "spec", "ticket"})

# The source repository uses labels that are useful to humans. Workflow
# statuses are intentionally a smaller, stable vocabulary.
MATT_STATUS_MAP = {
    "needs-triage": "needs-triage",
    "needs_info": "needs-more-information",
    "needs-info": "needs-more-information",
    "needs-information": "needs-more-information",
    "needs-more-information": "needs-more-information",
    "ready-for-agent": "ready",
    "ready-for-afk": "ready",
    "ready": "ready",
    "ready-for-human": "needs-human",
    "needs-human": "needs-human",
    "wontfix": "not-doing",
    "wont-fix": "not-doing",
    "not-doing": "not-doing",
    "in-progress": "running",
    "running": "running",
    "done": "completed",
    "completed": "completed",
    "open": "proposed",
    "proposed": "proposed",
    "untriaged": "needs-triage",
    "draft": "draft",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "stale": "stale",
    "rejected": "rejected",
    "superseded": "superseded",
    "approved": "approved",
    "current": "current",
}

DEFAULT_ARTIFACT_STATUSES = {
    "triage": "needs-triage",
    "spec": "current",
    "ticket": "draft",
}

_APPROVED_SOURCE_STATUSES = frozenset(
    {"approved", "current", "ready", "ready-for-agent", "ready-for-afk"}
)
_NOT_CURRENT_SOURCE_STATUSES = frozenset(
    {"stale", "superseded", "rejected", "wontfix", "wont-fix", "not-doing"}
)
_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?([A-Za-z][A-Za-z0-9 _-]*?)(?:\*\*)?:\s*(?:\*\*)?\s*(.*?)\s*$"
)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_TICKET_NUMBER_RE = re.compile(r"^(?P<number>\d+)(?:[-_.](?P<slug>.+))?$")
_SAFE_ID_RE = re.compile(r"[^a-z0-9]+")


class ArtifactAdapterError(RuntimeError):
    """Base error for local artifact adaptation failures."""


class ArtifactValidationError(ArtifactAdapterError, ValueError):
    """Raised when a local artifact cannot become a valid workflow node."""


class ArtifactReferenceError(ArtifactValidationError):
    """Raised when a blocker reference cannot be resolved unambiguously."""


def _clean_value(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _normalise_key(value: object) -> str:
    return re.sub(r"[-\s]+", "_", str(value).strip().lower())


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [
                item.strip().strip("'\"")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
    return value


def _front_matter(text: str) -> dict[str, Any]:
    """Parse the scalar subset of YAML used by local Matt documents."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next(
        (
            index
            for index in range(1, len(lines))
            if lines[index].strip() in {"---", "..."}
        ),
        None,
    )
    if end is None:
        return {}
    result: dict[str, Any] = {}
    list_key: str | None = None
    for line in lines[1:end]:
        list_item = re.match(r"^\s*-\s+(.*?)\s*$", line)
        if list_item and list_key is not None:
            current = result.setdefault(list_key, [])
            if isinstance(current, list):
                current.append(_parse_scalar(list_item.group(1)))
            continue
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9 _-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            list_key = _normalise_key(match.group(1))
            parsed = _parse_scalar(match.group(2))
            result[list_key] = [] if parsed == "" else parsed
        elif line.strip():
            list_key = None
    return result


def _label_values(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line)
        if match:
            result[_normalise_key(match.group(1))] = match.group(2).strip()
    return result


def _field_value(
    text: str,
    front_matter: Mapping[str, Any],
    *keys: str,
) -> Any:
    labels = _label_values(text)
    sections = _sections(text)
    for key in keys:
        normalized = _normalise_key(key)
        if normalized in front_matter:
            return front_matter[normalized]
        if normalized in labels:
            return labels[normalized]
        if normalized in sections and sections[normalized]:
            return sections[normalized]
    return None


def _parse_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    raise ArtifactValidationError(f"{field_name} must be boolean")


def _canonical_status(raw_status: object, *, default: str) -> tuple[str, str]:
    raw = _clean_value(raw_status) or default
    normalized = raw.lower().replace("_", "-").strip()
    status = MATT_STATUS_MAP.get(normalized, normalized)
    return raw, status


def _title_from(text: str, path: Path, front_matter: Mapping[str, Any]) -> str:
    explicit = _clean_value(_field_value(text, front_matter, "title", "name"))
    if explicit:
        title = explicit
    else:
        title = ""
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match and len(match.group(1)) == 1:
                title = match.group(2).strip()
                break
        title = title or path.stem.replace("-", " ").replace("_", " ").strip()
    # Matt ticket headings commonly begin with "01:". The number remains in
    # the node ID, but it is noise in the human title.
    return re.sub(r"^\s*\d+\s*[:.)-]\s*", "", title).strip() or path.stem


def _sections(text: str) -> dict[str, str]:
    lines = text.splitlines(keepends=True)
    sections: dict[str, str] = {}
    current: str | None = None
    content: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            if current is not None:
                sections[current] = "".join(content).strip()
            current = _normalise_key(match.group(2))
            content = []
        elif current is not None:
            content.append(line)
    if current is not None:
        sections[current] = "".join(content).strip()
    return sections


def _blocker_refs(text: str, front_matter: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _field_value(
        text,
        front_matter,
        "blocked_by",
        "blocked by",
        "depends_on",
        "depends on",
    )
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    else:
        values = re.split(r",|;|\band\b|\r?\n", str(raw), flags=re.IGNORECASE)
    result: list[str] = []
    for value in values:
        if _FIELD_RE.match(value):
            continue
        cleaned = re.sub(r"^\s*[-*]\s*", "", value).strip().strip("`")
        link = re.fullmatch(r"\[([^\]]+)\]\([^)]*\)", cleaned)
        if link:
            cleaned = link.group(1).strip()
        cleaned = cleaned.lstrip("#").strip()
        lowered = cleaned.lower()
        if (
            not cleaned
            or lowered in {"none", "n/a", "nil", "-", "unblocked"}
            or lowered.startswith("none (")
            or lowered.startswith("no blockers")
        ):
            continue
        if cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def _slug(value: str) -> str:
    result = _SAFE_ID_RE.sub("-", value.lower()).strip("-")
    return result or "artifact"


def _relative_source_ref(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArtifactValidationError(
            f"Artifact is outside the local repository root: {path}"
        ) from exc


def _feature_name(relative_path: str, root: Path) -> str:
    parts = Path(relative_path).parts
    if root.name == ".scratch" and parts:
        return parts[0]
    if ".scratch" in parts:
        index = parts.index(".scratch")
        if index + 1 < len(parts):
            return parts[index + 1]
    if len(parts) >= 2:
        return parts[-2]
    return root.name


def _default_node_id(kind: str, relative_path: str, root: Path) -> str:
    path = Path(relative_path)
    feature = _slug(_feature_name(relative_path, root))
    stem = path.stem
    if kind == "ticket":
        match = _TICKET_NUMBER_RE.match(stem)
        ticket_part = _slug(stem)
        if match and match.group("number"):
            ticket_part = f"{match.group('number')}-{_slug(match.group('slug') or 'ticket')}"
        return f"ticket-{feature}-{ticket_part}"
    return f"{kind}-{feature}"


def _ticket_number(path: Path) -> str | None:
    match = _TICKET_NUMBER_RE.match(path.stem)
    return match.group("number") if match else None


def _infer_root(path: Path) -> Path:
    resolved = path.resolve()
    parts = resolved.parts
    if ".scratch" in parts:
        index = parts.index(".scratch")
        prefix = parts[:index]
        if prefix:
            return Path(*prefix)
    return resolved.parent


@dataclass(frozen=True)
class LocalMattArtifact:
    """One source Markdown file and its validated workflow projection."""

    path: Path
    text: str
    node: WorkflowNode
    title: str
    source_status: str
    blocker_refs: tuple[str, ...] = ()
    sections: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "blocker_refs", tuple(self.blocker_refs))
        object.__setattr__(self, "sections", dict(self.sections))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def kind(self) -> str:
        return self.node.kind

    @property
    def node_id(self) -> str:
        return self.node.node_id

    @property
    def provenance(self) -> Provenance:
        return self.node.provenance

    @property
    def source_ref(self) -> str:
        return self.node.provenance.source_ref

    @property
    def content_digest(self) -> str:
        return self.node.provenance.digest

    @property
    def is_current(self) -> bool:
        return self.node.is_current

    @property
    def is_approved(self) -> bool:
        return self.node.is_approved

    def section(self, name: str, default: str | None = None) -> str | None:
        return self.sections.get(_normalise_key(name), default)

    def to_node(self) -> WorkflowNode:
        return self.node

    def render_snapshot(self):
        """Render this artifact through the Markdown snapshot implementation."""

        from .snapshots import MarkdownSnapshotRenderer

        return MarkdownSnapshotRenderer().render(self)


class LocalMattArtifactAdapter:
    """Base class for a read-only adapter for one Matt artifact kind."""

    kind: str
    default_status: str

    def __init__(
        self,
        *,
        kind: str | None = None,
        default_status: str | None = None,
    ) -> None:
        selected_kind = kind or getattr(self, "kind", None)
        if selected_kind not in MATT_ARTIFACT_KINDS:
            raise ArtifactValidationError(
                f"Unsupported local Matt artifact kind: {selected_kind}"
            )
        self.kind = selected_kind
        self.default_status = default_status or DEFAULT_ARTIFACT_STATUSES[selected_kind]

    def _matches(self, path: Path, root: Path) -> bool:
        if path.suffix.lower() != ".md":
            return False
        relative = Path(_relative_source_ref(path.resolve(), root))
        if ".scratch" not in root.parts and ".scratch" not in relative.parts:
            return False
        if self.kind == "triage":
            return (
                path.name.lower() == "triage.md"
                or path.parent.name.lower() == "triage"
                or (
                    path.parent.name.lower() == "issues"
                    and (
                        path.stem.lower().startswith("triage-")
                        or path.stem.lower().endswith("-triage")
                    )
                )
            )
        if self.kind == "spec":
            return path.name.lower() == "spec.md" or path.parent.name.lower() == "spec"
        return path.parent.name.lower() == "issues" and not (
            path.stem.lower().startswith("triage-")
            or path.stem.lower().endswith("-triage")
        )

    def discover(self, root: str | Path) -> tuple[LocalMattArtifact, ...]:
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise ArtifactValidationError(f"Artifact root is not a directory: {root_path}")
        paths = sorted(
            path
            for path in root_path.rglob("*.md")
            if path.is_file() and self._matches(path, root_path)
        )
        return tuple(self.adapt(path, root=root_path) for path in paths)

    def adapt(
        self,
        path: str | Path,
        *,
        root: str | Path | None = None,
        node_id: str | None = None,
        status: str | None = None,
        current: bool | None = None,
        approved: bool | None = None,
        source_revision: str | None = None,
        captured_at: str | None = None,
    ) -> LocalMattArtifact:
        source_path = Path(path).resolve()
        if not source_path.is_file():
            raise ArtifactValidationError(f"Local Matt artifact is not a file: {source_path}")
        root_path = Path(root).resolve() if root is not None else _infer_root(source_path)
        if not root_path.is_dir():
            raise ArtifactValidationError(f"Artifact root is not a directory: {root_path}")
        source_ref = _relative_source_ref(source_path, root_path)
        try:
            text = source_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactValidationError(f"Artifact is not valid UTF-8: {source_path}") from exc

        front = _front_matter(text)
        labels = _label_values(text)
        raw_status = status
        if raw_status is None:
            raw_status = _field_value(text, front, "status", "workflow status", "state")
        raw_status_value, canonical_status = _canonical_status(
            raw_status,
            default=self.default_status,
        )
        title = _title_from(text, source_path, front)

        explicit_id = node_id or _clean_value(
            _field_value(
                text,
                front,
                "node_id",
                "workflow node",
                "workflow_node",
                "ticket_id",
                "issue_id",
                "artifact_id",
                "id",
            )
        )
        selected_node_id = (explicit_id or _default_node_id(self.kind, source_ref, root_path))
        selected_node_id = selected_node_id.lstrip("#").strip()
        if not selected_node_id:
            raise ArtifactValidationError(f"Artifact has an empty workflow node ID: {source_path}")

        current_value = current
        if current_value is None:
            raw_current = _field_value(text, front, "current", "is_current")
            current_value = (
                _parse_bool(raw_current, field_name="current")
                if raw_current is not None
                else raw_status_value.lower() not in _NOT_CURRENT_SOURCE_STATUSES
            )
        approved_value = approved
        if approved_value is None:
            raw_approved = _field_value(text, front, "approved", "is_approved")
            approved_value = (
                _parse_bool(raw_approved, field_name="approved")
                if raw_approved is not None
                else raw_status_value.lower() in _APPROVED_SOURCE_STATUSES
            )

        revision_value = source_revision
        if revision_value is None:
            raw_revision = _field_value(
                text,
                front,
                "source_revision",
                "source revision",
                "revision",
            )
            revision_value = _clean_value(raw_revision)
        feature = _feature_name(source_ref, root_path)
        blockers = _blocker_refs(text, front)
        timestamp = captured_at or utc_now()
        provenance = Provenance.from_file(
            source_path,
            source_ref=source_ref,
            captured_at=timestamp,
            source_revision=revision_value,
            metadata={
                "adapter": self.__class__.__name__,
                "artifact_kind": self.kind,
                "feature": feature,
            },
        )
        metadata: dict[str, Any] = {
            "adapter": self.__class__.__name__,
            "artifact_kind": self.kind,
            "feature": feature,
            "source_status": raw_status_value,
            "source_ref": source_ref,
            "blocked_by": list(blockers),
        }
        if front:
            metadata["front_matter"] = copy.deepcopy(front)
        number = _ticket_number(source_path) if self.kind == "ticket" else None
        if number:
            metadata["ticket_number"] = number
        for key in ("labels", "owner", "priority"):
            if key in front:
                metadata[key] = copy.deepcopy(front[key])
            elif key in labels:
                metadata[key] = labels[key]
        node = WorkflowNode(
            node_id=selected_node_id,
            kind=self.kind,
            status=canonical_status,
            title=title,
            provenance=provenance,
            artifact=source_ref,
            current=bool(current_value),
            approved=bool(approved_value),
            created_at=timestamp,
            updated_at=timestamp,
            metadata=metadata,
        )
        try:
            node.validate()
        except ValueError as exc:
            raise ArtifactValidationError(
                f"Artifact {source_path} produced an invalid {self.kind} workflow node: {exc}"
            ) from exc
        return LocalMattArtifact(
            path=source_path,
            text=text,
            node=node,
            title=title,
            source_status=raw_status_value,
            blocker_refs=blockers,
            sections=_sections(text),
            metadata=metadata,
        )


class TriageArtifactAdapter(LocalMattArtifactAdapter):
    kind = "triage"
    default_status = DEFAULT_ARTIFACT_STATUSES["triage"]


class SpecArtifactAdapter(LocalMattArtifactAdapter):
    kind = "spec"
    default_status = DEFAULT_ARTIFACT_STATUSES["spec"]


class TicketArtifactAdapter(LocalMattArtifactAdapter):
    kind = "ticket"
    default_status = DEFAULT_ARTIFACT_STATUSES["ticket"]


TRIAGE_ADAPTER = TriageArtifactAdapter()
SPEC_ADAPTER = SpecArtifactAdapter()
TICKET_ADAPTER = TicketArtifactAdapter()


class LocalMattArtifactCatalog:
    """Discover local Matt artifacts and turn ticket blockers into edges."""

    def __init__(
        self,
        root: str | Path,
        *,
        adapters: Mapping[str, LocalMattArtifactAdapter] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ArtifactValidationError(f"Artifact root is not a directory: {self.root}")
        self.adapters = dict(
            adapters
            or {
                "triage": TRIAGE_ADAPTER,
                "spec": SPEC_ADAPTER,
                "ticket": TICKET_ADAPTER,
            }
        )
        if set(self.adapters) != set(MATT_ARTIFACT_KINDS):
            raise ArtifactValidationError(
                "Artifact catalog adapters must cover triage, spec, and ticket"
            )

    def adapter(self, kind: str) -> LocalMattArtifactAdapter:
        try:
            return self.adapters[kind]
        except KeyError as exc:
            raise ArtifactValidationError(
                f"Unsupported local Matt artifact kind: {kind}"
            ) from exc

    def discover(self, kind: str | None = None) -> tuple[LocalMattArtifact, ...]:
        if kind is not None:
            return self.adapter(kind).discover(self.root)
        artifacts: list[LocalMattArtifact] = []
        for selected_kind in ("triage", "spec", "ticket"):
            artifacts.extend(self.adapter(selected_kind).discover(self.root))
        return tuple(
            sorted(
                artifacts,
                key=lambda item: (item.source_ref, item.kind, item.node_id),
            )
        )

    def nodes(self, kind: str | None = None) -> tuple[WorkflowNode, ...]:
        return tuple(artifact.node for artifact in self.discover(kind))

    @staticmethod
    def _resolve_blocker(
        reference: str,
        target: LocalMattArtifact,
        artifacts: tuple[LocalMattArtifact, ...],
    ) -> LocalMattArtifact:
        candidates = list(artifacts)
        exact = [item for item in candidates if item.node_id == reference]
        if len(exact) == 1:
            return exact[0]
        target_feature = target.node.metadata.get("feature")
        if target_feature:
            feature_matches = [
                item
                for item in candidates
                if item.node.metadata.get("feature") == target_feature
            ]
            if feature_matches:
                candidates = feature_matches
        reference_lower = reference.lower()
        reference_slug = _slug(reference)
        number_match = re.match(r"^\s*(\d+)\b", reference)
        reference_number = number_match.group(1) if number_match else None
        matches: list[LocalMattArtifact] = []
        for item in candidates:
            names = {
                item.node_id.lower(),
                item.path.stem.lower(),
                item.path.name.lower(),
                item.source_ref.lower(),
                _slug(item.path.stem),
                _slug(item.title),
            }
            ticket_number = item.node.metadata.get("ticket_number")
            if ticket_number:
                names.add(str(ticket_number).lower())
            if (
                reference_lower in names
                or reference_slug in names
                or reference_number is not None
                and str(ticket_number).lower() == reference_number
            ):
                matches.append(item)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ArtifactReferenceError(
                f"Blocker reference {reference!r} in {target.source_ref} "
                "does not match a local artifact"
            )
        raise ArtifactReferenceError(
            f"Blocker reference {reference!r} in {target.source_ref} is ambiguous"
        )

    def edges(
        self,
        artifacts: Iterable[LocalMattArtifact] | None = None,
    ) -> tuple[WorkflowEdge, ...]:
        selected = tuple(artifacts) if artifacts is not None else self.discover()
        by_id: dict[str, LocalMattArtifact] = {}
        for artifact in selected:
            if artifact.node_id in by_id and by_id[artifact.node_id].path != artifact.path:
                raise ArtifactValidationError(
                    f"Duplicate local artifact node ID: {artifact.node_id}"
                )
            by_id[artifact.node_id] = artifact
        edges: list[WorkflowEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()
        for target in selected:
            if target.kind != "ticket":
                continue
            for reference in target.blocker_refs:
                blocker = self._resolve_blocker(reference, target, selected)
                edge = WorkflowEdge(
                    source=blocker.node_id,
                    target=target.node_id,
                    kind="blocks",
                    metadata={
                        "source_reference": reference,
                        "source_artifact": target.source_ref,
                    },
                )
                try:
                    edge.validate()
                except ValueError as exc:
                    raise ArtifactReferenceError(
                        f"Invalid blocker reference {reference!r} in {target.source_ref}: {exc}"
                    )
                key = (edge.source, edge.target, edge.kind)
                if key not in edge_keys:
                    edges.append(edge)
                    edge_keys.add(key)
        return tuple(
            sorted(edges, key=lambda edge: (edge.target, edge.source, edge.kind))
        )

    def workflow_parts(self) -> tuple[tuple[WorkflowNode, ...], tuple[WorkflowEdge, ...]]:
        artifacts = self.discover()
        return tuple(item.node for item in artifacts), self.edges(artifacts)

    def as_workflow_record(
        self,
        workflow_id: str,
        repo_path: str | Path | None = None,
        *,
        status: str = "draft",
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkflowRecord:
        nodes, edges = self.workflow_parts()
        return WorkflowRecord.create(
            workflow_id,
            str(repo_path or self.root),
            nodes=nodes,
            edges=edges,
            status=status,
            automation_mode="serial",
            metadata=metadata,
            now=now,
        )


def adapt_local_artifact(
    path: str | Path,
    *,
    kind: str | None = None,
    root: str | Path | None = None,
    **kwargs: Any,
) -> LocalMattArtifact:
    """Adapt one local Markdown file, inferring its kind when omitted."""

    source_path = Path(path)
    selected_kind = kind
    if selected_kind is None:
        lowered = source_path.name.lower()
        if lowered == "triage.md" or "triage" in source_path.stem.lower():
            selected_kind = "triage"
        elif lowered == "spec.md" or source_path.parent.name.lower() == "spec":
            selected_kind = "spec"
        elif source_path.parent.name.lower() == "issues":
            selected_kind = "ticket"
    if selected_kind not in MATT_ARTIFACT_KINDS:
        raise ArtifactValidationError(
            f"Cannot infer a local Matt artifact kind for {source_path}"
        )
    adapters = {
        "triage": TRIAGE_ADAPTER,
        "spec": SPEC_ADAPTER,
        "ticket": TICKET_ADAPTER,
    }
    return adapters[selected_kind].adapt(source_path, root=root, **kwargs)


def load_triage_artifact(path: str | Path, **kwargs: Any) -> LocalMattArtifact:
    return TRIAGE_ADAPTER.adapt(path, **kwargs)


def load_spec_artifact(path: str | Path, **kwargs: Any) -> LocalMattArtifact:
    return SPEC_ADAPTER.adapt(path, **kwargs)


def load_ticket_artifact(path: str | Path, **kwargs: Any) -> LocalMattArtifact:
    return TICKET_ADAPTER.adapt(path, **kwargs)


# Friendly aliases for callers that use the source repository's terminology.
LocalMattTriageAdapter = TriageArtifactAdapter
LocalMattSpecAdapter = SpecArtifactAdapter
LocalMattTicketAdapter = TicketArtifactAdapter
MattArtifactAdapter = LocalMattArtifactAdapter
ArtifactAdapter = LocalMattArtifactAdapter
MattArtifact = LocalMattArtifact
MattArtifactCatalog = LocalMattArtifactCatalog
ArtifactCatalog = LocalMattArtifactCatalog
TriageAdapter = TriageArtifactAdapter
SpecAdapter = SpecArtifactAdapter
TicketAdapter = TicketArtifactAdapter
MattTriageArtifactAdapter = TriageArtifactAdapter
MattSpecArtifactAdapter = SpecArtifactAdapter
MattTicketArtifactAdapter = TicketArtifactAdapter


__all__ = [
    "ArtifactAdapterError",
    "ArtifactAdapter",
    "ArtifactCatalog",
    "ArtifactReferenceError",
    "ArtifactValidationError",
    "DEFAULT_ARTIFACT_STATUSES",
    "LocalMattArtifact",
    "LocalMattArtifactAdapter",
    "LocalMattArtifactCatalog",
    "LocalMattSpecAdapter",
    "LocalMattTicketAdapter",
    "LocalMattTriageAdapter",
    "MATT_ARTIFACT_KINDS",
    "MATT_STATUS_MAP",
    "MattArtifact",
    "MattArtifactAdapter",
    "MattArtifactCatalog",
    "MattSpecArtifactAdapter",
    "MattTicketArtifactAdapter",
    "MattTriageArtifactAdapter",
    "SPEC_ADAPTER",
    "SpecAdapter",
    "SpecArtifactAdapter",
    "TICKET_ADAPTER",
    "TicketAdapter",
    "TicketArtifactAdapter",
    "TRIAGE_ADAPTER",
    "TriageAdapter",
    "TriageArtifactAdapter",
    "adapt_local_artifact",
    "load_spec_artifact",
    "load_ticket_artifact",
    "load_triage_artifact",
]
