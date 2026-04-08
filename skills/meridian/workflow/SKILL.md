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
4. Meridian-related inbound support requests are durably captured in `customer_support/` so Philip can answer asynchronously.

This is **event-driven**, not polling-driven.
Do not create hourly loops for immediate work.

## Core Rules

- New user work enters through **Philip first** by default.
- Treat Philip as the default human-facing Meridian persona.
- If a Meridian-related request comes from Telegram or another async inbox and does not require an immediate synchronous answer, record it into `customer_support/` first so Philip can process it later.
- Use **sequential handoff** as the default. Parallel work is allowed only when file ownership and subsystem boundaries are clearly disjoint.
- Only wake the next persona when the task state requires it.
- Prefer the existing Meridian task system over ad-hoc status tracking.
- Treat `customer_support/` as a durable inbox outside the delivery queues. It is not a replacement for `tasks/`; it is the human-request mailbox Philip checks between backlog passes.
- Use official Meridian workflow primitives for queue changes:
  - `task_claim` for explicit ownership
  - `task_transition` for queue/state changes
- Do not treat raw file moves as the primary workflow API.
- If no meaningful next action exists, stop and report status instead of looping.
- Optimize for small, composable task packets over giant context-heavy assignments.

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
- if it is an async support/request-for-update style message, create or update a `customer_support/` ticket first
- Philip should create or refine the task
- Philip may promote a task to `ready/` only when it is decision-complete via `task_transition`

### 2. Implementation

Wake Fatih only when:
- a task exists in `tasks/ready/`, and
- there is no more urgent unfinished Matthew/Fatih feedback loop to resolve

Fatih should not start unrelated work while a review/request-changes loop is active.
Fatih should claim work explicitly before `ready -> in_progress`.
Fatih should create meaningful task-related commits before handing work to review.
Fatih should usually be the only persona writing production code in a shared checkout.

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

## Context Budget Policy

- Do not use very large context windows as the default operating mode for Meridian workflow.
- Prefer focused task packets plus fresh reads of the relevant files.
- Recommended local coding budget:
  - normal implementation/review loop: `32k`
  - larger cross-file work: `48k` to `64k`
  - `128k`-class context only for explicit repo exploration or synthesis passes
- Bigger context is not a substitute for clean task decomposition. If a task seems to require "the whole repo," reshape the task before increasing context.

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
- Matthew should use patrol time for architecture/security/codebase review, package risk triage, and concrete tech-debt creation instead of silent reshaping.
- Philip should use patrol time for customer-support inbox triage, UI/UX walkthroughs, GIS/product analysis, backlog shaping, feature framing, and implementation read-through.
- Role availability is event-driven. Roles should wake for real queue, review, or support events and stop cleanly when no meaningful event exists.

## Repo Safety

- If all personas point at one live project checkout, parallel code editing is unsafe.
- In that configuration, only Fatih should write production code. Philip and Matthew stay read-heavy and mostly edit planning, task, debt, and support artifacts.
- The long-term safer model is: shared control plane for `tasks/` and `customer_support/`, plus isolated code worktrees or branches per writing persona.
- If parallel implementation is needed later, split work by explicit ownership such as frontend vs backend, or by fully disjoint files. Never rely on "they will probably stay out of each other's way."

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
