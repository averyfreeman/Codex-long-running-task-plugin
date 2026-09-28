# Task

## Goal

Implement the approved complementary integration between the separate
`git-bbq-matt-skills` source repository and Long-Running Tasks. Add a local-only,
serial-first canonical workflow record that links triage, spec, ticket, and run
artifacts; add a pinned offline `matt-tools-three` installer; preserve the
existing direct runner workflow; and document the resulting interfaces and
recovery behavior.

## Acceptance criteria

- Existing direct `start_run` behavior and its current test suite remain
  compatible.
- A validated workflow record can represent triage, spec, ticket, and run
  nodes with provenance digests and blocker edges.
- The scheduler selects only current, approved, unblocked ticket nodes and
  rejects unsupported parallel scheduling explicitly.
- A workflow run receives an immutable execution projection without mutating
  the base checkout.
- Stale source artifacts stop automatic advancement and require reconciliation.
- Failed and interrupted runs remain inspectable and recoverable.
- The default Matt-tools installer is offline-capable, idempotent, pinned, and
  does not invoke repository setup implicitly.
- Mermaid and workflow documentation describe the implemented ownership and
  lifecycle boundaries.

## Scope constraints

- The bundle ID `matt-tools-three` means exactly these source skill paths from
  the separate `git-bbq-matt-skills` repository: `skills/engineering/to-spec`,
  `skills/engineering/to-tickets`, and `skills/engineering/triage`.
- `skills/engineering/setup-matt-pocock-skills` is a companion prerequisite
  reference only. The installer must not execute repository setup implicitly,
  and it must not substitute unrelated curated skills such as `debug`,
  `design-project`, or `test-first` for the three bundle members.
- The installer must record the source repository, source commit, selected
  paths, and content digests, and support bundled, explicit-fetch, and
  sibling-checkout modes without requiring network access in bundled mode.

## Verification

```sh
cd plugins/long-running-tasks-plugin && uv run --locked pytest
```
