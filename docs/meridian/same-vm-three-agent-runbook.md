# Meridian Same-VM Three-Agent Runbook

This is the recommended Meridian deployment shape for a split deployment where Hermes/LLM loops run on one strong host such as `llmsrv` and the actual Meridian checkout may live on a separate project machine over SSH.

## Goal

Run three independent Hermes agents on the same VM:

- `meridian-philip` for PM and backlog orchestration
- `meridian-fatih` for implementation
- `meridian-matthew` for review, architecture, and security triage

Keep **one** Hermes gateway service for Telegram and cron delivery.

In the current Meridian setup this usually means:

- `106` or `llmsrv`: Hermes gateway, cron, and the Philip/Fatih/Matthew loops
- `107`: the real Meridian checkout and git state

## Why this shape

The old setup packed Philip, Fatih, and Matthew into one long-running prompt.
That makes review starvation likely:

- one role can monopolize the loop
- "do review when idle" is weaker than "your only job is review"
- dirty local state can block the whole orchestration path

Separate agents on the same VM fix the coordination problem without adding the cost and operational weight of separate VMs.

## Architecture

### Shared

- one Meridian coordination workspace
- one LLM server / model endpoint
- one Telegram gateway service

### Isolated

- one Hermes profile per role
- one headless loop per role
- one log file per role
- independent memory/session state per role

### Important topology note

If the real Meridian repo lives on another machine such as `107`, the role profiles should use an SSH terminal backend and point at that machine deliberately.
Do not assume the repo exists on `106` just because Hermes is running there.

## Profiles

Use these profile names:

- `meridian-philip`
- `meridian-fatih`
- `meridian-matthew`

They should be created with `--clone` from the working default profile so they inherit model and terminal backend settings without sharing session history.

## Gateway and cron ownership

Keep Telegram and cron on the **default** Hermes profile unless you intentionally move them.

Reason:

- Hermes token locks prevent multiple profiles from using the same Telegram bot token
- cron delivery lives inside the gateway process
- the role loops do not need their own gateways

So the operating split is:

- default profile: Telegram gateway + daily report cron + human-facing chat
- role profiles: headless work loops only

## Role contracts

### Philip

- owns the `customer_support/` inbox for Meridian-related Telegram asks
- backlog grooming
- scope tightening
- prioritization
- promotion into `tasks/ready`
- human-facing planning summaries
- UI/UX walkthroughs
- GIS-aware product thinking

Philip must not implement or approve code.

### Fatih

- only claims from `tasks/ready`
- implements
- verifies
- creates task-related commits
- hands off into `tasks/review`

Fatih must not self-approve.
Fatih should be the default code-writing persona.

### Matthew

- only reviews `tasks/review` first
- approves or requests changes explicitly
- creates debt/investigation tasks with evidence
- performs read-only architecture/security patrol only when review is quiet

Matthew must not silently stall on missing pushes or vague handoffs; he should send work back explicitly.

## Customer Support Inbox

Use a top-level `customer_support/` mailbox in the Meridian workspace:

```text
customer_support/
  inbox/
  responded/
  summaries/
```

Recommended behavior:

- the default Hermes Telegram layer records Meridian-related async requests into `customer_support/inbox/`
- Philip checks this inbox during his sweep, adds a durable response/update, and routes any real delivery work into `tasks/`
- a daily cron summary may send one Telegram update covering support-ticket movement and notable waiting items

Treat this as a human mailbox, not as another delivery queue.

## Work Windows

Default role windows in [`scripts/meridian-role-loop.sh`](/Users/umut/Projects/hermes-agent/scripts/meridian-role-loop.sh):

- Philip: `20:00-01:00`
- Fatih: `09:30-18:30`
- Matthew: `22:00-06:00`

Override per role with environment variables:

```bash
export HERMES_MERIDIAN_TIMEZONE=Europe/Madrid
export HERMES_MERIDIAN_WINDOW_PHILIP=19:00-00:30
export HERMES_MERIDIAN_WINDOW_FATIH=10:00-18:00
export HERMES_MERIDIAN_WINDOW_MATTHEW=22:30-06:30
```

Behavior:

- inside the window: work slowly and deliberately
- outside the window: do not start new work
- if the window closes mid-task: wrap the bounded task, leave notes, and stop

## Scripts

Use:

- [`scripts/meridian-role-loop.sh`](/Users/umut/Projects/hermes-agent/scripts/meridian-role-loop.sh)
- [`scripts/meridian-multi-agent.sh`](/Users/umut/Projects/hermes-agent/scripts/meridian-multi-agent.sh)

### Setup profiles

```bash
cd ~/Hermes-Agent
scripts/meridian-multi-agent.sh setup-profiles
```

### Start all three loops

```bash
cd ~/Hermes-Agent
scripts/meridian-multi-agent.sh start
```

### Check status

```bash
cd ~/Hermes-Agent
scripts/meridian-multi-agent.sh status
```

### Restart one role

```bash
cd ~/Hermes-Agent
scripts/meridian-multi-agent.sh restart matthew
```

### Stop all loops

```bash
cd ~/Hermes-Agent
scripts/meridian-multi-agent.sh stop
```

## Update procedure

When Hermes code changes, restart:

1. the gateway service
2. the three role loops

Recommended sequence:

```bash
cd ~/Hermes-Agent
git pull
source venv/bin/activate
sudo systemctl restart hermes-gateway.service
scripts/meridian-multi-agent.sh restart
```

Why both:

- gateway carries Telegram + cron
- loops are plain background shell processes and otherwise keep running old code/state

## Daily report expectation

The daily Meridian report should stay as a cron job on the default profile and deliver through Telegram.

The role loops do not replace that cron. They are there to keep backlog, implementation, and review moving between reports.

## Repo Safety

This is the subtle part.

If Philip, Fatih, and Matthew all point at the same live git checkout on `107`, parallel code editing is unsafe.
You will get branch confusion, untracked local-state coupling, and review noise.

Current safe rule set:

- Philip stays read-heavy and edits planning/task/support artifacts, not production code
- Matthew stays read-heavy and edits review/debt artifacts, not production code
- Fatih is the main code-writing persona

If you later want true parallel code writing, do not keep everyone on one live checkout.
Move to:

- a shared control plane for `tasks/` and `customer_support/`
- isolated code worktrees or branches per writing persona on the project machine

## Operational notes

- If Telegram stops responding, inspect and restart `hermes-gateway.service`.
- If Matthew "is not reviewing", check `scripts/meridian-multi-agent.sh status` and the Matthew loop log first.
- If Fatih or Matthew are blocked by a dirty workspace, fix the workflow contract rather than folding the role back into Philip.
