# Agentic Workflow MVP

This document defines the file-based, LLM-first task system for the Meridian single-runtime workflow.

## Role Modes

### Philip Mode
- Role: product manager and scrum/task manager
- Owns: backlog quality, feature discovery, task prioritization, acceptance criteria, product documentation updates
- Can ask Umut questions over Telegram when a product or prioritization decision is needed
- Creates or refines execution packets for features, bugs, investigations, documentation gaps, CI/CD work, and technical debt
- Does not write production code in the normal workflow

### Fatih Mode
- Role: implementation developer
- Owns: code changes, tests, PR preparation, implementation notes
- Pulls only tasks that are ready and well-scoped
- Sends completed work to a separate Matthew review session by default

### Matthew Review Mode
- Role: reviewer, architect, and security triage
- Owns: code review, architecture review, regression risk detection, scanner triage, technical debt creation
- Reviews Fatih's work before merge
- Consumes GitHub Dependabot and other security signals, then turns real findings into tasks instead of flooding the backlog with raw alerts
- Consumes review evidence after implementation handoff

## Source of Truth

The canonical workflow context for agents working on Meridian lives in:
- `docs/llm/`
- `tasks/`

Jira is the primary backlog and prioritization system.
The markdown `tasks/` tree is the execution and review artifact system that the runtime moves through locally.

We are not using a single `tasks.json` file as the source of truth.

Task-per-file is a better fit because it:
- keeps Git diffs small and readable
- reduces merge conflicts between agents
- lets LLMs focus on one task at a time
- is easy to inspect manually

In addition to the delivery queues, Meridian may maintain a separate human-request inbox:

```text
customer_support/
  inbox/
  responded/
  summaries/
```

This mailbox is for Meridian-related inbound Telegram or async user requests.
It is not part of the delivery queue state machine; Philip mode owns it as a support and triage inbox.

## Directory Layout

```text
tasks/
  backlog/
  claimed/
  debt/
  done/
  in_progress/
  orchestration/
  ready/
  review/
  templates/
  waiting_human/
```

## Runtime Model

There is one always-on Meridian runtime for the project workspace.

- The runtime works directly in the live Meridian checkout on `107`.
- Its default pass is implementation-first Fatih mode.
- Review is a separate Hermes chat invocation that runs Matthew mode against `tasks/review/`.
- Planning and intake are on-demand Philip-mode passes triggered by `tasks/waiting_human/` or `customer_support/inbox/`.
- We no longer run three separate polling daemons.

This keeps the single available model slot busy with one meaningful pass at a time instead of three competing loops.

## Task Lifecycle

Default flow:

1. A Jira item or support request needs execution.
2. Philip mode creates or refines the execution packet in `tasks/backlog/` or `tasks/ready/`.
3. Fatih mode moves the task to `tasks/in_progress/` when implementation starts.
4. Fatih mode updates implementation notes and moves it to `tasks/review/`.
5. A separate Matthew review session runs.
6. Matthew either:
- returns it to `tasks/in_progress/` with review notes
- moves it to `tasks/done/`
- creates linked debt or follow-up tasks if needed

Debt flow:

1. Matthew detects a risk, architectural issue, or vulnerability.
2. Matthew verifies there is enough evidence to avoid low-signal noise.
3. Matthew creates a task in `tasks/debt/`.
4. Philip later promotes it to Jira, `tasks/backlog/`, or `tasks/ready/` based on priority.

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

Meridian is event-driven inside one runtime, not three independent polling daemons.

- Review has the highest priority while `tasks/review/` is non-empty.
- Implementation wakes when `tasks/ready/` has actionable work.
- Planning wakes only for `tasks/waiting_human/` or `customer_support/inbox/` work.
- If no meaningful event exists, the runtime logs a short no-op and sleeps.

## Shared Repo Safety

If all role modes point at one live project checkout, parallel code edits are risky.

Current safe posture:
- Philip mode: planning, support, backlog coordination, and queue artifacts
- Matthew mode: review output, debt, investigation, and queue artifacts
- Fatih mode: the primary code-writing mode

Long-term safer posture:
- shared control plane for `tasks/` and `customer_support/`
- isolated code worktrees or branches for code-writing personas
