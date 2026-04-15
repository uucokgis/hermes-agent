# Agentic Workflow MVP

This document defines the file-based, LLM-first task system for the Meridian single-runtime workflow.

## Working Lenses

### Philip Lens
- Role: product manager and scrum/task manager
- Owns: backlog quality, feature discovery, task prioritization, acceptance criteria, product documentation updates
- Can ask Umut questions over Telegram when a product or prioritization decision is needed
- Creates or refines execution packets for features, bugs, investigations, documentation gaps, CI/CD work, and technical debt
- Does not write production code in the normal workflow

### Fatih Lens
- Role: implementation developer
- Owns: code changes, tests, PR preparation, implementation notes
- Pulls the lowest-`order` backlog task when implementation work exists
- Keeps `tasks/in_progress/` limited to one item at a time
- Creates a task branch, commits the work, and sends it to a fresh Matthew review pass by default

### Matthew Review Lens
- Role: reviewer, architect, and security triage
- Owns: code review, architecture review, regression risk detection, scanner triage
- Reviews Fatih's work before merge
- Consumes GitHub Dependabot and other security signals, then turns real findings into precise backlog updates instead of flooding the queue with raw alerts
- Consumes review evidence after implementation handoff

## Source of Truth

The canonical workflow context for agents working on Meridian lives in:
- `docs/llm/`
- `tasks/`

Jira is the primary backlog and prioritization system.
The markdown `tasks/` tree is the short-horizon execution queue that the runtime moves through locally.

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
It is not part of the delivery queue state machine; the Philip lens owns it as a support and triage inbox.

## Directory Layout

```text
tasks/
  backlog/
  in_progress/
  review/
  templates/
```

## Runtime Model

There is one Meridian execution flow for the project workspace.

- The agent works directly in the live Meridian checkout on `107`.
- Its default working posture is the implementation-first Fatih lens.
- Review is a fresh Matthew pass against the task branch and `tasks/review/`.
- Planning and intake are on-demand Philip passes triggered by `customer_support/inbox/` or explicit reprioritization needs.
- We no longer run three separate polling daemons or role profiles.

This keeps the single available model slot focused on one meaningful task at a time.

## Task Lifecycle

Default flow:

1. A Jira item or support request needs execution.
2. The Philip lens creates or refines the execution packet in `tasks/backlog/` and assigns an `order`.
3. The Fatih lens moves the task to `tasks/in_progress/`, creates a task branch, and implements the change.
4. The Fatih lens updates implementation notes, records branch and commit metadata, and moves the task to `tasks/review/`.
5. A fresh Matthew review pass runs.
6. Matthew either:
- returns it to `tasks/backlog/` with review notes and a lower `order`
- approves the work and deletes the task file
- records any non-blocking concerns directly in the review output

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

Matthew should review before push or merge unless there is an explicit emergency override. Even then, Matthew should review after the fact and create debt or follow-up tasks if needed.

## Availability Model

Meridian is event-driven inside one workflow, not three independent polling daemons.

- Review has the highest priority while `tasks/review/` is non-empty.
- Implementation continues while `tasks/in_progress/` contains one active item.
- If implementation is idle, the next lowest-`order` task from `tasks/backlog/` becomes the next candidate.
- Planning wakes for `customer_support/inbox/` or explicit reprioritization work.
- If no meaningful event exists, the workflow stops cleanly instead of keeping a polling daemon alive.

## Shared Repo Safety

If all working lenses point at one live project checkout, parallel code edits are risky.

Current safe posture:
- Philip lens: planning, support, backlog coordination, and queue artifacts
- Matthew lens: review output, debt, investigation, and queue artifacts
- Fatih lens: the primary code-writing mode

Long-term safer posture:
- shared control plane for `tasks/` and `customer_support/`
- isolated code worktrees or branches for code-writing tasks
