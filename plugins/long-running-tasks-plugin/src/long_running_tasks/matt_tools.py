"""Install the pinned, local Matt-tools skill bundle.

The long-running task runner owns this small installer because the workflow
needs a stable set of source skills without taking ownership of the separate
``git-bbq-matt-skills`` repository.  The default mode is deliberately
``bundled``: it reads only package data and is safe to use without a network.

The two opt-in source modes make provenance explicit:

``sibling-checkout``
    Read a caller-provided local checkout.  Its ``HEAD`` and every selected
    file must match the pinned manifest.

``explicit-fetch``
    Clone and check out the pinned source commit in a temporary directory.
    This is the only mode that can use the network.

Installation copies the three selected skill directories into flat skill
directories (``<destination>/to-spec``, ``to-tickets``, and ``triage``) by
default.  A JSON record at ``.matt-tools-three.install.json`` records the
source repository, source commit, selected paths, and content digests.  A
second install is a no-op when the managed files and record are unchanged.
Existing conflicting files are never overwritten implicitly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

from .state import utc_now
from .workflow import canonical_json, digest_bytes, digest_json


MATT_TOOLS_BUNDLE_ID = "matt-tools-three"
MATT_TOOLS_MANIFEST_VERSION = 1
MATT_TOOLS_SOURCE_REPOSITORY = "https://github.com/averyfreeman/git-bbq-matt-skills.git"
MATT_TOOLS_SOURCE_COMMIT = "64fb7a440ff4a5e0b3d82680b2d73c2b93e1f2fa"
MATT_TOOLS_SOURCE_PATHS = (
    "skills/engineering/to-spec",
    "skills/engineering/to-tickets",
    "skills/engineering/triage",
)
MATT_TOOLS_COMPANION_PATHS = ("skills/engineering/setup-matt-pocock-skills",)
MATT_TOOLS_INSTALL_RECORD = ".matt-tools-three.install.json"
MATT_TOOLS_MODES = frozenset({"bundled", "explicit-fetch", "sibling-checkout"})
MATT_TOOLS_LAYOUTS = frozenset({"flat", "source"})

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_ROOT = Path(__file__).resolve().parent
_BUNDLE_ROOT = _PACKAGE_ROOT / "bundles" / MATT_TOOLS_BUNDLE_ID
_MANIFEST_PATH = _BUNDLE_ROOT / "manifest.json"


class MattToolsError(RuntimeError):
    """Base class for pinned Matt-tools installation failures."""


class ManifestError(MattToolsError, ValueError):
    """Raised when the pinned bundle manifest is malformed or inconsistent."""


class UnsupportedInstallModeError(MattToolsError, ValueError):
    """Raised when an installer mode or layout is not supported."""


class SourceVerificationError(MattToolsError):
    """Raised when a source checkout cannot prove the pinned provenance."""


class ChecksumVerificationError(MattToolsError):
    """Raised when a selected source or bundled file has the wrong digest."""


class InstallationConflictError(MattToolsError):
    """Raised when installation would overwrite an unmanaged or changed file."""


class InstallationRecordError(MattToolsError):
    """Raised when a prior installation record is corrupt or incompatible."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field_name} must be a non-empty string")
    return value


def _validate_relative_path(value: str, field_name: str) -> str:
    _require_text(value, field_name)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ManifestError(f"{field_name} must be a safe relative POSIX path")
    if "\\" in value:
        raise ManifestError(f"{field_name} must use POSIX separators")
    return value


def _is_under(path: str, directory: str) -> bool:
    path_parts = PurePosixPath(path).parts
    directory_parts = PurePosixPath(directory).parts
    return len(path_parts) > len(directory_parts) and path_parts[: len(directory_parts)] == directory_parts


def _validate_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ManifestError(f"{field_name} must be a 64-character SHA-256 hex digest")
    return value


