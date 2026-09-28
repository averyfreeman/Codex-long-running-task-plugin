"""Small deterministic local fixtures for workflow-model tests and examples."""

from __future__ import annotations

from importlib.resources import files

from .workflow import WorkflowRecord


def load_workflow_fixture(name: str = "workflow_record.json") -> WorkflowRecord:
    """Load a packaged local workflow fixture through the public model parser."""

    resource = files("long_running_tasks").joinpath("fixtures", name)
    try:
        return WorkflowRecord.from_json(resource.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Unknown workflow fixture: {name}") from exc


def sample_workflow_record() -> WorkflowRecord:
    """Return the complete triage/spec/ticket/run fixture record."""

    return load_workflow_fixture()


__all__ = ["load_workflow_fixture", "sample_workflow_record"]

