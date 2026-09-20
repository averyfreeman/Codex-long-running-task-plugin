from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


class TaskSpecError(ValueError):
    """Raised when TASK.md or PLAN.md violates the runner contract."""


@dataclass(frozen=True)
class TaskSpec:
    path: Path
    goal: str
    acceptance_criteria: tuple[str, ...]
    verification_command: str
    text: str
    digest: str


@dataclass(frozen=True)
class PlanItem:
    task_id: str
    description: str
    checked: bool
    line_number: int
    raw_line: str


@dataclass(frozen=True)
class PlanSpec:
    path: Path
    items: tuple[PlanItem, ...]
    text: str
    digest: str

    @property
    def next_item(self) -> PlanItem | None:
        return next((item for item in self.items if not item.checked), None)

    @property
    def complete(self) -> bool:
        return bool(self.items) and all(item.checked for item in self.items)


PLAN_ITEM_RE = re.compile(r"^- \[(?P<checked>[ xX])\] (?P<id>T\d{3,}): (?P<description>\S.*)$")
HEADING_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
FENCE_RE = re.compile(r"```(?P<language>[A-Za-z0-9_+-]*)\n(?P<body>.*?)```", re.DOTALL)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _section(text: str, name: str) -> str:
    matches = list(HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        if match.group("name").strip().casefold() == name.casefold():
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return text[match.end() : end].strip()
    raise TaskSpecError(f"TASK.md must contain a ## {name} section")


def parse_task(path: Path) -> TaskSpec:
    path = Path(path)
    if not path.is_file():
        raise TaskSpecError(f"Missing task file: {path}")
    text = path.read_text(encoding="utf-8")
    goal = _section(text, "Goal")
    if not goal:
        raise TaskSpecError("TASK.md Goal must not be empty")

    acceptance = _section(text, "Acceptance criteria")
    criteria = tuple(
        line.strip()[2:].strip()
        for line in acceptance.splitlines()
        if line.strip().startswith("-") and line.strip()[1:].strip()
    )
    if not criteria:
        raise TaskSpecError("TASK.md must contain at least one acceptance criterion")

    verification = _section(text, "Verification")
    fences = [match for match in FENCE_RE.finditer(verification) if match.group("language").casefold() in {"", "sh", "shell", "bash", "zsh"}]
    if len(fences) != 1:
        raise TaskSpecError("TASK.md Verification must contain exactly one shell code fence")
    command_lines = [line.strip() for line in fences[0].group("body").splitlines() if line.strip()]
    if len(command_lines) != 1:
        raise TaskSpecError("TASK.md Verification must contain exactly one command line")

    return TaskSpec(
        path=path,
        goal=goal,
        acceptance_criteria=criteria,
        verification_command=command_lines[0],
        text=text,
        digest=_digest(text),
    )


def parse_plan(path: Path) -> PlanSpec:
    path = Path(path)
    if not path.is_file():
        raise TaskSpecError(f"Missing plan file: {path}")
    text = path.read_text(encoding="utf-8")
    items: list[PlanItem] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = PLAN_ITEM_RE.fullmatch(line.rstrip("\r"))
        if match:
            task_id = match.group("id")
            if task_id in seen:
                raise TaskSpecError(f"PLAN.md contains duplicate item ID {task_id}")
            seen.add(task_id)
            items.append(
                PlanItem(
                    task_id=task_id,
                    description=match.group("description").strip(),
                    checked=match.group("checked").casefold() == "x",
                    line_number=line_number,
                    raw_line=line,
                )
            )
        elif re.match(r"^- \[[ xX]\]", line):
            raise TaskSpecError(f"PLAN.md line {line_number} is not a strict T### item")
    if not items:
        raise TaskSpecError("PLAN.md must contain at least one strict T### item")
    return PlanSpec(path=path, items=tuple(items), text=text, digest=_digest(text))


def mark_item_complete(plan: PlanSpec, task_id: str) -> str:
    matches = [item for item in plan.items if item.task_id == task_id]
    if len(matches) != 1:
        raise TaskSpecError(f"Unknown plan item {task_id}")
    item = matches[0]
    if item.checked:
        raise TaskSpecError(f"Plan item {task_id} is already complete")
    lines = plan.text.splitlines(keepends=True)
    index = item.line_number - 1
    original = lines[index]
    newline = "\n" if original.endswith("\n") else ""
    body = original[:-1] if newline else original
    if body.endswith("\r"):
        body = body[:-1]
        newline = "\r\n"
    lines[index] = body.replace("- [ ]", "- [x]", 1) + newline
    updated = "".join(lines)
    reparsed = parse_plan_text(plan.path, updated)
    if sum(item.checked for item in reparsed.items) != sum(item.checked for item in plan.items) + 1:
        raise TaskSpecError("Plan update must complete exactly one item")
    return updated


def parse_plan_text(path: Path, text: str) -> PlanSpec:
    temporary = PlanSpec(path=path, items=(), text=text, digest=_digest(text))
    items: list[PlanItem] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = PLAN_ITEM_RE.fullmatch(line.rstrip("\r"))
        if match:
            task_id = match.group("id")
            if task_id in seen:
                raise TaskSpecError(f"PLAN.md contains duplicate item ID {task_id}")
            seen.add(task_id)
            items.append(
                PlanItem(
                    task_id=task_id,
                    description=match.group("description").strip(),
                    checked=match.group("checked").casefold() == "x",
                    line_number=line_number,
                    raw_line=line,
                )
            )
        elif re.match(r"^- \[[ xX]\]", line):
            raise TaskSpecError(f"PLAN.md line {line_number} is not a strict T### item")
    if not items:
        raise TaskSpecError("PLAN.md must contain at least one strict T### item")
    return PlanSpec(path=path, items=tuple(items), text=text, digest=_digest(text))
