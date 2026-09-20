from pathlib import Path

import pytest

from long_running_tasks.task_spec import TaskSpecError, mark_item_complete, parse_plan, parse_task


TASK = """# Task

## Goal

Make the example implementation work.

## Acceptance criteria

- The implementation creates the expected marker.
- Verification passes.

## Verification

```sh
test -f marker.txt
```
"""

PLAN = """# Plan

- [ ] T001: Create the marker
- [ ] T002: Add verification coverage
"""


def test_task_and_strict_plan_parse(tmp_path: Path):
    (tmp_path / "TASK.md").write_text(TASK, encoding="utf-8")
    (tmp_path / "PLAN.md").write_text(PLAN, encoding="utf-8")

    task = parse_task(tmp_path / "TASK.md")
    plan = parse_plan(tmp_path / "PLAN.md")

    assert task.verification_command == "test -f marker.txt"
    assert task.digest
    assert [item.task_id for item in plan.items] == ["T001", "T002"]
    assert plan.next_item.task_id == "T001"
    updated = mark_item_complete(plan, "T001")
    assert "- [x] T001" in updated
    assert "- [ ] T002" in updated


def test_plan_rejects_duplicate_or_loose_ids(tmp_path: Path):
    path = tmp_path / "PLAN.md"
    path.write_text("- [ ] T001: one\n- [ ] T001: duplicate\n", encoding="utf-8")
    with pytest.raises(TaskSpecError, match="duplicate"):
        parse_plan(path)
    path.write_text("- [ ] T1: too short\n", encoding="utf-8")
    with pytest.raises(TaskSpecError):
        parse_plan(path)


def test_task_requires_one_verification_command(tmp_path: Path):
    path = tmp_path / "TASK.md"
    path.write_text(TASK.replace("test -f marker.txt", "test -f marker.txt\nprintf bad"), encoding="utf-8")
    with pytest.raises(TaskSpecError, match="exactly one command"):
        parse_task(path)
