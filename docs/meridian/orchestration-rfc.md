# Meridian Orchestration RFC

## Status

Draft

## Summary

This RFC defines how Meridian should evolve from a file-based workflow with persona prompts into a real event-driven orchestration system.

Today we already have:
- queue directories as a source of truth under `tasks/`
- persona definitions for Philip, Fatih, and Matthew
- a lightweight dispatcher that computes queue status and suggests who should wake next

What we do not yet have is the workflow engine itself.

This RFC proposes:
- deterministic queue transitions
- a real orchestration loop with leases and idempotent wakeups
- explicit separation between planning work and delivery work
- LLM personas acting as scoped workers, not as the workflow engine

## Problem

The current Meridian design is coherent at the prompt and docs level, but incomplete at runtime.

Current gaps:
- queue transitions are mostly left to persona behavior
- the dispatcher does not actually wake workers or execute transitions
- there is no official task claim or lease model
- there is no stale-task handling
- there is no ready-queue replenishment policy
- a single stale `in_progress` task can starve Philip's backlog grooming lane
- event-driven orchestration is implied, but the runtime behaves more like manual status inspection plus optional cron

This creates an unstable operating model:
- the system can describe the workflow correctly
- but it cannot reliably run the workflow end to end

## Goals

- Keep `tasks/` as the human-readable source of truth.
- Preserve the Philip -> Fatih -> Matthew operating model.
- Make queue transitions deterministic and auditable.
- Make wakeups idempotent and safe under retries.
- Prevent starvation when `ready/` is empty but `backlog/` contains promotable tasks.
- Support event-driven orchestration with reconcile fallback.
- Let humans inspect and override the system easily.

## Non-Goals

- Replacing the file-based queue model with a database-first system
- Turning Meridian into a fully autonomous background swarm
- Removing human approval for risky merges or ambiguous product decisions
- Solving every project-specific task quality issue in the orchestration layer

## Current State

### Existing Components

- `tasks/backlog`, `ready`, `in_progress`, `review`, `done`, `debt`
- persona skills:
  - `meridian-philip`
  - `meridian-fatih`
  - `meridian-matthew`
- workflow skill:
  - `meridian-workflow`
- dispatcher:
  - `hermes_cli/meridian_dispatcher.py`

### What the Dispatcher Does Today

The current dispatcher:
- scans queue directories
- computes queue counts
- determines `active_persona`, `workflow_state`, and `waiting_on`
- records lightweight bookkeeping in `workflow_state.json`
- prints a dispatch suggestion

The current dispatcher does not:
- spawn or wake an agent
- claim a task
- move a task between queues
- enforce transition preconditions
- resolve stale work
- emit events to a worker runtime

### Why the Current Behavior Stalls

The current priority order is effectively:
1. `review`
2. `in_progress`
3. `ready`
4. `backlog`

That makes sense for delivery throughput, but it creates a starvation mode:
- if `in_progress` is non-empty
- and `ready/` is empty
- Philip may never be asked to replenish `ready/`

This is the main reason the current model can feel stuck even when backlog work is promotable.

## Design Principles

### 1. Files remain canonical

Task files under `tasks/` remain the source of truth.

### 2. Engine decides scheduling, personas do scoped work

The workflow engine decides:
- what needs attention
- who should work next
- whether a wakeup is needed
- whether a queue transition is valid

The LLM persona decides:
- how to perform the scoped PM, implementation, or review work
- how to write notes, code, or review output within that assignment

### 3. Queue transitions are explicit

No component should rely on an LLM casually moving files around as an implicit side effect.

All queue transitions should go through an official transition mechanism.

### 4. Event-driven first, reconcile second

The system should react to explicit transitions and updates immediately.

It should also periodically reconcile state so missed events or manual edits do not permanently wedge the workflow.

### 5. Planning and delivery are separate lanes

Meridian has two distinct kinds of work:
- delivery lane: `ready -> in_progress -> review -> done`
- planning lane: `backlog` and `debt` grooming

Philip should not be starved just because delivery has an old `in_progress` task.

### 6. Small-context work beats giant-context work

The orchestration model should assume:
- smaller, well-shaped tasks
- fresh reading of relevant files
- explicit handoff artifacts

It should not assume one agent keeps the whole repository in memory for long stretches.
Large context windows may be available, but they are not the primary coordination mechanism.

