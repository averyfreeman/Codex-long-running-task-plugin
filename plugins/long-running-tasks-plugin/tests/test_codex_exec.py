from pathlib import Path

from long_running_tasks.codex_exec import build_exec_command, build_resume_command


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


def test_resume_command_uses_config_overrides(tmp_path: Path):
    command = build_resume_command(
        session_id="session-1",
        worktree=tmp_path / "worktree",
        output_file=tmp_path / "last.txt",
        approval_mode="unattended",
    )
    assert command[:4] == ["codex", "exec", "resume", "session-1"]
    assert "sandbox_mode=workspace-write" in command
    assert "approval_policy=never" in command
    assert "-C" not in command
    assert "--ask-for-approval" not in command