@dataclass(frozen=True)
class BundleFile:
    """One source file included in a bundle manifest."""

    source_path: str
    bundle_path: str
    sha256: str

    @property
    def digest(self) -> str:
        """Compatibility name for callers that call checksums digests."""

        return self.sha256

    def validate(self) -> "BundleFile":
        _validate_relative_path(self.source_path, "bundle file.source_path")
        _validate_relative_path(self.bundle_path, "bundle file.bundle_path")
        _validate_digest(self.sha256, f"bundle file {self.source_path}.sha256")
        return self

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "source_path": self.source_path,
            "bundle_path": self.bundle_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BundleFile":
        if not isinstance(value, Mapping):
            raise ManifestError("manifest files must contain JSON objects")
        result = cls(
            source_path=value.get("source_path", value.get("path")),
            bundle_path=value.get("bundle_path", value.get("path", value.get("source_path"))),
            sha256=value.get("sha256", value.get("digest")),
        )
        return result.validate()


@dataclass(frozen=True)
class BundleManifest:
    """Validated metadata for one pinned source-skill bundle."""

    bundle_id: str
    source_repository: str
    source_commit: str
    selected_paths: tuple[str, ...]
    files: tuple[BundleFile, ...]
    version: int = MATT_TOOLS_MANIFEST_VERSION
    companion_references: tuple[str, ...] = field(default_factory=tuple)
    installer_modes: tuple[str, ...] = tuple(sorted(MATT_TOOLS_MODES))
    destination_layout: str = "flat-skill-directories"
    bundle_digest: str | None = None
    manifest_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_paths", tuple(self.selected_paths))
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "companion_references", tuple(self.companion_references))
        object.__setattr__(self, "installer_modes", tuple(self.installer_modes))

    @property
    def source_ref(self) -> str:
        """The pinned repository plus commit, useful in install records."""

        return f"{self.source_repository}@{self.source_commit}"

    @property
    def content_digest(self) -> str:
        """The manifest's stable digest over its selected content identities."""

        return self.computed_bundle_digest

    @property
    def computed_bundle_digest(self) -> str:
        payload = {
            "bundle_id": self.bundle_id,
            "source_commit": self.source_commit,
            "files": [
                {"source_path": item.source_path, "sha256": item.sha256}
                for item in self.files
            ],
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def computed_manifest_digest(self) -> str:
        return hashlib.sha256(canonical_json(self._payload()).encode("utf-8")).hexdigest()

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.version,
            "bundle_id": self.bundle_id,
            "version": self.version,
            "source_repository": self.source_repository,
            "source_commit": self.source_commit,
            "selected_paths": list(self.selected_paths),
            "companion_references": list(self.companion_references),
            "files": [item.to_dict() for item in self.files],
            "installer_modes": list(self.installer_modes),
            "destination_layout": self.destination_layout,
        }

    def validate(self, *, require_matt_tools_three: bool = False) -> "BundleManifest":
        if not isinstance(self.version, int) or self.version != MATT_TOOLS_MANIFEST_VERSION:
            raise ManifestError(f"Unsupported bundle manifest version: {self.version}")
        _require_text(self.bundle_id, "manifest.bundle_id")
        _require_text(self.source_repository, "manifest.source_repository")
        if not _COMMIT_RE.fullmatch(self.source_commit):
            raise ManifestError("manifest.source_commit must be a full 40-character commit SHA")
        if not self.selected_paths:
            raise ManifestError("manifest.selected_paths must not be empty")
        for path in self.selected_paths:
            _validate_relative_path(path, "manifest.selected_paths entry")
        if len(set(self.selected_paths)) != len(self.selected_paths):
            raise ManifestError("manifest.selected_paths contains duplicates")
        for path in self.companion_references:
            _validate_relative_path(path, "manifest.companion_references entry")
        if set(self.installer_modes) != set(MATT_TOOLS_MODES):
            raise ManifestError("manifest.installer_modes must advertise all supported installer modes")
        if self.destination_layout != "flat-skill-directories":
            raise ManifestError("manifest.destination_layout must be flat-skill-directories")
        if not self.files:
            raise ManifestError("manifest.files must not be empty")
        file_paths: set[str] = set()
        bundle_paths: set[str] = set()
        for item in self.files:
            item.validate()
            if item.source_path in file_paths:
                raise ManifestError(f"Duplicate manifest source path: {item.source_path}")
            if item.bundle_path in bundle_paths:
                raise ManifestError(f"Duplicate manifest bundle path: {item.bundle_path}")
            if not any(_is_under(item.source_path, selected) for selected in self.selected_paths):
                raise ManifestError(f"Manifest file is outside selected paths: {item.source_path}")
            if not any(_is_under(item.bundle_path, selected) for selected in self.selected_paths):
                raise ManifestError(f"Manifest bundle file is outside selected paths: {item.bundle_path}")
            file_paths.add(item.source_path)
            bundle_paths.add(item.bundle_path)
        if self.bundle_digest is not None and self.bundle_digest != self.computed_bundle_digest:
            raise ManifestError("manifest.bundle_digest does not match the selected file identities")
        if self.manifest_digest is not None and self.manifest_digest != self.computed_manifest_digest:
            raise ManifestError("manifest.manifest_digest does not match the manifest payload")
        if require_matt_tools_three:
            if self.bundle_id != MATT_TOOLS_BUNDLE_ID:
                raise ManifestError(f"Expected bundle {MATT_TOOLS_BUNDLE_ID}, got {self.bundle_id}")
            if self.source_repository != MATT_TOOLS_SOURCE_REPOSITORY:
                raise ManifestError("matt-tools-three source repository is not the pinned repository")
            if self.source_commit != MATT_TOOLS_SOURCE_COMMIT:
                raise ManifestError("matt-tools-three source commit is not the pinned commit")
            if self.selected_paths != MATT_TOOLS_SOURCE_PATHS:
                raise ManifestError("matt-tools-three selected paths do not match the three-skill contract")
            if any(path in self.selected_paths for path in MATT_TOOLS_COMPANION_PATHS):
                raise ManifestError("matt-tools-three must not include the setup companion skill")
        return self

    def to_dict(self, *, include_digests: bool = True) -> dict[str, Any]:
        self.validate()
        payload = self._payload()
        if include_digests:
            payload["bundle_digest"] = self.computed_bundle_digest
            payload["manifest_digest"] = self.computed_manifest_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BundleManifest":
        if not isinstance(value, Mapping):
            raise ManifestError("bundle manifest must be a JSON object")
        files = value.get("files")
        if not isinstance(files, list):
            raise ManifestError("manifest.files must be an array")
        selected_paths = value.get("selected_paths", value.get("paths", ()))
        companion_references = value.get("companion_references", ())
        installer_modes = value.get("installer_modes", tuple(sorted(MATT_TOOLS_MODES)))
        for field_name, field_value in (
            ("selected_paths", selected_paths),
            ("companion_references", companion_references),
            ("installer_modes", installer_modes),
        ):
            if not isinstance(field_value, (list, tuple)):
                raise ManifestError(f"manifest.{field_name} must be an array")
        version = value.get("version", value.get("schema_version", MATT_TOOLS_MANIFEST_VERSION))
        result = cls(
            bundle_id=value.get("bundle_id", value.get("id")),
            source_repository=value.get("source_repository", value.get("repository")),
            source_commit=value.get("source_commit", value.get("commit")),
            selected_paths=tuple(selected_paths),
            files=tuple(BundleFile.from_dict(item) for item in files),
            version=version,
            companion_references=tuple(companion_references),
            installer_modes=tuple(installer_modes),
            destination_layout=value.get("destination_layout", "flat-skill-directories"),
            bundle_digest=value.get("bundle_digest"),
            manifest_digest=value.get("manifest_digest"),
        )
        return result.validate()

    @classmethod
    def from_json(cls, text: str) -> "BundleManifest":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError("bundle manifest is not valid JSON") from exc
        return cls.from_dict(value)

    @classmethod
    def load(cls, path: Path) -> "BundleManifest":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"Unable to read bundle manifest: {path}") from exc
        return cls.from_json(text)


