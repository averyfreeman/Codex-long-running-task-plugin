from __future__ import annotations

from scripts.validate_integration import validate


def test_release_metadata_bundle_and_workflow_docs_are_consistent():
    assert validate() == []
