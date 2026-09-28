---
name: long-running-tasks
description: Run explicit repository tasks directly with start_run or through a local serial workflow that coordinates triage, specs, tickets, immutable projections, reconciliation, and recovery.
---

# Long-Running Tasks

Use this workflow when the user explicitly requests durable multi-step
repository work or asks to continue an existing run. The target repository
must be provided explicitly to the MCP tools.

Choose one entry point:

- Use direct `start_run` for the existing `TASK.md` / `PLAN.md` contract when
  no triage/spec/ticket coordination is requested. Its request shape and
  behavior remain unchanged.
- Use the canonical workflow path when local Matt artifacts should govern
  which ticket runs. It is local-only and serial-first; it complements the
  separate `git-bbq-matt-skills` source repository and does not replace the
  direct runner.

Do not infer a workflow from a request merely being long. A workflow needs an
explicit local record with provenance and typed edges.

## Prepare the repository

Before starting, ensure the target repository has:

- `TASK.md` with `## Goal`, `## Acceptance criteria`, and exactly one explicit
  shell verification command under `## Verification`.
- `PLAN.md` containing unique strict items such as `- [ ] T001: Description`.

Each unchecked plan item is one bounded slice. The runner, not Codex, marks one
item complete after verification. Codex must not edit `TASK.md`, mark `PLAN.md`,
commit, tag, merge, push, or publish.

## Canonical workflow path

Use the following serial sequence when triage, spec, ticket, and run artifacts
are in scope:

1. Install `matt-tools-three` with the default bundled installer when the
   source skills are needed. The bundle contains exactly `to-spec`,
   `to-tickets`, and `triage`; setup-matt-pocock-skills is a companion
   reference only. Bundled installation is pinned, checksum verified,
   offline-capable, idempotent, and does not run repository setup.
2. Read local Markdown through the artifact adapters and build a validated
   `WorkflowRecord`. Keep each source node's `provenance.digest`; represent
   blocker references as directed `blocks` edges. The record may contain
   `triage`, `spec`, `ticket`, and `run` nodes and must use
   `automation_mode: "serial"`.
3. Register the record with `create_workflow`, then call
   `schedule_workflow`. Automatic selection returns at most one current,
   approved, unblocked ticket. Parallel mode is rejected explicitly.
4. Call `start_workflow` for the selected record or workflow ID. The runner
   captures an immutable execution projection in the external runtime store;
   it does not write the base checkout. The execution still uses the existing
   isolated worktree, runner-owned branch, verification, and checkpoint
   contract.
5. Inspect `get_workflow_projection` alongside normal run status and logs to
   verify the exact ticket and source digests received by the run.

Run `reconcile_workflow` when a source artifact may have changed, disappeared,
or become unverifiable. Review its dry-run and persist the stale transition
only with explicit confirmation; the scheduler then stops automatic
advancement until a source update is represented in a current record. Existing
failed, interrupted, paused, cancelled, and `needs_recovery` runs remain
inspectable; use `resume_run` for a reviewed retry and `recover_run` after a
retained worker or checkpoint boundary requires reconciliation.

See [`docs/workflow.md`](../../docs/workflow.md) for the ownership diagram,
record shape, installer provenance, lifecycle, and recovery details.

## Use the MCP tools

Call `start_run` with an absolute `repo_path`. It returns a stable `run_id`
immediately. Poll `get_run_status` and use `get_run_logs` with bounded limits and
cursors. Use `resume_run` after `needs_approval`, `failed`, or a recoverable pause;
use `recover_run` when the state is `needs_recovery`. Use `cancel_run` only when
the user confirms cancellation. Cancellation can remain `cancelling` while the
worker exits and the repository lock is held.

Workflow-specific calls are `create_workflow`, `get_workflow`,
`list_workflows`, `schedule_workflow`, `start_workflow`,
`get_workflow_projection`, and `reconcile_workflow`. They operate on the local
canonical record and external runtime store; they do not edit Matt source
artifacts.

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
