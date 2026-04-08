# Meridian Review System Implementation Plan

This document is an implementation-first plan for hardening the Meridian multi-agent review pipeline.
It is written for coding agents and orchestration loops, not for human-facing product documentation.

## Current Status

Completed in Hermes:

- `tasks/in_progress` is now the canonical write target.
- Hermes reads both `tasks/in_progress` and legacy `tasks/in-progress` for workflow, support summaries, and notifier snapshots.
- Canonical `tasks/in_progress` wins when the same task exists in both locations.

This removes silent queue loss while preserving backward compatibility during migration.

## Objectives

1. Make review and quality signals agent-consumable and machine-actionable.
2. Reduce irrelevant quality noise with diff-aware lane selection.
3. Separate active review work from review artifacts and patrol output.
4. Preserve backward compatibility long enough to migrate existing repositories safely.

## Phase 1: Finish Queue Canonicalization

Goal: eliminate the legacy `tasks/in-progress` directory from active use.

### Deliverables

- Add a Hermes command or Meridian maintenance script that:
  - scans `tasks/in-progress`
  - moves files into `tasks/in_progress`
  - refuses unsafe overwrites when the same task exists in both places with divergent contents
  - emits a migration summary
- Add a sentinel `README.md` in `tasks/in-progress` that says the directory is deprecated.
- Add a doctor/status warning when legacy queue files still exist.

### Agent Rules

- Read compatibility remains enabled during migration.
- All new writes must continue targeting `tasks/in_progress`.
- If a duplicate exists in both directories, canonical `in_progress` is source of truth and the duplicate must be surfaced as drift.

### Suggested Tests

- migration moves legacy-only tasks
- migration skips identical duplicates cleanly
- migration blocks divergent duplicates
- doctor/status reports drift when legacy files remain

## Phase 2: Review Decision Envelope

Goal: turn Matthew output into structured review state, not just markdown prose.

## Core Principle

The review artifact should optimize for agent execution:

- explicit outcome
- explicit required actions
- explicit blocking severity
- explicit follow-up tasks
- explicit linkage to quality evidence

Markdown narrative remains optional and secondary.

## Canonical Artifact Shape

Every active review decision file should begin with frontmatter like:

```yaml
review_schema_version: 1
review_id: REVIEW-20260408-001
review_task_id: PHILIP-20260405-010-P4
review_kind: decision
review_outcome: request_changes
decision_bucket: blocking
reviewer: matthew
assignee: fatih
status: final
quality_gate:
  task_id: PHILIP-20260405-010-P4
  decision: review
  normalized_summary: "review=2 advisory=1"
  report_path: ~/.hermes/meridian/review_signals/PHILIP-20260405-010-P4.md
evidence_refs:
  - type: file
    path: frontend/src/store/drawingSessionStore.ts
  - type: quality_gate
    lane: frontend-lint
required_actions:
  - id: RA-1
    severity: blocking
    summary: Prevent recursive selection loop between map and table
    owner: fatih
    status: open
followup_tasks:
  - TASK-20260408-001
transition_recommendation:
  from_queue: review
  to_queue: in_progress
updated_at: 2026-04-08T01:30:00Z
```

## Required Fields

- `review_schema_version`
- `review_id`
- `review_task_id`
- `review_kind`
- `review_outcome`
- `decision_bucket`
- `reviewer`
- `status`
- `required_actions`
- `updated_at`

## Enums

`review_kind`

- `decision`
- `patrol`
- `triage`

`review_outcome`

- `approved`
- `request_changes`
- `blocked`
- `debt_only`
- `needs_human`

`decision_bucket`

- `passed`
- `advisory`
- `review`
- `blocking`

`status`

- `draft`
- `final`
- `superseded`
- `archived`

## Behavioral Rules

- `approved` should usually recommend `review -> done`
- `request_changes` should usually recommend `review -> in_progress`
- `blocked` should usually recommend `review -> waiting_human`
- `debt_only` may allow `review -> done` while emitting debt follow-ups
- `needs_human` should require a structured blocking reason

## Hermes Changes

- Add parser/validator for review envelopes.
- Add helper to load the latest final decision artifact for a task.
- Expose structured review status in gateway `/meridian_task`.
- Teach dispatcher/notifier to summarize active blocking review actions.
- Optionally reject malformed Matthew decisions in role loop post-processing.

## Suggested Tests