## Canonical State Model

### Queue State

Queue membership is derived from the task file's directory:
- `tasks/backlog/`
- `tasks/in_progress/`
- `tasks/review/`

### Task Metadata

Each task file should contain enough metadata for deterministic orchestration.

Required fields:
- `id`
- `order`
- `title`
- `type`
- `priority`
- `risk`
- `created_by`
- `assigned_to`
- `reviewer`
- `acceptance_criteria`
- `updated_at`

Recommended orchestration fields:
- `status`
- `claimed_by`
- `claimed_at`
- `last_transition_at`
- `blocked_reason`
- `waiting_on`
- `review_loop_id`
- `depends_on`
- `stale_after`

### Derived Runtime State

`workflow_state.json` should remain derived and non-canonical.

It may store:
- last dispatched action
- orchestration leases
- idempotency keys
- last transition timestamps
- waiting-human markers
- stale escalation bookkeeping

It must not become the main source of task truth.

## Official Task Lifecycle

### Primary Flow

1. Philip discovers or refines work.
2. Philip creates or updates a task in `backlog/`.
3. Philip promotes a decision-complete task to `ready/`.
4. Fatih claims a ready task and moves it to `in_progress/`.
5. Fatih implements and verifies it.
6. Fatih moves it to `review/`.
7. Matthew reviews it.
8. Matthew either:
   - moves it to `done/`
   - returns it to `in_progress/`
   - escalates it to `waiting_human`
   - creates linked `debt/` or follow-up tasks

### Role Purity

- Philip owns intake, shaping, prioritization, and handoff quality.
- Fatih owns implementation and verification.
- Matthew owns review, architectural judgment, and security/risk evaluation.
- In a shared checkout, only Fatih should write production code during the normal flow.
- Parallel coding is an opt-in exception that requires disjoint ownership, not a default behavior.

### Debt Flow

1. Matthew creates a debt or investigation task in `debt/`.
2. Philip later triages it.
3. Philip promotes it to `backlog/` or `ready/` when appropriate.

## Queue Transition Contract

All task movement should use an official transition API.

### Allowed Transitions

- `backlog -> ready`
- `backlog -> debt`
- `debt -> backlog`
- `debt -> ready`
- `ready -> in_progress`
- `in_progress -> review`
- `review -> done`
- `review -> in_progress`
- `review -> waiting_human`
- `waiting_human -> in_progress`
- `waiting_human -> done`

Optional exceptional transitions:
- `ready -> backlog`
- `in_progress -> backlog`

These should require a reason such as invalid assumptions or scope reset.

### Transition Preconditions

`backlog -> ready`
- acceptance criteria are concrete
- dependencies are known or absent
- no blocking ambiguity remains
- work is implementable without guessing

`ready -> in_progress`
- task is claimed by Fatih
- no unresolved review loop has higher priority

`in_progress -> review`
- implementation notes updated
- verification passed or explicit failure rationale recorded

`review -> done`
- Matthew approved
- merge policy allows completion without human approval

`review -> in_progress`
- Matthew requested concrete changes

`review -> waiting_human`
- architecture, migration, security, or other high-risk gate requires a human decision

## Official APIs and Tools

The workflow should expose explicit tools instead of relying on raw file moves.

### `task_claim`

Purpose:
- claim a task for a specific actor

Inputs:
- `task_id`
- `actor`
- `lease_ttl`
- `reason`

Outputs:
- success
- current owner
- claim expiration

### `task_transition`

Purpose:
- move a task between workflow states atomically

Inputs:
- `task_id`
- `actor`
- `from_queue`
- `to_queue`
- `reason`
- `notes`
- optional metadata patch

Effects:
- move the file
- update frontmatter status
- update timestamps
- append transition log
- emit a task-transition event

### `task_select`

Purpose:
- deterministically select the next task for an actor

Inputs:
- `queue`
- `actor`
- selection policy

Outputs:
- selected task id
- rationale

### `task_escalate`

Purpose:
- mark a task as waiting on human or stale escalation

Inputs:
- `task_id`
- `actor`
- `reason`
- `target`

## Selection Policy

The system must stop treating queue order as enough.

Selection should consider:
- queue priority
- explicit task priority
- age
- stale status
- dependency readiness
- active review loop continuity

Recommended default policy:
- highest workflow urgency first
- then highest task priority
- then oldest eligible task

