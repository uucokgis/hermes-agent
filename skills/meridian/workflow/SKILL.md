---
name: meridian-workflow
description: Use when a user asks Hermes directly to handle Meridian work. Route requests through the single Meridian runtime using Philip/Fatih/Matthew role modes and task-state handoff, not polling.
version: 1.1.0
author: Hermes Agent
metadata:
  hermes:
    tags: [meridian, workflow, orchestration, philip, fatih, matthew]
    related_skills: [meridian-philip, meridian-fatih, meridian-matthew]
---

# Meridian Workflow

Use this when the user talks to Hermes directly about Meridian work, for example:
- "Implement this feature in Meridian"
- "Handle this in Meridian"
- "Review this Meridian change"

## Goal

Coordinate Meridian work through one runtime with three role modes:
1. Philip mode handles intake and shaping only when needed.
2. Fatih mode implements only when a task is ready.
3. Matthew mode reviews in a separate session and decides whether to approve, request changes, or escalate.
4. Meridian-related inbound support requests are durably captured in `customer_support/` so Philip can answer asynchronously.

This is event-driven, not polling-driven.

## Core Rules

- New user work enters through Philip mode by default.
- Treat Philip as the default human-facing Meridian persona.
- If a Meridian-related request comes from Telegram or another async inbox and does not require an immediate synchronous answer, record it into `customer_support/` first so Philip can process it later.
- Use sequential handoff as the default. Parallel work is allowed only when file ownership and subsystem boundaries are clearly disjoint.
- Only wake the next mode when the task state requires it.
- Prefer the existing Meridian task system over ad-hoc status tracking.
- Treat `customer_support/` as a durable inbox outside the delivery queues.
- Use official Meridian workflow primitives for queue changes when available.
- If no meaningful next action exists, stop and report status instead of looping.
- Do not assume separate always-on Philip or Matthew daemons exist.

## Persona Loading

Before asking for a persona-specific pass, load that persona's skill:
- `skill_view(name="meridian-philip")`
- `skill_view(name="meridian-fatih")`
- `skill_view(name="meridian-matthew")`

## Event-Driven Handoff Policy

### Intake

When the user gives a new Meridian request:
- route it to Philip mode first
- if it is an async support/request-for-update style message, create or update a `customer_support/` ticket first
- Philip should create or refine the task
- Philip may promote a task to `ready/` only when it is decision-complete

### Implementation

Wake Fatih mode only when:
- a task exists in `tasks/ready/`, and
- there is no more urgent unfinished review loop to resolve

Fatih should claim work explicitly before `ready -> in_progress`.
Fatih should create meaningful task-related commits before handing work to review.
Fatih should usually be the only persona writing production code in a shared checkout.

### Review

Wake Matthew mode when:
- a task reaches `tasks/review/`, or
- the user explicitly asks for architectural/security review

If Matthew requests changes:
- route the work back to Fatih mode
- do not start unrelated new implementation

If Matthew approves:
- move the task to `done/` unless the workflow explicitly requires `waiting_human`

Matthew should review the recorded branch or commit first when those fields exist, keeping the review scope narrow and deterministic.
Small review-contained fixes are the exception, not the default.

## Priority Of Work

When deciding which mode to wake next, prefer:
1. active review work
2. ready tasks
3. waiting-human or inbox work
4. new intake from the current user request

## Repo Safety

- If all modes point at one live project checkout, parallel code editing is unsafe.
- In that configuration, only Fatih should write production code.
- Philip and Matthew stay read-heavy and mostly edit planning, task, debt, and support artifacts.

## Efficiency Rules

- No competing polling loops for immediate work.
- No recursive orchestration.
- No "check every hour" behavior unless the user is explicitly configuring patrols.
- Keep delegation linear and minimal to reduce compute overhead.

## Desired Outcome

For a direct Meridian request, Hermes should behave like a lightweight coordinator:
- organize the work
- wake the next mode only when needed
- stop cleanly when the next state is "waiting"
