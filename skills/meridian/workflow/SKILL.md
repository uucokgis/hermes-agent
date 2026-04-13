---
name: meridian-workflow
description: Use when a user asks Hermes directly to handle Meridian work. One agent takes the task from shaping through branch, implementation, commit, review, push, and merge.
version: 2.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [meridian, workflow, orchestration, single-agent]
    related_skills: [meridian-planner, meridian-developer, meridian-reviewer]
---

# Meridian Workflow

Use this when the user talks to Hermes directly about Meridian work, for example:
- "Implement this feature in Meridian"
- "Handle this in Meridian"
- "Review this Meridian change"

## Goal

Run Meridian work end-to-end inside one agent session:
1. Shape the task with Planner-style product clarity when needed.
2. Implement with Developer-style execution discipline.
3. Re-read the diff with Reviewer-style eyes before push or merge.
4. Keep support and waiting-human inputs durable in `customer_support/` or `tasks/` when async follow-up is needed.

This is linear and deterministic, not polling-driven.

## Core Rules

- New user work enters the single agent first; use the Planner lens only to clarify scope and acceptance criteria.
- If a Meridian-related request comes from Telegram or another async inbox and does not require an immediate synchronous answer, record it into `customer_support/` first so the Planner lens can process it later.
- Work one task at a time in one branch unless the user explicitly asks for a broader release train.
- Prefer the existing Meridian task system over ad-hoc status tracking.
- Treat `customer_support/` as a durable inbox outside the delivery queues.
- Use official Meridian workflow primitives for queue changes when available.
- Create or switch to a task branch before production edits when repo policy allows it.
- Make at least one task-scoped commit before review.
- Run a fresh self-review pass after implementation and before push/merge.
- If no meaningful next action exists, stop and report status instead of looping.
- Do not assume separate always-on daemons exist. There is one agent.

## Working Lenses

When you need a stronger posture for a phase, load the matching skill:
- `skill_view(name="meridian-planner")` — task intake, acceptance criteria, backlog
- `skill_view(name="meridian-developer")` — implementation, commits, verify.sh
- `skill_view(name="meridian-reviewer")` — code review, architecture, security

These are lenses for the same agent session, not separate workers.

## Execution Policy

### Intake

When the user gives a new Meridian request:
- clarify product intent, constraints, and acceptance criteria first
- if it is an async support/request-for-update style message, create or update a `customer_support/` ticket first
- create or refine the task packet
- move it to `ready/` only when it is decision-complete

### Implementation

Switch into the Developer lens only when:
- a task exists in `tasks/ready/`, and
- there is no more urgent unfinished review loop to resolve

Before coding:
- claim the task explicitly before `ready -> in_progress`
- create or switch to a task branch

During coding:
- keep changes narrow and production-safe
- commit meaningful, task-related checkpoints
- capture verification notes in the task

### Review

Switch into the Reviewer lens when:
- a task reaches `tasks/review/`, or
- the user explicitly asks for architectural/security review

During review:
- re-read the task, branch, diff, and verification evidence from a fresh reviewer posture
- decide whether the work is ready, needs changes, or requires human input
- fix only tiny review-contained issues when that is clearly safer than bouncing the task back

If the Reviewer pass requests changes:
- stay on the same task branch
- apply the fixes
- commit again
- re-run the Reviewer lens before push or merge

If the Reviewer pass approves:
- push the task branch
- merge with `main` using the repo's normal policy
- move the task to `done/` unless the workflow explicitly requires `waiting_human`

## Priority Of Work

When deciding the next step, prefer:
1. finishing the active review loop on the current task
2. ready tasks
3. waiting-human or inbox work
4. new intake from the current user request

## Repo Safety

- If all work points at one live project checkout, parallel code editing is unsafe.
- Prefer one active implementation branch at a time.
- Treat the Planner and Reviewer lenses as read-heavy mindsets unless a tiny review-contained fix is clearly lower risk.

## Efficiency Rules

- No competing polling loops for immediate work.
- No recursive orchestration.
- No "check every hour" behavior unless the user is explicitly configuring patrols.
- Keep the workflow linear: intake -> branch -> implement -> commit -> review -> push -> merge.

## Desired Outcome

For a direct Meridian request, Hermes should behave like a disciplined senior engineer:
- organize the work
- execute it in one focused branch
- review it with fresh eyes before it leaves the machine
- stop cleanly when the next state is "waiting"
