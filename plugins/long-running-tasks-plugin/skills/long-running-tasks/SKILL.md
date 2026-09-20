---
name: long-running-tasks
description: Run explicit repository tasks as isolated, verifiable, recoverable Codex slices with async status and logs.
---

# Long-Running Tasks

Use this workflow only when the user has an explicit multi-step repository task
that should continue with minimal interruption. The target repository must be
provided explicitly to the MCP tools.

## Prepare the repository

Before starting, ensure the target repository has:

- `TASK.md` with `## Goal`, `## Acceptance criteria`, and exactly one explicit
  shell verification command under `## Verification`.
- `PLAN.md` containing unique strict items such as `- [ ] T001: Description`.

Each unchecked plan item is one bounded slice. The runner, not Codex, marks one
item complete after verification. Codex must not edit `TASK.md`, mark `PLAN.md`,
commit, tag, merge, push, or publish.

## Use the MCP tools

Call `start_run` with an absolute `repo_path`. It returns a stable `run_id`
immediately. Poll `get_run_status` and use `get_run_logs` with bounded limits and
cursors. Use `resume_run` after `needs_approval`, `failed`, or a recoverable pause;
use `recover_run` when the state is `needs_recovery`. Use `cancel_run` only when
the user confirms cancellation. Cancellation can remain `cancelling` while the
worker exits and the repository lock is held.

Do not start a second run for the same repository while another run is active or
recoverable. The runner creates an external worktree and a branch named
`codex/long-running/<run_id>`; the base branch remains untouched.

## Approval modes

- `policy` is the default. Routine verified work continues; destructive changes,
  approval requests, ambiguity, failed verification, and merge or publish intent
  pause safely.
- `unattended` is an explicit opt-in that suppresses routine approval prompts and
  continues through automatic review where supported. Its initial invocation
  uses `--approve-for-me` without `--sandbox`; the current CLI rejects that
  combination. Native resume uses equivalent `workspace-write`,
  `approval_policy=on-request`, and `approvals_reviewer=auto_review` config
  overrides because resume does not accept `--approve-for-me`. It still stops
  on failures and risk signals, and never merges or publishes automatically.
- `on-request` passes through Codex's normal approval policy.
- `auto_review` requests automatic review where the CLI supports it.
- `never` is a separate advanced unattended mode that uses
  `--dangerously-bypass-approvals-and-sandbox`. Never combine it with
  `--approve-for-me` or `--sandbox`; it does not authorize pushing, merging,
  or publishing.

## Context pressure and recovery

The runner persists `.codex-loop/state.json` and writes an external continuation
summary before context reaches its configured threshold. It resumes the same
Codex session with `codex exec resume`. If resumption cannot be completed after
bounded retries, the run becomes `needs_recovery` rather than silently starting
an unrelated session.

Verification must pass before a slice is checkpointed. A worker restart or MCP
server restart does not silently create a second worker: stale active runs are
reconciled to `needs_recovery`, and stale locks must be recovered explicitly.
Successful checkpoints
contain exactly one completed plan item, versioned state, an implementation
commit, and an annotated tag. A failed verification leaves the worktree and
changes available for inspection without creating a checkpoint.