@dataclass(frozen=True)
class InstalledFile:
    """A managed destination file returned by an install operation."""

    source_path: str
    destination: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_path": self.source_path,
            "destination": self.destination,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class InstallResult:
    """The inspectable result of one idempotent bundle installation."""

    bundle_id: str
    mode: str
    destination: Path
    record_path: Path
    source_repository: str
    source_commit: str
    selected_paths: tuple[str, ...]
    files: tuple[InstalledFile, ...]
    changed: bool
    already_installed: bool
    record_digest: str

    @property
    def installed(self) -> bool:
        return self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "mode": self.mode,
            "destination": str(self.destination),
            "record_path": str(self.record_path),
            "source_repository": self.source_repository,
            "source_commit": self.source_commit,
            "selected_paths": list(self.selected_paths),
            "files": [item.to_dict() for item in self.files],
            "changed": self.changed,
            "already_installed": self.already_installed,
            "record_digest": self.record_digest,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow the result to be consumed like the runner's JSON summaries."""

        return self.to_dict()[key]


def _manifest_path() -> Path:
    return _MANIFEST_PATH


def _bundle_root() -> Path:
    return _BUNDLE_ROOT


def load_matt_tools_manifest(path: Path | None = None) -> BundleManifest:
    """Load the pinned manifest, requiring the exact three-skill contract."""

    manifest = BundleManifest.load(path or _manifest_path())
    return manifest.validate(require_matt_tools_three=True)


