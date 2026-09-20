# Long-Running Tasks

Long-Running Tasks turns an explicit `TASK.md` and strict `PLAN.md` into
isolated, verifiable Codex slices. Each successful slice updates the plan,
records versioned state, creates an implementation checkpoint, and adds an
annotated Git tag.

The plugin uses a Python stdio MCP server and a locked `uv` environment. A run
gets an external retained worktree and a branch named
`codex/long-running/<run_id>`; the target repository's base branch is never
modified. Runtime records, continuation summaries, and detailed logs stay
outside the target repository.

## Prepare a repository

The target must be a clean Git repository. Add a task definition with a goal,
observable acceptance criteria, and exactly one shell verification command:

~~~markdown
# Task

## Goal

Describe the desired outcome.

## Acceptance criteria

- State an observable acceptance condition.

## Verification

    python -m pytest
~~~

Add one idempotent slice per strict plan item:

~~~markdown
- [ ] T001: Implement the first bounded slice
- [ ] T002: Add verification coverage
~~~

The runner owns `PLAN.md`, `.codex-loop/state.json`, verification, commits, and
tags. Codex must not edit the task or plan files, commit, tag, merge, push, or
publish.

## Codex skill example

Ask Codex to start a durable run for an explicit repository:

~~~text
$long-running-tasks-plugin:long-running-tasks
Start a durable run for /absolute/path/to/repo with approval_mode=policy.
Continue verified slices automatically, pause for failures, risk, ambiguity,
or merge/publish intent, and never push or merge automatically.
~~~

The skill teaches the runner's state machine, worktree policy, context-pressure
continuation, and recovery behavior. Use the returned `run_id` to monitor the
same run rather than starting a second run for the repository.

## MCP examples

Start an asynchronous run. The tool returns immediately with a stable `run_id`:

~~~json
{
  "repo_path": "/absolute/path/to/repo",
  "approval_mode": "policy",
  "max_iterations": 10
}
~~~

Then use the read-only tools with bounded pages:

~~~json
{
  "run_id": "ab12cd34ef56"
}
~~~

~~~json
{
  "run_id": "ab12cd34ef56",
  "limit": 50,
  "cursor": "line:50"
}
~~~

Useful lifecycle calls are:

- `get_run_status(run_id)` for the current state, worktree, branch, slice, and checkpoint.
- `get_run_logs(run_id, limit, cursor)` for bounded structured progress and failure details.
- `list_runs(repo_path, limit, cursor)` to find an existing run.
- `resume_run(run_id, approval_mode?)` after an approval pause or recoverable failure.
- `recover_run(run_id, confirm=true)` after `needs_recovery` and a review of the retained worktree.
- `cancel_run(run_id, confirm=true)` to stop safely while retaining the worktree for inspection.

Mutating tools require explicit confirmation where applicable. Cancellation may
briefly return `cancelling` while the Codex and verification process groups
exit; the repository lock remains held until the worker is gone.

## CLI examples

The CLI uses the same core library and external runtime store:

~~~sh
cd /path/to/long-running-tasks-plugin/plugins/long-running-tasks-plugin

# Start a durable worker and return immediately.
uv run --locked long-running-tasks start /absolute/path/to/repo \
  --approval-mode policy --detach

# Inspect the run and page its logs.
uv run --locked long-running-tasks status ab12cd34ef56
uv run --locked long-running-tasks logs ab12cd34ef56 --limit 50

# Resume, recover, or cancel after reviewing status.
uv run --locked long-running-tasks resume ab12cd34ef56
uv run --locked long-running-tasks recover ab12cd34ef56 --confirm
uv run --locked long-running-tasks cancel ab12cd34ef56 --confirm
~~~

Set `LONG_RUNNING_TASKS_DATA_DIR` to place external logs, snapshots, journals,
and worker metadata in a chosen cache directory:

~~~sh
export LONG_RUNNING_TASKS_DATA_DIR="$HOME/.cache/long-running-tasks"
~~~

## Approval modes and unattended operation

`policy` is the safe default. Routine verified changes continue automatically;
the runner pauses for destructive changes, failed verification, ambiguous
requirements, Codex approval events, risky command intent, or merge/publish
actions.

`unattended` is the explicit opt-in for suppressing routine prompts:

~~~json
{
  "repo_path": "/absolute/path/to/repo",
  "approval_mode": "unattended"
}
~~~

It still stops on failure, risk, ambiguity, approval signals, or merge/publish
intent. It never pushes, merges, or publishes automatically. The raw
`on-request`, `auto_review`, and `never` modes remain available for compatibility;
`never` is an advanced approval bypass and should be used only when its risks
are understood.

## Checkpoints, continuation, and recovery

Before each slice is checkpointed, the runner runs the task's verification
command and checks for cancellation. A successful slice has exactly one checked
plan item, a committed `.codex-loop/state.json`, an implementation commit, and
an annotated deterministic tag such as `codex-run/ab12cd34ef56/T001`. Only the
verified slice, runner-owned `PLAN.md`, and runner-owned state are staged;
verification artifacts and detailed logs are not staged.

When context pressure reaches the configured threshold, the runner persists
state, writes an external continuation summary, and resumes the same native
Codex session with `codex exec resume`. Three failed native resume attempts
become `needs_recovery`; the runner never silently switches to an unrelated
session.

If a worker stops at a commit or tag boundary, `recover_run` reconciles the
recorded journal and exact change manifest without overwriting a conflicting
tag. Review the retained worktree before confirming recovery. A failed
verification leaves changes available for inspection and creates no checkpoint.

## Development and release checks

~~~sh
uv run --locked pytest
uv lock --check
uv run --with pyyaml python3 /Users/avery/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
~~~

The local repository marketplace is at
`../../.agents/plugins/marketplace.json`. The first release is Git-backed and
pinned to a release tag; no automatic push, merge, or publish is performed by
the runner.

## Attribution and license

Created and maintained by Avery Freeman. Source repository: [Codex-long-running-task-plugin](https://github.com/averyfreeman/Codex-long-running-task-plugin).

Released under the [MIT License](LICENSE), copyright © 2026 Avery Freeman.
