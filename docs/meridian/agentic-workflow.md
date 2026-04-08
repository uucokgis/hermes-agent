# Agentic Workflow MVP

This document defines the file-based, LLM-first task system for the Meridian multi-agent workflow.

## Agents

### Philip
- Role: product manager and scrum/task manager
- Owns: backlog quality, feature discovery, task prioritization, acceptance criteria, product documentation updates
- Can ask Umut questions over Telegram when a product or prioritization decision is needed
- Creates tasks for features, bugs, investigations, documentation gaps, CI/CD work, and technical debt

### Fatih
- Role: implementation developer
- Owns: code changes, tests, PR preparation, implementation notes
- Pulls only tasks that are ready and well-scoped
- Sends completed work to Matthew for review by default

### Matthew
- Role: reviewer, architect, and security triage
- Owns: code review, architecture review, regression risk detection, scanner triage, technical debt creation
- Reviews Fatih's work before merge
- Consumes GitHub Dependabot and other security signals, then turns real findings into tasks instead of flooding the backlog with raw alerts
- Consumes Hermes-generated review-signal reports when a task enters `review/`

## Source of Truth

The canonical workflow context for agents working on Meridian lives in:
- `docs/llm/`
- `tasks/`

We are not using a single `tasks.json` file as the source of truth.

Task-per-file is a better fit because it:
- keeps Git diffs small and readable
- reduces merge conflicts between agents
- lets LLMs focus on one task at a time
- is easy to inspect manually
- can later be indexed into a UI or SQLite cache without changing the source format

In addition to the delivery queues, Meridian may maintain a separate human-request inbox:

```text
customer_support/
  inbox/
  responded/
  summaries/
```

This mailbox is for Meridian-related inbound Telegram or async user requests.
It is not part of the delivery queue state machine; Philip owns it as a support and triage inbox.

## Directory Layout

```text
tasks/
  backlog/
  ready/
  in_progress/
  review/
  done/
  debt/
  templates/
```

Directory meaning:
- `backlog/`: newly created or not yet prioritized work
- `ready/`: work that can be picked up immediately by Fatih
- `in_progress/`: active implementation or investigation
- `review/`: work awaiting Matthew's review or security triage
- `done/`: completed and accepted work
- `debt/`: technical debt, security debt, architecture debt, or future cleanup items

## Task Lifecycle

Default flow:

1. Philip discovers or refines work.
2. Philip creates a task in `tasks/backlog/`.
3. Philip moves a clear task to `tasks/ready/`.
4. Fatih moves the task to `tasks/in_progress/` when implementation starts.
5. Fatih updates implementation notes and moves it to `tasks/review/`.
6. Hermes may trigger backend/frontend quality and security scans and attach the output as review evidence.
7. Matthew reviews it.
8. Matthew either:
- returns it to `tasks/in_progress/` with review notes
- moves it to `tasks/done/`
- creates linked debt or follow-up tasks if needed

Debt flow:

1. Matthew detects a risk, architectural issue, or vulnerability.
2. Matthew verifies there is enough evidence to avoid low-signal noise.
3. Matthew creates a task in `tasks/debt/`.
4. Philip later promotes it to `tasks/backlog/` or `tasks/ready/` based on priority.

## Task Types

Supported values for `type`:
- `feature`
- `bug`
- `investigation`
- `tech_debt`
- `security`
- `documentation`
- `architecture`
- `ci_cd`

## Required Task Quality Bar

Every task must include:
- a stable `id`
- a clear title
- a concrete `type`
- evidence or rationale
- impacted component or files when known
- a risk level
- acceptance criteria or completion conditions

Agents must not create vague tasks based only on intuition.

## Security Triage Rules

Matthew is responsible for triaging security signals, including Dependabot alerts.

Dependabot findings should become one of:
- `security` if immediate fix is needed
- `tech_debt` if real but can wait
- `investigation` if severity or exploitability is unclear

Dependabot findings should not become tasks when:
- they are already tracked elsewhere
- the package is unused or unreachable in runtime
- the alert is clearly not applicable to this project

Each security task should capture:
- affected package and version
- source of the signal, for example Dependabot
- severity
- exploitability or runtime exposure notes
- recommended action

## Telegram Usage

Philip should proactively ask Umut questions only when:
- feature intent is unclear
- acceptance criteria are missing
- multiple product directions are valid
- priority conflicts need a human decision

Telegram should not be used for questions that can be answered from code, tests, docs, or recent task history.

Meridian-related inbound Telegram requests that do not need an immediate synchronous answer should be written into `customer_support/` first so Philip can review them asynchronously and prepare a durable response/update.

## Review Policy

Default rule: Fatih does not self-approve.

Matthew should review before merge unless there is an explicit emergency override. Even then, Matthew should review after the fact and create debt or follow-up tasks if needed.

## Availability Model

Meridian should be event-driven, not wall-clock-driven.

- Philip stays available for backlog, orchestration, and support events.
- Fatih acts only when ready work or a review/request-changes loop exists.
- Matthew acts when review, risk, support-clarification, or architecture patrol events exist.
- If no meaningful event exists, the role should stop cleanly instead of manufacturing work.

## Shared Repo Safety

If all personas point at one live project checkout, parallel code edits are risky.

Current safe posture:
- Philip: planning, support, backlog, and queue artifacts
- Matthew: review output, debt, investigation, and queue artifacts
- Fatih: the primary code-writing persona

Long-term safer posture:
- shared control plane for `tasks/` and `customer_support/`
- isolated code worktrees or branches for code-writing personas
