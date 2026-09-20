from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


APPROVAL_MODES = {"policy", "on-request", "auto_review", "never", "unattended"}


class CodexError(RuntimeError):
    """Raised when the Codex executable cannot be started or resumed."""


@dataclass
class CodexResult:
    returncode: int
    session_id: str | None
    context_ratio: float
    context_pressure: bool
    approval_requested: bool
    cancelled: bool
    timed_out: bool
    stdout: str
    stderr: str
    events: list[dict] = field(default_factory=list)


def ensure_codex() -> str:
    executable = shutil.which("codex")
    if not executable:
        raise CodexError("Codex CLI was not found on PATH")
    return executable


def _approval_policy(mode: str, *, resume: bool) -> str:
    if mode not in APPROVAL_MODES:
        raise ValueError(f"Unsupported approval mode: {mode}")
    if mode in {"never", "unattended"}:
        return "never"
    if mode == "auto_review":
        return "never" if resume else "on-request"
    return "on-request"


def build_exec_command(
    *,
    worktree: Path,
    output_file: Path,
    approval_mode: str,
    model: str | None = None,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "-c",
        f"approval_policy={_approval_policy(approval_mode, resume=False)}",
        "-C",
        str(worktree),
        "--output-last-message",
        str(output_file),
    ]
    if approval_mode in {"auto_review", "unattended"}:
        command.append("--approve-for-me")
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def build_resume_command(
    *,
    session_id: str,
    worktree: Path,
    output_file: Path,
    approval_mode: str,
    model: str | None = None,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "resume",
        session_id,
        "--json",
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        f"approval_policy={_approval_policy(approval_mode, resume=True)}",
        "--output-last-message",
        str(output_file),
    ]
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def _terminate_group(process: subprocess.Popen[str], grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _event_session_id(event: dict) -> str | None:
    event_type = str(event.get("type", "")).casefold()
    if "thread" not in event_type and "session" not in event_type:
        return None
    for key in ("thread_id", "session_id", "id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _find_numbers(value: object, keys: set[str]) -> dict[str, float]:
    found: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in keys and isinstance(child, (int, float)):
                found[normalized] = float(child)
            found.update(_find_numbers(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_numbers(child, keys))
    return found


def _context_ratio(event: dict) -> float | None:
    numbers = _find_numbers(
        event,
        {
            "context_used_tokens",
            "input_tokens",
            "total_tokens",
            "used_tokens",
            "context_window",
            "max_context_tokens",
            "context_limit",
        },
    )
    used = next((numbers[key] for key in ("context_used_tokens", "input_tokens", "total_tokens", "used_tokens") if key in numbers), None)
    maximum = next((numbers[key] for key in ("context_window", "max_context_tokens", "context_limit") if key in numbers), None)
    if used is None or not maximum:
        return None
    return max(0.0, min(1.0, used / maximum))


class CodexExecutor:
    def __init__(self, *, context_budget_tokens: int = 100_000, threshold: float = 0.80):
        self.context_budget_tokens = context_budget_tokens
        self.threshold = threshold
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._process

    def cancel(self) -> None:
        with self._process_lock:
            if self._process is not None:
                _terminate_group(self._process)

    def run(
        self,
        *,
        prompt: str,
        worktree: Path,
        output_file: Path,
        approval_mode: str,
        model: str | None = None,
        session_id: str | None = None,
        timeout_seconds: float = 1800,
        cancel_event: threading.Event | None = None,
        event_callback: Callable[[dict], None] | None = None,
    ) -> CodexResult:
        executable = ensure_codex()
        command = (
            build_resume_command(
                session_id=session_id,
                worktree=worktree,
                output_file=output_file,
                approval_mode=approval_mode,
                model=model,
            )
            if session_id
            else build_exec_command(
                worktree=worktree,
                output_file=output_file,
                approval_mode=approval_mode,
                model=model,
            )
        )
        command[0] = executable
        output_file.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            command,
            cwd=worktree,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with self._process_lock:
            self._process = process

        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def drain(stream: object, label: str) -> None:
            assert hasattr(stream, "readline")
            while True:
                line = stream.readline()  # type: ignore[attr-defined]
                if not line:
                    break
                lines.put((label, line))
            lines.put((label, None))

        stdout_thread = threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.write("\n")
            process.stdin.close()
        except BrokenPipeError:
            pass

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        events: list[dict] = []
        session = session_id
        ratio: float | None = None
        context_pressure = False
        approval_requested = False
        cancelled = False
        timed_out = False
        started = time.monotonic()
        closed_streams = 0
        while process.poll() is None or closed_streams < 2 or not lines.empty():
            if cancel_event is not None and cancel_event.is_set() and not cancelled:
                cancelled = True
                _terminate_group(process)
            if time.monotonic() - started > timeout_seconds and process.poll() is None:
                timed_out = True
                _terminate_group(process)
            try:
                label, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                closed_streams += 1
                continue
            if label == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
            if label == "stdout":
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    events.append(event)
                    session = _event_session_id(event) or session
                    ratio = _context_ratio(event) or ratio
                    event_type = str(event.get("type", "")).casefold()
                    approval_requested = approval_requested or "approval" in event_type or bool(event.get("requires_approval"))
                    if ratio is not None and ratio >= self.threshold and not context_pressure:
                        context_pressure = True
                        _terminate_group(process)
                    if event_callback:
                        event_callback(event)
            elif re.search(r"approval required|needs approval|requesting approval", line, re.IGNORECASE):
                approval_requested = True

        process.wait()
        with self._process_lock:
            self._process = None
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        if ratio is None:
            estimated = (len(prompt) + len(stdout) + len(stderr)) / 4
            ratio = max(0.0, min(1.0, estimated / self.context_budget_tokens))
        if ratio >= self.threshold:
            context_pressure = True
        return CodexResult(
            returncode=process.returncode,
            session_id=session,
            context_ratio=ratio,
            context_pressure=context_pressure,
            approval_requested=approval_requested,
            cancelled=cancelled,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            events=events,
        )
