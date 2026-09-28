#!/usr/bin/env python3
"""Validate release metadata and the documented workflow integration.

This check is deliberately local and dependency-light. It catches drift among
the plugin manifest, Python package metadata, lockfile, marketplace pin,
vendored Matt-tools checksums, and the agent-facing workflow documentation.
It does not run Git, fetch a source checkout, or mutate the repository.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parent.parent
VERSION_PATTERN = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
LOCK_PACKAGE_PATTERN = re.compile(
    r'(?ms)^\[\[package\]\]\nname = "long-running-tasks-plugin"\nversion = "([^"]+)"'
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _require_text(value: object, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    return value


def validate() -> list[str]:
    errors: list[str] = []
    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    pyproject_path = PLUGIN_ROOT / "pyproject.toml"
    version_path = PLUGIN_ROOT / "src" / "long_running_tasks" / "__init__.py"
    lock_path = PLUGIN_ROOT / "uv.lock"
    marketplace_path = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"

    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"unable to read plugin manifest: {exc}")
        manifest = {}
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"unable to read pyproject metadata: {exc}")
        project = {}

    versions = {
        "plugin.json": manifest.get("version"),
        "pyproject.toml": project.get("version"),
    }
    version_match = VERSION_PATTERN.search(version_path.read_text(encoding="utf-8")) if version_path.is_file() else None
    versions["__version__"] = version_match.group(1) if version_match else None
    lock_match = LOCK_PACKAGE_PATTERN.search(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else None
    versions["uv.lock"] = lock_match.group(1) if lock_match else None

    release_version: str | None = None
    for label, value in versions.items():
        text = _require_text(value, label, errors)
        if text is not None:
            release_version = release_version or text
            if text != release_version:
                errors.append(f"release version drift: {label} has {text}, expected {release_version}")

    try:
        marketplace = _read_json(marketplace_path)
        plugins = marketplace.get("plugins", [])
        entry = next(item for item in plugins if item.get("name") == manifest.get("name"))
        ref = entry.get("source", {}).get("ref")
        expected_ref = f"v{release_version}" if release_version else None
        if ref != expected_ref:
            errors.append(f"marketplace release ref is {ref!r}, expected {expected_ref!r}")
    except (OSError, ValueError, StopIteration, AttributeError, TypeError) as exc:
        errors.append(f"unable to validate marketplace release pin: {exc}")

    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "src"))
        from long_running_tasks.matt_tools import verify_bundled_matt_tools

        verify_bundled_matt_tools()
    except Exception as exc:  # pragma: no cover - the message is the useful result
        errors.append(f"vendored Matt-tools bundle validation failed: {exc}")

    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8") if (PLUGIN_ROOT / "README.md").is_file() else ""
    skill = (PLUGIN_ROOT / "skills" / "long-running-tasks" / "SKILL.md").read_text(encoding="utf-8") if (PLUGIN_ROOT / "skills" / "long-running-tasks" / "SKILL.md").is_file() else ""
    workflow_doc = (PLUGIN_ROOT / "docs" / "workflow.md").read_text(encoding="utf-8") if (PLUGIN_ROOT / "docs" / "workflow.md").is_file() else ""
    for label, text in (("plugin README", readme), ("skill guidance", skill), ("workflow documentation", workflow_doc)):
        if not text:
            errors.append(f"{label} is missing")
    required_doc_terms = (
        "triage",
        "spec",
        "ticket",
        "run",
        "start_run",
        "stale",
        "reconcile",
        "projection",
        "mermaid",
    )
    combined = "\n".join((readme, skill, workflow_doc)).lower()
    for term in required_doc_terms:
        if term.lower() not in combined:
            errors.append(f"workflow documentation is missing {term!r}")
    if "```mermaid" not in workflow_doc.lower():
        errors.append("workflow documentation must contain a Mermaid diagram")

    changelog = PLUGIN_ROOT / "CHANGELOG.md"
    if release_version and (not changelog.is_file() or f"## {release_version}" not in changelog.read_text(encoding="utf-8")):
        errors.append(f"CHANGELOG.md must contain a {release_version} release entry")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Workflow integration validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Workflow integration validation passed: {PLUGIN_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