### Review Loop Continuity

If a review loop exists for a task, it should usually outrank unrelated new implementation.

That prevents context thrash and half-finished loops.

## Planning Lane vs Delivery Lane

This is the key design change.

### Delivery Lane

Owned primarily by Fatih and Matthew.

States:
- `ready`
- `in_progress`
- `review`
- `waiting_human`
- `done`

### Planning Lane

Owned primarily by Philip.

States:
- `backlog`
- `debt`

### Why Two Lanes Matter

Without lane separation, any active delivery work can block backlog grooming.

With lane separation:
- Matthew and Fatih can keep the active loop moving
- Philip can still replenish `ready/` when capacity is low

## Ready Replenishment Policy

Meridian should maintain a small buffer of ready work.

Recommended policy:
- introduce `ready_target`, default `2`
- if `ready_count < ready_target`
- and backlog contains eligible work
- schedule Philip to promote up to the deficit

Guardrails:
- do not promote vague tasks just to fill the buffer
- do not exceed a small WIP-friendly ready queue
- respect explicit project priorities and dependency order

## Event Model

### Primary Events

- `task_created`
- `task_updated`
- `task_claimed`
- `task_transitioned`
- `verification_finished`
- `review_finished`
- `human_confirmation_received`
- `stale_task_detected`

### Event Sources

Primary:
- explicit calls from workflow tools such as `task_transition`

Secondary:
- filesystem watcher on the Meridian `tasks/` tree

Fallback:
- periodic reconcile pass

### Why Reconcile Still Matters

Humans will sometimes edit task files manually.

A reconcile pass ensures the engine can recover when:
- an event is missed
- a task is moved manually
- the process restarts
- a worker crashes mid-action

## Orchestrator Responsibilities

The orchestrator should own:
- snapshot building
- eligibility checks
- selection
- idempotent wakeup planning
- worker dispatch
- lease management
- stale detection
- human-wait holds

The orchestrator should not own:
- writing product requirements
- implementing code
- reviewing code in depth

## Dispatcher V2

The current dispatcher should evolve into four internal parts.

### 1. Snapshot Builder

Responsibilities:
- scan queues
- build queue counts
- resolve active review loop
- infer waiting-human state
- detect stale claims and stale queue entries

### 2. Planner

Responsibilities:
- decide whether to wake Philip, Fatih, or Matthew
- apply lane policies
- apply ready replenishment policy
- generate explicit `planned_actions`

### 3. Actuator

Responsibilities:
- execute the planned wakeups
- attach idempotency keys
- ensure duplicate wakeups are suppressed

### 4. Lease Manager

Responsibilities:
- prevent duplicate orchestrator runs
- prevent duplicate task claims
- expire abandoned leases safely

## Idempotency and Leases

### Orchestrator Lease

Only one orchestration pass should act at a time for a workspace.

### Worker Lease

Each worker-task assignment should have:
- a task id
- an actor
- an expiration time
- a run id

If the worker crashes or disappears, the lease can expire and the task can be reconsidered.

### Idempotent Wakeups

Every wakeup should carry an idempotency key derived from:
- workspace
- actor
- task id or planning action id
- queue snapshot version

This prevents duplicate worker launches after retries or restarts.

## Stale Task Policy

The system should treat stale work as a first-class workflow concern.

### Examples

- `in_progress` task with no meaningful update for 24h or 48h
- `review` task stuck without review beyond SLA
- `waiting_human` task forgotten without reminder

### Response

For stale `in_progress`:
- mark as stale
- wake Matthew or Philip for triage
- optionally route back to `backlog` or `ready` if implementation never really started

For stale `review`:
- wake Matthew

For stale `waiting_human`:
- remind the human decision owner

## Human Confirmation Model

`waiting_human` should be an explicit workflow state, not just an inferred flag.

Use it when:
- risky merge needs approval
- product ambiguity needs a decision
- architecture direction is unresolved

A task in `waiting_human` should not disappear from orchestration; it should be visible, queryable, and remindable.

## Observability

The system should expose:
- current queue counts
- active leases
- stale tasks
- pending human confirmations
- last transitions
- last dispatched actions

CLI additions could include:
- `hermes meridian status`
- `hermes meridian dispatch`
- `hermes meridian leases`
- `hermes meridian stale`
- `hermes meridian history <task-id>`