- parse valid decision envelope
- reject missing required fields
- choose latest final envelope when multiple exist
- map envelope outcome to transition recommendation
- gateway task detail renders structured decision summary

## Phase 3: Review Queue Refactor

Goal: separate active queue state from review byproducts.

## Target Layout

```text
tasks/
  review/
    active/
    decisions/
    patrol/
    archive/
```

## Directory Semantics

- `review/active/`: live tasks awaiting Matthew action
- `review/decisions/`: structured review decision envelopes
- `review/patrol/`: architecture/security patrol outputs and scanner triage summaries
- `review/archive/`: superseded or closed review artifacts

## Transition Rules

- Fatih handoff goes only to `review/active/`
- Matthew decision files go only to `review/decisions/`
- Night patrol outputs go only to `review/patrol/`
- Once a review loop is resolved, stale decision artifacts move to `review/archive/`

## Backward-Compatible Rollout

Step 1:

- keep reading old flat `tasks/review/`
- classify files by envelope frontmatter or filename heuristics

Step 2:

- start writing new artifacts into split subdirectories

Step 3:

- add migration script to relocate old review artifacts

Step 4:

- stop reading flat `tasks/review/` once repository is clean

## Classification Rules for Migration

Prefer frontmatter when present:

- `review_kind: decision` -> `review/decisions/`
- `review_kind: patrol` -> `review/patrol/`
- `status: archived|superseded` -> `review/archive/`

Fallback filename heuristics:

- names containing `APPROVAL`, `REQUEST-CHANGES`, `DECISION`, `REVIEW` -> `review/decisions/`
- names containing `PATROL` -> `review/patrol/`
- names representing implementation handoff tasks -> `review/active/`

## Hermes Changes

- Update review candidate discovery to read `review/active/` first.
- Prevent quality gate from treating `review/decisions/` and `review/patrol/` files as active review tasks.
- Update gateway prompts and support summaries to only count `review/active/`.
- Add archival helper for superseded decision envelopes.

## Suggested Tests

- only `review/active/` contributes to active review queue counts
- decision artifacts no longer appear as reviewable implementation tasks
- patrol artifacts are excluded from active dispatch
- flat legacy review directory still works during migration

## Phase 4: Diff-Aware Quality Gate

Goal: run only the lanes that matter for the task under review.

## Scope Derivation Order

1. task frontmatter `linked_files`
2. task frontmatter `linked_dirs`
3. associated branch diff against merge base
4. fallback to repo-wide scan only when scope cannot be derived safely

## Proposed Task Metadata Additions

```yaml
linked_files:
  - frontend/src/store/drawingSessionStore.ts
  - frontend/src/components/Map/MapToolbar.tsx
linked_dirs:
  - frontend/src/store
pr_branch: task/drawing-session-fix
quality_scope:
  mode: diff
```

## Lane Selection Policy

- backend-only diff:
  - `backend-quality`
  - `backend-security`
- frontend-only diff:
  - `frontend-lint`
  - `frontend-build`
  - `frontend-security`
- mixed diff:
  - all impacted backend and frontend lanes
- docs/tasks-only diff:
  - skip heavy lanes, emit `skipped` with rationale

## Optional File-Scoped Enhancements

- run `pytest` only for impacted backend test folders when scope is narrow enough
- run eslint on changed frontend paths before falling back to full lint
- keep full build for frontend changes unless a lighter confidence-preserving rule is agreed

## Safety Rules

- If diff resolution fails, do not silently skip review; fall back to broader scan.
- Persist the derived scope in the quality report so Matthew can inspect why a lane ran or did not run.
- Quality gate remains an evidence source, not an autonomous merge decider.

## Suggested Tests

- frontend-only task does not trigger backend lanes
- docs-only task yields skipped result with rationale
- linked_files override branch diff
- failed diff resolution falls back to full scan

## Recommended Execution Order

1. queue migration tooling
2. review decision envelope parser and schema
3. review queue split with backward-compatible readers
4. diff-aware quality scope derivation
5. optional stricter policy enforcement in Matthew loop

## Definition of Done

The system is considered upgraded when:

- there is only one active implementation queue for in-progress work
- active review tasks are isolated from review artifacts
- Matthew decisions are machine-readable and drive queue transitions
- quality reports are scoped to the actual task diff by default
- gateway task detail can show both latest quality result and latest structured review decision
