# Meridian Orchestration Implementation Backlog

## Purpose

This backlog translates the Meridian orchestration RFC into implementation-ready workstreams.

Primary reference:
- `docs/meridian/orchestration-rfc.md`

Design intent:
- keep `tasks/` as the canonical source of truth
- preserve the Philip -> Fatih -> Matthew operating model
- move workflow control from prompt luck into deterministic orchestration code

## Implementation Order

Recommended order:
1. transition engine and task workflow tools
2. dispatcher v2 planner logic
3. runtime actuation, leases, and idempotency
4. skill contract tightening
5. observability and operator commands

This order minimizes rework because later phases depend on explicit transition primitives.

## Epic A: Transition Engine

### Goal

Introduce official workflow primitives for claiming tasks and transitioning them between queues.

### Why First

Without explicit transitions, every other orchestration layer remains fragile because task movement is still implicit and persona-driven.

### Deliverables

- `task_claim` workflow primitive
- `task_transition` workflow primitive
- transition validation rules
- frontmatter synchronization for `status` and timestamps
- append-only transition audit trail
- tests covering valid and invalid transitions

### Proposed Scope

New or updated capabilities:
- locate a task by `task_id`
- atomically move a task file between queue directories
- update task metadata such as:
  - `status`
  - `claimed_by`
  - `claimed_at`
  - `last_transition_at`
  - `waiting_on`
  - `blocked_reason`
- reject invalid transitions with clear errors
- write transition history in a structured form

### Suggested File Targets

- `tools/` new Meridian workflow tool module
- `hermes_cli/meridian_dispatcher.py`
- Meridian tests under `tests/`

### Acceptance Criteria

- tasks can be claimed through a single official entry point
- tasks can be transitioned through a single official entry point
- invalid queue moves are rejected deterministically
- queue directory and frontmatter state stay in sync
- every transition is auditable

### Risks

- partial writes causing queue/frontmatter mismatch
- accidental breaking of manual task editing workflows

### Notes

Use atomic file operations and keep the task file canonical.

## Epic B: Dispatcher V2 Planner

### Goal

Turn the current dispatcher from a status helper into a real planning engine.

### Deliverables

- queue snapshot refactor
- `planned_actions` output
- planning lane and delivery lane handling
- ready replenishment policy
- stale task detection
- selection policy beyond simple directory ordering

### Proposed Scope

Planner behavior should support:
- review loop continuity
- active delivery work prioritization
- Philip backlog grooming even when delivery is active
- `ready_target` policy
- stale detection for `in_progress`, `review`, and `waiting_human`

### Suggested File Targets

- `hermes_cli/meridian_dispatcher.py`
- new helper module if the file gets too large
- tests for planner behavior

### Acceptance Criteria

- planner emits explicit next actions instead of only a single suggestion string
- planner can schedule Philip to replenish `ready/` without waiting for delivery to go fully idle
- stale work is surfaced as a first-class planning concern
- selection policy is deterministic and covered by tests

### Risks

- overcomplicated heuristics that are hard to trust
- unstable priority rules that change behavior between ticks

### Notes

Favor deterministic and explainable rules over clever scoring.

## Epic C: Runtime Actuation, Leases, and Idempotency

### Goal

Allow planned actions to be executed safely without duplicate worker wakeups or conflicting task ownership.

### Deliverables

- orchestrator lease
- worker-task lease model
- idempotency keys for wakeups
- execution path from `planned_actions` to actual worker invocation
- reconcile-safe reruns

### Proposed Scope

Runtime capabilities:
- only one orchestration pass may act at a time
- each worker assignment carries lease metadata
- retries do not create duplicate wakeups
- abandoned work can be recovered after lease expiry

### Suggested File Targets

- new Meridian runtime/orchestration module
- `hermes_cli/meridian_dispatcher.py`
- integration points in agent orchestration

### Acceptance Criteria

- duplicate dispatches are suppressed
- task claims are lease-backed
- stale or abandoned leases can be recovered safely
- actuation can be retried idempotently

### Risks

- adding hidden state that diverges from task files
- lease expiry policy being too aggressive or too slow

### Notes

`workflow_state.json` should remain derived bookkeeping, not canonical state.

## Epic D: Event Ingestion and Reconcile

### Goal

Make Meridian genuinely event-driven while remaining resilient to missed events and manual edits.

### Deliverables

- event emission from workflow tools
- filesystem watcher or equivalent trigger
- periodic reconcile pass
- operator-safe recovery after restart

### Proposed Scope

Support these event classes:
- `task_created`
- `task_updated`
- `task_claimed`
- `task_transitioned`
- `human_confirmation_received`
- `stale_task_detected`

### Suggested File Targets

- workflow tool module
- Meridian runtime module
- optional hook points in cron or gateway integration

### Acceptance Criteria

- explicit transitions emit orchestration events
- manual edits can still be recovered by reconcile
- restart does not lose the system’s ability to continue

### Risks

- watcher-only model being brittle
- duplicate events causing repeated wakeups

### Notes

Use event-driven first, reconcile second.

## Epic E: Skill Contract Tightening

### Goal

Update persona skills so workers use explicit workflow tools instead of ad hoc file movement.

### Deliverables