def verify_bundled_matt_tools(
    *,
    manifest: BundleManifest | None = None,
    bundle_root: Path | None = None,
) -> BundleManifest:
    """Verify every vendored bundle file and return its validated manifest.

    This function is intentionally filesystem-only.  It is the check used by
    the default installer mode and never calls Git or a network client.
    """

    selected_manifest = manifest or load_matt_tools_manifest()
    selected_manifest.validate(require_matt_tools_three=manifest is None)
    root = Path(bundle_root) if bundle_root is not None else _bundle_root()
    _read_and_verify_files(selected_manifest, root, use_bundle_paths=True)
    return selected_manifest


def _read_json(path: Path, error_type: type[MattToolsError], message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_type(message) from exc
    if not isinstance(value, dict):
        raise error_type(message)
    return value


def _safe_join(root: Path, relative: str, *, field_name: str) -> Path:
    _validate_relative_path(relative, field_name)
    root = root.resolve()
    candidate = root / Path(*PurePosixPath(relative).parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SourceVerificationError(f"{field_name} escapes its source root") from exc
    if candidate.is_symlink():
        raise SourceVerificationError(f"{field_name} must not be a symlink: {relative}")
    return candidate


def _read_and_verify_files(
    manifest: BundleManifest,
    root: Path,
    *,
    use_bundle_paths: bool,
) -> tuple[tuple[BundleFile, bytes], ...]:
    result: list[tuple[BundleFile, bytes]] = []
    for item in manifest.files:
        relative = item.bundle_path if use_bundle_paths else item.source_path
        path = _safe_join(root, relative, field_name=f"bundle file {relative}")
        if not path.is_file():
            raise SourceVerificationError(f"Missing selected bundle file: {path}")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SourceVerificationError(f"Unable to read selected bundle file: {path}") from exc
        actual = digest_bytes(content)
        if actual != item.sha256:
            raise ChecksumVerificationError(
                f"Checksum mismatch for {relative}: expected {item.sha256}, got {actual}"
            )
        result.append((item, content))
    return tuple(result)


def _git_output(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SourceVerificationError("git is required for source-backed installer modes") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise SourceVerificationError(f"git {' '.join(args)} failed{suffix}") from exc
    return completed.stdout.strip()


def _verify_checkout_commit(checkout: Path, expected_commit: str) -> None:
    actual = _git_output(["-C", str(checkout), "rev-parse", "--verify", "HEAD"])
    if actual.casefold() != expected_commit.casefold():
        raise SourceVerificationError(
            f"Source checkout is at {actual}, expected pinned commit {expected_commit}"
        )


def _normalise_mode(mode: str) -> str:
    aliases = {
        "bundle": "bundled",
        "bundled": "bundled",
        "fetch": "explicit-fetch",
        "explicit-fetch": "explicit-fetch",
        "sibling": "sibling-checkout",
        "sibling-checkout": "sibling-checkout",
    }
    try:
        return aliases[mode]
    except (KeyError, TypeError) as exc:
        raise UnsupportedInstallModeError(
            f"Unsupported Matt-tools install mode {mode!r}; choose bundled, explicit-fetch, or sibling-checkout"
        ) from exc


def _normalise_layout(layout: str) -> str:
    aliases = {"flat": "flat", "flat-skill-directories": "flat", "source": "source", "preserve": "source"}
    try:
        return aliases[layout]
    except (KeyError, TypeError) as exc:
        raise UnsupportedInstallModeError(
            f"Unsupported Matt-tools destination layout {layout!r}; choose flat or source"
        ) from exc


def _destination_relative_path(source_path: str, layout: str) -> PurePosixPath:
    if layout == "source":
        return PurePosixPath(source_path)
    parts = PurePosixPath(source_path).parts
    prefix = ("skills", "engineering")
    if len(parts) < 4 or parts[:2] != prefix:
        raise ManifestError(f"Cannot flatten source skill path: {source_path}")
    return PurePosixPath(parts[2], *parts[3:])


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _record_payload(
    manifest: BundleManifest,
    *,
    mode: str,
    layout: str,
    installed_files: tuple[InstalledFile, ...],
    installed_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "bundle_id": manifest.bundle_id,
        "mode": mode,
        "destination_layout": layout,
        "source_repository": manifest.source_repository,
        "source_commit": manifest.source_commit,
        "selected_paths": list(manifest.selected_paths),
        "companion_references": list(manifest.companion_references),
        "bundle_digest": manifest.computed_bundle_digest,
        "manifest_digest": manifest.computed_manifest_digest,
        "files": [item.to_dict() for item in installed_files],
        "installed_at": installed_at,
    }


def _with_record_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    result["record_digest"] = digest_json(result)
    return result


def _record_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "bundle_id": payload.get("bundle_id"),
        "destination_layout": payload.get("destination_layout"),
        "source_repository": payload.get("source_repository"),
        "source_commit": payload.get("source_commit"),
        "selected_paths": payload.get("selected_paths"),
        "companion_references": payload.get("companion_references"),
        "bundle_digest": payload.get("bundle_digest"),
        "manifest_digest": payload.get("manifest_digest"),
        "files": payload.get("files"),
    }


class MattToolsInstaller:
    """Install the pinned Matt-tools bundle through an explicit source mode."""

    def __init__(
        self,
        *,
        manifest: BundleManifest | None = None,
        bundle_root: Path | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.manifest = manifest or load_matt_tools_manifest()
        self.manifest.validate(require_matt_tools_three=manifest is None)
        self.bundle_root = Path(bundle_root) if bundle_root is not None else _bundle_root()
        self.clock = clock

    @contextmanager
    def _source(
        self,
        mode: str,
        *,
        source_checkout: Path | None,
        source_repository: str | None,
    ) -> Iterator[tuple[Path, bool]]:
        if mode == "bundled":
            if source_checkout is not None:
                raise UnsupportedInstallModeError("bundled mode cannot use source_checkout")
            yield self.bundle_root, True
            return

        if mode == "sibling-checkout":
            if source_checkout is None:
                raise SourceVerificationError("sibling-checkout mode requires source_checkout")
            checkout = Path(source_checkout).expanduser().resolve()
            if not checkout.is_dir():
                raise SourceVerificationError(f"Source checkout does not exist: {checkout}")
            _verify_checkout_commit(checkout, self.manifest.source_commit)
            yield checkout, False
            return

        repository = source_repository or self.manifest.source_repository
        _require_text(repository, "source_repository")
        with tempfile.TemporaryDirectory(prefix=f"{self.manifest.bundle_id}-fetch-") as temporary:
            checkout = Path(temporary) / "checkout"
            _git_output(["clone", "--no-checkout", "--no-tags", repository, str(checkout)])
            _git_output(["fetch", "--no-tags", "origin", self.manifest.source_commit], cwd=checkout)
            _git_output(["checkout", "--detach", self.manifest.source_commit], cwd=checkout)
            _verify_checkout_commit(checkout, self.manifest.source_commit)
            yield checkout, False

    def install(
        self,
        destination: Path,
        *,
        mode: str = "bundled",
        source_checkout: Path | None = None,
        source_repository: str | None = None,
        layout: str = "flat",
        destination_layout: str | None = None,
    ) -> InstallResult:
        """Install into ``destination`` and return an inspectable result.

        ``destination_layout="source"`` is an escape hatch for callers that
        need the original ``skills/engineering/...`` tree.  The normal
        ``flat`` layout mirrors the per-skill directories used by Codex and
        other Agent Skills-compatible harnesses.
        """

        if destination_layout is not None:
            if layout != "flat" and _normalise_layout(layout) != _normalise_layout(destination_layout):
                raise UnsupportedInstallModeError("layout and destination_layout disagree")
            layout = destination_layout
        canonical_mode = _normalise_mode(mode)
        canonical_layout = _normalise_layout(layout)
        target = Path(destination).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)

        with self._source(
            canonical_mode,
            source_checkout=source_checkout,
            source_repository=source_repository,
        ) as (source_root, use_bundle_paths):
            verified = _read_and_verify_files(self.manifest, source_root, use_bundle_paths=use_bundle_paths)
            installed_files = tuple(
                InstalledFile(
                    source_path=item.source_path,
                    destination=str(_destination_relative_path(item.source_path, canonical_layout)),
                    sha256=item.sha256,
                )
                for item, _content in verified
            )
            return self._install_verified(
                target,
                canonical_mode,
                canonical_layout,
                verified,
                installed_files,
            )

    def _install_verified(
        self,
        target: Path,
        mode: str,
        layout: str,
        verified: tuple[tuple[BundleFile, bytes], ...],
        installed_files: tuple[InstalledFile, ...],
    ) -> InstallResult:
        record_path = target / MATT_TOOLS_INSTALL_RECORD
        expected_payload = _record_payload(
            self.manifest,
            mode=mode,
            layout=layout,
            installed_files=installed_files,
            installed_at="",
        )
        if record_path.exists() or record_path.is_symlink():
            if record_path.is_symlink() or not record_path.is_file():
                raise InstallationRecordError(f"Installation record is not a regular file: {record_path}")
            record = _read_json(record_path, InstallationRecordError, f"Corrupt installation record: {record_path}")
            record_digest = record.pop("record_digest", None)
            if not isinstance(record_digest, str) or digest_json(record) != record_digest:
                raise InstallationRecordError(f"Installation record digest is invalid: {record_path}")
            expected_identity = _record_identity(expected_payload)
            if _record_identity(record) != expected_identity:
                raise InstallationConflictError(
                    f"Installation record belongs to a different Matt-tools projection: {record_path}"
                )
            self._assert_destination_files(target, verified, layout)
            return self._result(
                target,
                record_path,
                mode,
                installed_files,
                changed=False,
                already_installed=True,
                record_digest=record_digest,
            )

        for item, content in verified:
            relative = _destination_relative_path(item.source_path, layout)
            destination = _safe_destination(target, relative)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise InstallationConflictError(f"Destination is not a regular file: {destination}")
                actual = digest_bytes(destination.read_bytes())
                if actual != item.sha256:
                    raise InstallationConflictError(
                        f"Refusing to overwrite existing file with different content: {destination}"
                    )

        changed = False
        for item, content in verified:
            destination = _safe_destination(target, _destination_relative_path(item.source_path, layout))
            if destination.exists():
                continue
            _atomic_write_bytes(destination, content)
            changed = True

        record = _with_record_digest(
            _record_payload(
                self.manifest,
                mode=mode,
                layout=layout,
                installed_files=installed_files,
                installed_at=self.clock(),
            )
        )
        _atomic_write_json(record_path, record)
        changed = True
        return self._result(
            target,
            record_path,
            mode,
            installed_files,
            changed=changed,
            already_installed=False,
            record_digest=record["record_digest"],
        )

    def _assert_destination_files(
        self,
        target: Path,
        verified: tuple[tuple[BundleFile, bytes], ...],
        layout: str,
    ) -> None:
        for item, _content in verified:
            destination = _safe_destination(target, _destination_relative_path(item.source_path, layout))
            if destination.is_symlink() or not destination.is_file():
                raise InstallationConflictError(f"Managed destination file is missing: {destination}")
            actual = digest_bytes(destination.read_bytes())
            if actual != item.sha256:
                raise InstallationConflictError(f"Managed destination file changed: {destination}")

    def _result(
        self,
        target: Path,
        record_path: Path,
        mode: str,
        installed_files: tuple[InstalledFile, ...],
        *,
        changed: bool,
        already_installed: bool,
        record_digest: str,
    ) -> InstallResult:
        return InstallResult(
            bundle_id=self.manifest.bundle_id,
            mode=mode,
            destination=target,
            record_path=record_path,
            source_repository=self.manifest.source_repository,
            source_commit=self.manifest.source_commit,
            selected_paths=self.manifest.selected_paths,
            files=installed_files,
            changed=changed,
            already_installed=already_installed,
            record_digest=record_digest,
        )


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise InstallationConflictError(f"Unsafe destination path: {relative}")
    candidate = root.joinpath(*relative.parts)
    resolved_root = root.resolve()
    resolved_parent = candidate.parent.resolve()
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise InstallationConflictError(f"Destination escapes install root: {relative}") from exc
    return candidate


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def install_matt_tools(
    destination: Path,
    *,
    mode: str = "bundled",
    source_checkout: Path | None = None,
    source_repository: str | None = None,
    layout: str = "flat",
    destination_layout: str | None = None,
    manifest: BundleManifest | None = None,
    bundle_root: Path | None = None,
) -> InstallResult:
    """Install the pinned Matt-tools bundle using the offline-capable default."""

    return MattToolsInstaller(manifest=manifest, bundle_root=bundle_root).install(
        destination,
        mode=mode,
        source_checkout=source_checkout,
        source_repository=source_repository,
        layout=layout,
        destination_layout=destination_layout,
    )


# Short aliases keep the public surface discoverable for callers that refer to
# the feature as a bundle installer rather than Matt-tools specifically.
BundleChecksumError = ChecksumVerificationError
InstallerError = MattToolsError
load_bundle_manifest = load_matt_tools_manifest
verify_bundle = verify_bundled_matt_tools
install_bundle = install_matt_tools


__all__ = [
    "BundleChecksumError",
    "BundleFile",
    "BundleManifest",
    "ChecksumVerificationError",
    "InstallationConflictError",
    "InstallationRecordError",
    "InstallResult",
    "InstallerError",
    "MATT_TOOLS_BUNDLE_ID",
    "MATT_TOOLS_COMPANION_PATHS",
    "MATT_TOOLS_INSTALL_RECORD",
    "MATT_TOOLS_MANIFEST_VERSION",
    "MATT_TOOLS_MODES",
    "MATT_TOOLS_SOURCE_COMMIT",
    "MATT_TOOLS_SOURCE_PATHS",
    "MATT_TOOLS_SOURCE_REPOSITORY",
    "ManifestError",
    "MattToolsError",
    "MattToolsInstaller",
    "SourceVerificationError",
    "UnsupportedInstallModeError",
    "install_bundle",
    "install_matt_tools",
    "load_bundle_manifest",
    "load_matt_tools_manifest",
    "verify_bundle",
    "verify_bundled_matt_tools",
]
