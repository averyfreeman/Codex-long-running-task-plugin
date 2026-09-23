# Long-Running Tasks Flexibility Report

Status: decision recorded; this release changes documentation and Git policy only.

Date: 2026-09-22

## Executive recommendation

Treat the long-running task capability as **always available, selectively invoked, and persistent across qualifying turns**.

The recommended design is a passive router in front of the existing strict executor:

- The router recognizes explicit durable-work intent and continuation of an already active run.
- The executor still requires its current repository, clean-state, `TASK.md`, `PLAN.md`, verification, worktree, checkpoint, and recovery contracts.
- Ordinary lengthy requests, generic “multi-turn” language, and the presence of a default prompt do not start a run by themselves.
- A default prompt or Personalization entry should describe how to request the capability, not silently invoke it on every turn.

This preserves the useful continuity of a durable run without imposing worktree creation, Git checks, subprocesses, or verification cost on unrelated turns.

## Why “every turn use” is the wrong qualifier

“Every turn use” combines two different product decisions:

1. Whether the capability is available to the assistant without repeated setup.
2. Whether the current turn meets the conditions for invoking the capability.

The first is desirable for Personalization and Default Prompt integration. The second must remain conditional because an invocation can create external state, require a clean repository, run a verification command, and consume meaningful time and compute. The better requirement is:

> Make the capability ambiently available, but activate it only for explicit durable-work intent or continuation of an active durable run.

## Current contract and evidence

The existing implementation already has a strong execution boundary:

- `plugins/long-running-tasks-plugin/.codex-plugin/plugin.json` exposes a broad `defaultPrompt`. That field is best treated as a user-facing starter instruction, not as an unconditional per-turn execution rule.
- `plugins/long-running-tasks-plugin/skills/long-running-tasks/SKILL.md` requires an explicitly chosen repository, a clean Git state, `TASK.md`, `PLAN.md`, an explicit verification command, and a worktree-managed execution model.
- `plugins/long-running-tasks-plugin/src/long_running_tasks/mcp_server.py` requires an explicit repository path for `start_run` and describes the operation as durable work.
- `plugins/long-running-tasks-plugin/src/long_running_tasks/runner.py` enforces repository and planning prerequisites, executes verification through `/bin/sh -c`, records checkpoints, and owns worktree/task lifecycle behavior. It deliberately does not silently merge or push user work.

The flexibility change should therefore target routing and activation metadata before changing the executor. Weakening the executor to accommodate ambient invocation would make the safety contract less reliable.

## Refined evaluation criteria

Scores are from 1 (poor) to 5 (strong). A high score is desirable for every column; for “Dormant overhead” it means lower inactive-turn cost, and for “Effort” it means lower implementation effort. Weighted totals use these weights:

| Criterion | Weight | Meaning |
| --- | ---: | --- |
| Safety / contract integrity | 20% | Protects explicit repository, Git, planning, verification, and recovery preconditions. |
| Invocation precision | 20% | Starts only when the current turn actually needs durable execution. |
| Dormant overhead | 15% | Cost while the capability is available but inactive. |
| Multi-turn continuity | 15% | Continues an active run without repeated setup or accidental resets. |
| Git BBQ complement | 10% | Leaves Git policy, documentation, and release habits in the right owner. |
| Compatibility | 10% | Works with Personalization, Default Prompt, and ordinary turns. |
| Implementation effort | 5% | Small, bounded change preferred. |
| Testability | 5% | Behavior can be tested with deterministic activation and continuation cases. |

## Options

| Option | Safety | Invocation precision | Dormant overhead | Multi-turn continuity | Git BBQ complement | Compatibility | Effort | Testability | Weighted total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Prompt / metadata only | 5 | 4 | 5 | 2 | 5 | 5 | 5 | 3 | **4.25** |
| Passive router + strict executor | 5 | 5 | 4 | 5 | 5 | 4 | 3 | 4 | **4.60** |
| Router + read-only eligibility gate | 5 | 5 | 2 | 5 | 4 | 5 | 2 | 5 | **4.30** |
| Sticky opt-in mode | 4 | 4 | 3 | 5 | 3 | 3 | 2 | 3 | **3.65** |
| Ambient runner | 2 | 2 | 2 | 5 | 1 | 2 | 1 | 3 | **2.35** |

### 1. Prompt / metadata only

Add clearer activation language and continuation metadata to the skill and plugin manifest, while leaving invocation selection to the host. This is cheap and safe, but it cannot guarantee reliable selective activation or preserve continuity when the host does not carry state forward.