- Philip skill updated to use official transition tools for backlog grooming
- Fatih skill updated to claim and transition ready work explicitly
- Matthew skill updated to approve, return, or escalate through official transitions
- workflow skill updated to reflect planner/worker split

### Suggested File Targets

- `skills/meridian/philip/SKILL.md`
- `skills/meridian/fatih/SKILL.md`
- `skills/meridian/matthew/SKILL.md`
- `skills/meridian/workflow/SKILL.md`
- related Meridian docs

### Acceptance Criteria

- no skill instructs the agent to rely on raw file moves as the primary workflow API
- worker responsibilities align with the new deterministic engine
- docs and skills tell the same story

### Risks

- skills drifting from implementation reality
- old habits lingering in prompt text

## Epic F: Observability and Operator Experience

### Goal

Make Meridian inspectable and operable by humans.

### Deliverables

- expanded `hermes meridian status`
- lease inspection
- stale task inspection
- task history lookup
- clearer dispatch output

### Suggested CLI commands

- `hermes meridian status`
- `hermes meridian dispatch`
- `hermes meridian leases`
- `hermes meridian stale`
- `hermes meridian history <task-id>`

### Acceptance Criteria

- a human can explain why the planner picked a persona
- a human can see which tasks are stale
- a human can inspect current leases and recent transitions

## Cross-Cutting Test Backlog

### Unit Tests

- valid and invalid transitions
- metadata synchronization
- planner selection policy
- ready replenishment rules
- stale detection
- idempotency key stability

### Integration Tests

- backlog contains promotable tasks and `ready/` is replenished
- stale `in_progress` does not permanently starve Philip
- review loop continuity outranks unrelated new work
- waiting-human hold and resume path works
- manual file changes are recovered by reconcile

### End-to-End Tests

- Philip promotes a backlog task to `ready/`
- Fatih claims ready work and moves it to `review/`
- Matthew approves or returns review work
- risky review routes to waiting human and resumes correctly

## Recommended Agent Sequencing

If implementing with multiple agents, use this order:

1. Agent 1: Transition engine
2. Agent 2: Planner and dispatcher v2
3. Agent 3: Runtime actuation and leases
4. Agent 4: Skill tightening and docs sync
5. Agent 5: Observability and follow-up test hardening

Reason:
- each later stream depends on clearer contracts from the previous one

## Ready-to-Assign Task Cards

### Task 1: Build Meridian transition primitives

Implement official `task_claim` and `task_transition` primitives for Meridian workflow state changes.

Done when:
- claims and transitions are atomic
- frontmatter is synchronized
- invalid transitions are rejected
- transition history is recorded
- tests cover success and failure cases

### Task 2: Refactor dispatcher into snapshot + planner

Refactor the Meridian dispatcher to produce explicit `planned_actions` and support planning lane plus delivery lane behavior.

Done when:
- planner output is structured
- ready replenishment is supported
- stale work is surfaced
- planner behavior is deterministic and test-covered

### Task 3: Add actuation and lease management

Add orchestration and worker leases plus idempotent actuation of planned actions.

Done when:
- duplicate dispatches are suppressed
- worker ownership is lease-backed
- abandoned work can be recovered

### Task 4: Add events and reconcile loop

Emit workflow events from transitions and add a reconcile path for recovery from manual edits or missed events.

Done when:
- explicit transitions trigger orchestration
- reconcile restores correctness after drift

### Task 5: Tighten persona skills

Update Meridian persona skills to use the new workflow engine primitives instead of direct file-driven behavior.

Done when:
- prompts reflect the deterministic workflow model
- skills and docs are aligned

## Prompt for the Next Agent

Use this prompt for the next implementation-focused agent:

```text
Read these files first:
- docs/meridian/orchestration-rfc.md
- docs/meridian/orchestration-implementation-backlog.md
- hermes_cli/meridian_dispatcher.py
- skills/meridian/philip/SKILL.md
- skills/meridian/fatih/SKILL.md
- skills/meridian/matthew/SKILL.md

We are implementing Meridian as a deterministic workflow engine on top of the existing Philip -> Fatih -> Matthew model.

Start with Epic A from docs/meridian/orchestration-implementation-backlog.md:
- build official Meridian task workflow primitives
- add task claim support
- add task transition support
- enforce valid queue transitions
- keep task files under tasks/ as canonical state
- keep workflow_state.json derived only

Constraints:
- do not replace the file-based queue model
- do not rely on raw file moves as the primary workflow API
- keep the implementation deterministic and testable
- add or update tests for each behavior you introduce
- preserve existing Meridian dispatcher behavior unless you are explicitly replacing it with a tested structured version

Deliver:
- code changes
- tests
- a short summary of the transition model you implemented
- any follow-up gaps that should be handled in Epic B
```

## Optional Narrower Prompt

If you want the next agent to focus only on the first implementation slice:

```text
Implement Epic A from docs/meridian/orchestration-implementation-backlog.md.

Read:
- docs/meridian/orchestration-rfc.md
- docs/meridian/orchestration-implementation-backlog.md
- hermes_cli/meridian_dispatcher.py

Build official Meridian workflow primitives for task claim and task transition.

Requirements:
- tasks/ remains canonical
- workflow_state.json remains derived
- transitions are validated
- frontmatter status stays in sync with queue directory
- transitions are auditable
- add focused tests

Do not start planner or actuation work yet unless needed to support the transition primitives cleanly.
```
