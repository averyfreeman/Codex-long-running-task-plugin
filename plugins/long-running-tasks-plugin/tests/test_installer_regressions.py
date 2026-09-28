from __future__ import annotations

import json
from pathlib import Path

import pytest

from long_running_tasks.matt_tools import (
    InstallationConflictError,
    MattToolsInstaller,
    install_matt_tools,
    load_matt_tools_manifest,
)


def test_source_layout_preserves_pinned_tree_and_is_idempotent(tmp_path: Path):
    destination = tmp_path / "skills"
    installer = MattToolsInstaller(
        clock=lambda: "2026-01-01T00:00:00+00:00",
    )

    first = installer.install(destination, layout="source")
    record_path = destination / ".matt-tools-three.install.json"
    record_before = record_path.read_bytes()
    second = installer.install(destination, layout="source")

    assert first.changed is True
    assert second.already_installed is True
    assert second.changed is False
    assert record_path.read_bytes() == record_before
    assert (destination / "skills/engineering/to-spec/SKILL.md").is_file()
    assert (destination / "skills/engineering/to-tickets/agents/openai.yaml").is_file()
    assert (destination / "skills/engineering/triage/SKILL.md").is_file()
    assert not (destination / "to-spec").exists()
    assert json.loads(record_before)["destination_layout"] == "source"


def test_install_layout_change_is_a_conflict_not_a_silent_rewrite(tmp_path: Path):
    destination = tmp_path / "skills"
    install_matt_tools(destination)

    with pytest.raises(InstallationConflictError, match="different Matt-tools projection"):
        install_matt_tools(destination, layout="source")


def test_pinned_manifest_exposes_only_the_three_selected_source_roots():
    manifest = load_matt_tools_manifest()

    assert manifest.selected_paths == (
        "skills/engineering/to-spec",
        "skills/engineering/to-tickets",
        "skills/engineering/triage",
    )
    assert all(
        item.source_path.startswith(manifest.selected_paths)
        for item in manifest.files
    )
    assert "setup-matt-pocock-skills" not in " ".join(item.source_path for item in manifest.files)
