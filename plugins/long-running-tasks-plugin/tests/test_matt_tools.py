from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from long_running_tasks.matt_tools import (
    BundleFile,
    BundleManifest,
    ChecksumVerificationError,
    InstallationConflictError,
    MATT_TOOLS_COMPANION_PATHS,
    MATT_TOOLS_SOURCE_COMMIT,
    MATT_TOOLS_SOURCE_PATHS,
    MattToolsInstaller,
    SourceVerificationError,
    install_matt_tools,
    load_matt_tools_manifest,
    verify_bundled_matt_tools,
)


PLUGIN_ROOT = Path(__file__).parents[1]
BUNDLE_ROOT = PLUGIN_ROOT / "src" / "long_running_tasks" / "bundles" / "matt-tools-three"


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _local_source(tmp_path: Path) -> tuple[Path, BundleManifest]:
    source = tmp_path / "source"
    source.mkdir()
    contents = {
        "skills/engineering/to-spec/SKILL.md": "# to-spec\n",
        "skills/engineering/to-spec/agents/openai.yaml": "interface:\n  display_name: to-spec\n",
        "skills/engineering/to-tickets/SKILL.md": "# to-tickets\n",
        "skills/engineering/to-tickets/agents/openai.yaml": "interface:\n  display_name: to-tickets\n",
        "skills/engineering/triage/SKILL.md": "# triage\n",
    }
    for relative, content in contents.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git("init", "-q", cwd=source)
    _git("config", "user.email", "tests@example.invalid", cwd=source)
    _git("config", "user.name", "Offline Test", cwd=source)
    _git("add", ".", cwd=source)
    _git("commit", "-q", "-m", "fixture", cwd=source)
    commit = _git("rev-parse", "HEAD", cwd=source)
    selected_paths = tuple(
        sorted({"/".join(Path(path).parts[:3]) for path in contents})
    )
    # The paths above are intentionally assembled from the source tree rather
    # than copied from the production pin, so the source-backed modes prove
    # their generic behavior with a fully local repository.
    files = tuple(
        BundleFile(relative, relative, hashlib.sha256(content.encode("utf-8")).hexdigest())
        for relative, content in contents.items()
    )
    manifest = BundleManifest(
        bundle_id="fixture-bundle",
        source_repository=str(source),
        source_commit=commit,
        selected_paths=selected_paths,
        files=files,
    ).validate()
    return source, manifest


def test_pinned_manifest_contains_exact_three_skills_and_bundle_checksums():
    manifest = load_matt_tools_manifest()

    assert manifest.source_commit == MATT_TOOLS_SOURCE_COMMIT
    assert manifest.selected_paths == MATT_TOOLS_SOURCE_PATHS
    assert len(manifest.files) == 8
    assert set(MATT_TOOLS_COMPANION_PATHS).isdisjoint(manifest.selected_paths)
    assert manifest.bundle_digest == manifest.computed_bundle_digest
    assert verify_bundled_matt_tools() == manifest


def test_bundled_install_is_offline_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def no_process(*_args, **_kwargs):
        raise AssertionError("bundled installation must not invoke a process")

    monkeypatch.setattr("long_running_tasks.matt_tools.subprocess.run", no_process)
    destination = tmp_path / "skills"

    first = install_matt_tools(destination)
    record_path = destination / ".matt-tools-three.install.json"
    record_before = record_path.read_bytes()
    second = install_matt_tools(destination)

    assert first.changed is True
    assert first.already_installed is False
    assert second.changed is False
    assert second.already_installed is True
    assert record_path.read_bytes() == record_before
    assert (destination / "to-spec" / "SKILL.md").is_file()
    assert (destination / "to-tickets" / "agents" / "openai.yaml").is_file()
    assert (destination / "triage" / "OUT-OF-SCOPE.md").is_file()
    assert not (destination / "setup-matt-pocock-skills").exists()

    record = json.loads(record_before)
    assert record["source_commit"] == MATT_TOOLS_SOURCE_COMMIT
    assert record["selected_paths"] == list(MATT_TOOLS_SOURCE_PATHS)
    assert {item["source_path"] for item in record["files"]} == {
        item.source_path for item in load_matt_tools_manifest().files
    }


def test_bundled_checksum_mismatch_is_rejected_before_install(tmp_path: Path):
    bundle_copy = tmp_path / "bundle"
    shutil.copytree(BUNDLE_ROOT, bundle_copy)
    tampered = bundle_copy / "skills" / "engineering" / "triage" / "SKILL.md"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(ChecksumVerificationError, match="Checksum mismatch"):
        verify_bundled_matt_tools(bundle_root=bundle_copy)

    destination = tmp_path / "destination"
    installer = MattToolsInstaller(bundle_root=bundle_copy)
    with pytest.raises(ChecksumVerificationError):
        installer.install(destination)
    assert not destination.exists() or not list(destination.rglob("*"))


def test_sibling_checkout_requires_the_pinned_commit_and_verifies_content(tmp_path: Path):
    source, manifest = _local_source(tmp_path)
    destination = tmp_path / "skills"
    installer = MattToolsInstaller(manifest=manifest)

    result = installer.install(destination, mode="sibling-checkout", source_checkout=source)
    assert result.changed is True
    assert result.source_commit == manifest.source_commit

    changed = source / "skills/engineering/to-spec/SKILL.md"
    changed.write_text("changed after commit\n", encoding="utf-8")
    with pytest.raises(ChecksumVerificationError):
        installer.install(tmp_path / "changed-destination", mode="sibling-checkout", source_checkout=source)


def test_explicit_fetch_works_from_a_local_repository_without_network(tmp_path: Path):
    source, manifest = _local_source(tmp_path)
    result = MattToolsInstaller(manifest=manifest).install(
        tmp_path / "skills",
        mode="explicit-fetch",
        source_repository=str(source),
    )

    assert result.changed is True
    assert (tmp_path / "skills" / "to-spec" / "SKILL.md").read_text(encoding="utf-8") == "# to-spec\n"


def test_changed_managed_file_is_not_overwritten_on_repeat_install(tmp_path: Path):
    destination = tmp_path / "skills"
    install_matt_tools(destination)
    target = destination / "triage" / "SKILL.md"
    target.write_text("local edit\n", encoding="utf-8")

    with pytest.raises(InstallationConflictError, match="changed"):
        install_matt_tools(destination)


def test_source_mode_without_a_checkout_fails_explicitly(tmp_path: Path):
    with pytest.raises(SourceVerificationError, match="requires source_checkout"):
        MattToolsInstaller(manifest=_local_source(tmp_path)[1]).install(
            tmp_path / "skills", mode="sibling-checkout"
        )
