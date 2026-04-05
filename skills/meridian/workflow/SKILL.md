---
name: meridian-workflow
description: Use when a user asks Hermes directly to handle Meridian work. Route requests through the Philip -> Fatih -> Matthew workflow using task-state handoff, not polling.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [meridian, workflow, orchestration, philip, fatih, matthew]
    related_skills: [meridian-philip, meridian-fatih, meridian-matthew]
---

# Meridian Workflow

Use this when the user talks to Hermes directly about Meridian work, for example:
- "Implement this feature in Meridian"
- "Handle this in Meridian through the Philip/Fatih/Matthew workflow"
- "Handle this in Meridian"

## Goal

Coordinate Meridian work through a lightweight workflow:
1. Philip does intake and task shaping.
2. Fatih implements only when a task is ready.
3. Matthew reviews and decides whether to merge, request changes, or escalate.

This is **event-driven**, not polling-driven.
Do not create hourly loops for immediate work.

## Core Rules

- New user work enters through **Philip first** by default.
- Treat Philip as the default human-facing Meridian persona.
- Use **sequential handoff**, not parallel delegation.
- Only wake the next persona when the task state requires it.
- Prefer the existing Meridian task system over ad-hoc status tracking.
- Use official Meridian workflow primitives for queue changes:
  - `task_claim` for explicit ownership
  - `task_transition` for queue/state changes
- Do not treat raw file moves as the primary workflow API.
- If no meaningful next action exists, stop and report status instead of looping.

## Persona Loading

Before delegating to a persona, load that persona's skill:
- `skill_view(name="meridian-philip")`
- `skill_view(name="meridian-fatih")`
- `skill_view(name="meridian-matthew")`

Pass the relevant persona instructions into `delegate_task` as context so the child agent has everything it needs.

## Event-Driven Handoff Policy

### 1. Intake

When the user gives a new Meridian request:
- route it to Philip first
- Philip should create or refine the task
- Philip may promote a task to `ready/` only when it is decision-complete via `task_transition`

### 2. Implementation

Wake Fatih only when:
- a task exists in `tasks/ready/`, and
- there is no more urgent unfinished Matthew/Fatih feedback loop to resolve

Fatih should not start unrelated work while a review/request-changes loop is active.
Fatih should claim work explicitly before `ready -> in_progress`.
Fatih should create meaningful task-related commits before handing work to review.

### 3. Review

Wake Matthew when:
- a task reaches `tasks/review/`, or
- the user explicitly asks for architectural/security review

If Matthew requests changes:
- route the work back to Fatih
- do not start unrelated new implementation

If Matthew approves:
- merge only when the work is low-risk and within the contextual merge policy
- otherwise transition the task to `waiting_human`

## Priority of Work

When deciding who to wake next, prefer:
1. active review / request-changes loops
2. ready tasks already prepared by Philip
3. new intake from the current user request

Do not keep taking new work if an existing Fatih <-> Matthew loop needs completion.

Use the dispatcher/reconcile outputs as the orchestration source of truth for derived workflow state.

## Night Patrol

Night patrol is separate from the immediate work pipeline.

- Do **not** start night patrol during a direct user-request workflow unless the user explicitly asks for it.
- If Fatih still has active work, let him finish it but avoid assigning him unrelated new work.
- If Fatih is idle, Philip and Matthew may do read-heavy scans and report findings.
- Matthew should use patrol time for architecture/security/codebase review and produce concrete follow-up work instead of silent reshaping.
- Philip should use patrol time for backlog shaping, feature framing, and implementation read-through.

## Efficiency Rules

- No polling loops for immediate work.
- No recursive orchestration.
- No "check every hour" behavior unless the user is explicitly configuring patrols.
- Keep delegation linear and minimal to reduce compute overhead.

## Desired Outcome

For a direct Meridian request, Hermes should behave like a lightweight coordinator:
- organize the work
- wake the next persona only when needed
- stop cleanly when the next state is "waiting"
