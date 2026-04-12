# Meridian Single-Agent Runbook

This is the recommended Meridian deployment shape for the current 106/107 topology.

## Goal

Run Meridian with one focused agent workflow instead of three polling personas.

- `106`: Hermes gateway and optional control scripts
- `107`: live Meridian checkout at `/home/umut/meridian`
- one active task branch at a time
- one agent session per task
- a fresh review pass before push and merge

## Why this shape

The old Philip/Fatih/Matthew daemon split did not create true parallelism.

- model access was serialized
- three loops competed for one slot
- polling created context churn
- Matthew and Philip woke too often without enough throughput gain

The single-agent model keeps the useful planning, implementation, and review lenses but removes daemon and profile overhead.

## Workflow behavior

Priority order:

1. `tasks/review/` -> Matthew-style review pass on the active branch
2. `tasks/ready/` -> Fatih-style implementation pass on a new task branch
3. `tasks/waiting_human/` or `customer_support/inbox/` -> Philip-style planning pass
4. otherwise -> stop cleanly instead of keeping an idle daemon alive

Jira remains the primary backlog system.
`tasks/` is only the execution and review artifact system.

## Default task flow

1. Start from the Meridian task packet or user request.
2. If scope is fuzzy, enter the Philip lens and tighten acceptance criteria first.
3. Claim the task and create a task branch in the Meridian repo.
4. Implement in the Fatih lens.
5. Commit task-scoped changes.
6. Re-open the work in a Matthew lens and review with fresh eyes.
7. If review passes, push the branch and merge into `main`.
8. If review fails, stay on the same branch, fix the findings, commit again, and rerun the Matthew pass.

## Operational notes

- The agent should work directly against the live checkout on `107`; it should not create or rely on a mirrored repo on `106`.
- `192.168.1.106` is the Hermes machine and `192.168.1.107` is the Meridian project machine in the current split topology.
- Use SSH access as `umut@192.168.1.106` and `umut@192.168.1.107` when the workflow needs to cross machines.
- The local Mac paths are still valid working copies for development and prompt updates:
  - `/Users/umut/Projects/hermes-agent`
  - `/Users/umut/Projects/meridian`
- Review is logically separate because it should happen with fresh reviewer attention, even if the same human and machine execute it.