## Failure Modes

### Duplicate wakeups

Mitigation:
- leases
- idempotency keys

### Manual file edits causing drift

Mitigation:
- reconcile pass
- official transition tool

### Stale `in_progress` starving backlog grooming

Mitigation:
- split planning and delivery lanes
- stale detection
- ready replenishment policy

### LLM makes unsafe transition

Mitigation:
- transition precondition checks in the tool layer

### Lost in-memory state after restart

Mitigation:
- keep file tasks canonical
- rebuild derived state from queues

## Testing Strategy

### Unit Tests

- snapshot building
- transition validation
- selection policy
- stale detection
- idempotency logic

### Integration Tests

- backlog ready replenishment
- active review loop continuity
- waiting-human hold and resume
- stale claim expiration
- manual file move recovered by reconcile

### End-to-End Tests

- new task enters backlog and is promoted by Philip
- ready task is claimed by Fatih and moved to review
- review task is approved by Matthew and closed
- risky review goes to waiting-human and resumes later

## Rollout Plan

### Phase 1: Transition Safety Rails

Build:
- `task_claim`
- `task_transition`
- transition validation
- transition audit trail

Outcome:
- queue moves become deterministic and auditable

### Phase 2: Dispatcher V2 Planning

Build:
- explicit `planned_actions`
- ready replenishment logic
- lane-aware scheduling
- stale detection

Outcome:
- the dispatcher becomes a real planner, not just a status printer

### Phase 3: Actuation and Leases

Build:
- worker wakeup execution
- task and orchestrator leases
- idempotency keys

Outcome:
- event-driven actions become safe and repeatable

### Phase 4: Event Ingestion

Build:
- event emission from transition tools
- optional filesystem watcher
- reconcile tick

Outcome:
- the system becomes event-driven with recovery fallback

### Phase 5: Persona Contract Tightening

Update skills so workers:
- use official workflow tools
- do not move files directly
- report explicit outcomes back to the engine

Outcome:
- personas become reliable workers on top of the engine

## Recommended First Implementation Slices

### Slice A: Workflow Tools

Own:
- task claim
- transition validation
- atomic queue transitions
- audit trail

### Slice B: Dispatcher V2

Own:
- snapshot
- planner
- ready replenishment
- stale policy

### Slice C: Worker Runtime

Own:
- wakeup execution
- idempotency keys
- leases
- retry and crash recovery

### Slice D: Skill Contract Updates

Own:
- Philip, Fatih, Matthew skill updates
- replace free-form file moves with tool-driven transitions

### Slice E: Observability and CLI

Own:
- status outputs
- history views
- stale inspection
- lease inspection

## Agent Workstream Breakdown

These are good implementation lanes for parallel agent work once the RFC is accepted.

### Worker 1: Transition Engine

Scope:
- task transition tool
- claim tool
- metadata updates
- audit logging

Likely files:
- `tools/`
- `hermes_cli/meridian_dispatcher.py`
- tests for workflow transitions

### Worker 2: Scheduling and Planner

Scope:
- queue snapshot refactor
- planned actions
- ready target policy
- stale detection

Likely files:
- `hermes_cli/meridian_dispatcher.py`
- tests for planning behavior

### Worker 3: Runtime and Wakeups

Scope:
- execution path from planned action to worker wakeup
- idempotency and lease management
- reconcile hooks

Likely files:
- new Meridian runtime module
- possible integration points in agent orchestration

### Worker 4: Persona and Skill Tightening

Scope:
- Philip, Fatih, Matthew skill updates
- workflow skill updates
- docs alignment

Likely files:
- `skills/meridian/*`
- `docs/meridian/*`

## Open Questions

- Should `waiting_human` be a dedicated queue or a metadata state layered onto existing queues?
- Should `done/` mean approved only, or approved and merged?
- Should Philip be allowed to promote from `debt/` directly to `ready/`?
- How should dependency chains between tasks be represented and enforced?
- Should verify output be stored inside the task file, a sidecar log, or both?
- What is the correct stale timeout per queue for Meridian's actual operating cadence?

## Recommendation

Adopt the RFC with one strong rule:

Meridian orchestration should become deterministic in scheduling and transitions, while remaining LLM-assisted in execution and judgment.

That preserves the value of Philip, Fatih, and Matthew as operating personas without making the workflow engine itself depend on prompt luck.