### 2. Passive router + strict executor — recommended

Add a small activation layer that evaluates the current turn and session state. It emits an invocation decision only for explicit durable-work intent or a known active-run continuation. The existing runner remains the enforcement boundary. This is the best balance: low inactive-turn cost, strong precision, and no duplication of Git policy.

### 3. Router + read-only eligibility gate

Add the router plus inexpensive preflight checks before starting the runner, such as repository existence, required files, and clean-state eligibility. This improves diagnostics and testability, but it adds per-candidate filesystem and Git work and can duplicate checks that the executor must still repeat.

### 4. Sticky opt-in mode

Let the user explicitly enable the capability for a session or project, then continue considering it until disabled. This supports multi-turn work well, but it introduces lifecycle, expiration, and stale-state questions and may be surprising in Personalization.

### 5. Ambient runner

Start or inspect durable runs whenever a request is merely lengthy or multi-turn. This maximizes continuity but fails the current contract: it broadens invocation too far, creates expensive stateful behavior without explicit intent, and makes Default Prompt routing unsafe.

## Ownership and integration choices

The flexibility feature should remain adjacent to Git BBQ rather than absorbing Git BBQ’s responsibilities.

| Ownership model | Benefits | Costs | Assessment |
| --- | --- | --- | --- |
| Concurrent Git BBQ + this plugin | Git BBQ owns `.githabits.yaml`, architecture records, and release policy; this plugin owns durable execution, routing, worktrees, checkpoints, and recovery. | Requires a documented boundary and coordination at integration points. | **Recommended**: clear ownership with low duplication. |
| Consolidate behavior into this plugin | One plugin could expose routing, execution, and policy. | Broadens the runner, duplicates Git BBQ policy/docs, and increases the blast radius of activation changes. | Not preferred. |
| Separate companion router plugin | Isolates Personalization/default-prompt routing. | Adds another install/discovery dependency and risks duplicated activation state. | Viable only if host limitations prevent a passive router here. |

The recommended seam is: Git BBQ governs repository habits and documentation; this plugin governs whether and how a durable execution is started or continued.

## Plugin ecosystem check

The available plugin catalog did not expose a one-to-one replacement for this repository’s contract. The closest results included methodology/TDD-oriented tooling such as `superpowers`, repository or GitHub integration, and terminal/filesystem helpers. None combined explicit `TASK.md` / `PLAN.md` requirements, isolated worktrees, verification commands, checkpoints, recovery, and long-running state in the same way.

That search is evidence against replacing the plugin wholesale, not evidence that no future integration is possible. A passive router can remain a thin compatibility layer while the current executor stays authoritative.

## Proposed activation lifecycle

1. **Idle:** Personalization or Default Prompt makes the capability discoverable; no runner is started and no worktree is created.
2. **Candidate:** A low-cost router checks for explicit durable-work language or an active-run continuation marker.
3. **Activate:** Only a qualifying candidate invokes the strict executor with an explicit repository and verification contract.
4. **Continue:** An active run identifier carries the task through later qualifying turns without repeating setup.
5. **Pause or complete:** The run records its checkpoint or terminal state; later unrelated turns return to idle.

The important distinction is that availability is persistent, while execution is event-driven.

## Decision record

Decision: adopt the **passive router + strict executor** direction as the target architecture.

Rationale: it directly addresses the Default Prompt over-selection problem, preserves the runner’s existing safety contract, limits inactive-turn cost, and supports multi-turn continuation. It also leaves Git habits and repository documentation under Git BBQ rather than introducing a second policy system.

This release intentionally does not implement runtime routing. Runtime work should be a separate change with activation, continuation, negative, and cost-boundary tests. The current release records the decision and configures the repository policy needed to ship it cleanly.

## Future implementation acceptance criteria

- A generic long request does not start a durable run solely because it spans multiple turns.
- An explicit request to execute repository work starts a run only after the current executor preconditions are satisfied.
- A continuation of an active run is recognized without requiring the user to restate repository, task, plan, and verification details.
- An unrelated turn while a run is idle does not create a worktree, checkpoint, subprocess, or verification process.
- A failed or stale run is recoverable and cannot be silently resumed against a different repository.
- Router decisions and executor rejections are observable and deterministic in tests.
- Git BBQ remains the owner of repository policy, documentation workflow, and release habits.

## Release note

The repository changes for this release are limited to this report and the corrected `.githabits.yaml` policy. Package and plugin metadata remain at `0.1.2`; `v0.1.3` is the next repository patch tag for this documentation/policy snapshot.
