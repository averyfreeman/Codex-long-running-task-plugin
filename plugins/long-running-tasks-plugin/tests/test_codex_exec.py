from pathlib import Path
import shutil
import subprocess

import pytest

from long_running_tasks.codex_exec import (
    CodexError,
    build_exec_command,
    build_resume_command,
    preflight_command,
    validate_command_compatibility,
)


MODES = ["policy", "on-request", "auto_review", "unattended", "never"]
EXPECTED_INITIAL = {
    "policy": ["--sandbox", "workspace-write", "-c", "approval_policy=on-request"],
    "on-request": ["--sandbox", "workspace-write", "-c", "approval_policy=on-request"],
    "auto_review": [
        "--sandbox",
        "workspace-write",
        "-c",
        "approval_policy=on-request",
        "-c",
        "approvals_reviewer=auto_review",
    ],
    "unattended": ["--approve-for-me"],
    "never": ["--dangerously-bypass-approvals-and-sandbox"],
}
EXPECTED_RESUME = {
    "policy": ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=on-request"],
    "on-request": ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=on-request"],
    "auto_review": [
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        "approval_policy=on-request",
        "-c",
        "approvals_reviewer=auto_review",
    ],
    "unattended": [
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        "approval_policy=on-request",
        "-c",
        "approvals_reviewer=auto_review",
    ],
    "never": ["--dangerously-bypass-approvals-and-sandbox"],
}


def test_initial_command_uses_current_exec_flags(tmp_path: Path):
    command = build_exec_command(
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode="policy",
    )
    assert command[:4] == ["codex", "exec", "--json", "--sandbox"]
    assert "approval_policy=on-request" in command
    assert "--ask-for-approval" not in command
    assert command[-1] == "-"


@pytest.mark.parametrize("mode", MODES)
def test_initial_command_matches_approval_mode_matrix(tmp_path: Path, mode: str):
    command = build_exec_command(
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode=mode,
    )
    options = command[3 : command.index("-C")]
    assert options == EXPECTED_INITIAL[mode]
    validate_command_compatibility(command)
    assert not ("--sandbox" in command and "--approve-for-me" in command)
    assert not ("--sandbox" in command and "--dangerously-bypass-approvals-and-sandbox" in command)


@pytest.mark.parametrize("mode", MODES)
def test_resume_command_matches_approval_mode_matrix(tmp_path: Path, mode: str):
    command = build_resume_command(
        session_id="session-1",
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode=mode,
    )
    options = command[5 : command.index("--output-last-message")]
    assert options == EXPECTED_RESUME[mode]
    validate_command_compatibility(command)
    assert command[-1] == "-"
    assert "-C" not in command


@pytest.mark.parametrize(
    "command",
    [
        ["codex", "exec", "--sandbox", "workspace-write", "--approve-for-me"],
        ["codex", "exec", "--sandbox=workspace-write", "--approve-for-me"],
        ["codex", "exec", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "exec", "--sandbox", "workspace-write", "--dangerously-bypass-approvals-and-sandbox"],
    ],
)
def test_conflicting_approval_flags_are_rejected(command: list[str]):
    with pytest.raises(CodexError, match="cannot be combined"):
        validate_command_compatibility(command)


def test_preflight_checks_version_and_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable = fake_bin / "codex"
    executable.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo 'codex-cli test'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    command = build_exec_command(
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode="unattended",
    )
    assert preflight_command(command, approval_mode="unattended", resume=False) == "codex-cli test"


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
@pytest.mark.parametrize("mode", MODES)
def test_installed_codex_accepts_each_command_matrix(tmp_path: Path, mode: str):
    initial = build_exec_command(
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode=mode,
    )
    resumed = build_resume_command(
        session_id="parser-only-session",
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode=mode,
    )
    assert preflight_command(initial, approval_mode=mode, resume=False)
    assert preflight_command(resumed, approval_mode=mode, resume=True)


def test_current_codex_rejects_the_legacy_combination(tmp_path: Path):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    result = subprocess.run(
        [
            codex,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--approve-for-me",
            "--ephemeral",
            "-",
        ],
        cwd=tmp_path,
        input="Reply with exactly OK.\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot be used with" in result.stderr
