# Meridian Control Plane And Repo Safety

This is the operating model for the current split setup:

- Hermes loops and gateway run on the LLM machine
- the real Meridian project checkout lives on the project machine

For the current deployment that means:

- `106`: Hermes, Telegram gateway, cron, Philip/Fatih/Matthew loops
- `107`: the live Meridian project checkout and git state

## Why this needs a control plane

If three personas all point at one live project checkout, two different concerns get mixed together:

- human/task coordination
- code editing and review

That is where "bam gum" branchless edits become dangerous.

The core issue is not only git conflict. It is also role confusion:

- Philip may touch code while grooming
- Matthew may patch instead of review
- Fatih may start new work while an old request-changes loop is still open

The fix is to make the coordination layer explicit.

## Shared Control Plane

Use one shared Meridian coordination workspace that contains:

```text
docs/llm/
tasks/
customer_support/
```

Recommended mailbox layout:

```text
customer_support/
  inbox/
  responded/
  summaries/
```

Ticket convention:

- each support file carries a numeric `ticket_id`
- the `target_role` field identifies the expected owner
- Telegram follow-ups can append new human instructions to the same ticket by `ticket_id`

Purpose:

- `tasks/`: delivery workflow source of truth
- `customer_support/`: async human inbox for Meridian-related Telegram asks
- `docs/llm/`: durable context the personas can reference

This control plane can live in the Meridian repo for now, but that means code and coordination still share one git surface.
That is acceptable only if code writing remains tightly constrained.

## Role Contract

### Philip

- owns backlog orchestration
- owns `customer_support/`
- does UI/UX and GIS-aware product analysis
- writes planning and queue artifacts
- does not write production code

### Fatih

- owns implementation
- takes only clear `tasks/ready/`
- addresses Matthew request-changes loops before new work
- is the main code-writing persona
- should sleep outside his implementation window

### Matthew

- owns review, architecture, and security patrol
- creates `tech_debt` or investigation tasks when evidence exists
- writes review output and debt artifacts
- does not turn into an implementation persona
- researches best practices and preserves what he learns as durable review heuristics
- acts as the slow, skeptical, principal-reviewer counterweight to Fatih's delivery bias

## Availability

The better model is event-driven availability, not hard wall-clock windows.

Rules:

- Philip stays available for backlog, support, and orchestration events
- Fatih stays available for real implementation events only
- Matthew stays available for review, risk, and clarification events
- when there is no meaningful event, the role should stop cleanly instead of manufacturing work
- behavioral gates matter more than time gates

## Current Safe Mode

Because the real project repo still acts as both code surface and coordination surface, the safe operating mode is:

- Philip: no code writes
- Matthew: no code writes except truly exceptional review surgery
- Fatih: code writes

This keeps the most dangerous collision surface small even before full worktree isolation exists.

## Future Safer Mode

If you want multiple personas writing code in parallel, move to this:

1. shared control plane for `tasks/`, `customer_support/`, and `docs/llm/`
2. isolated git worktrees or branches per writing persona on the project machine
3. clear merge/review contract before worktree results flow back into the integration branch

Until then, treat the project checkout as a shared live runway and keep only one active code-writing persona.
