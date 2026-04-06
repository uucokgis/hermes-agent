# Meridian Same-VM Three-Agent Runbook

This is the recommended Meridian deployment shape for a single strong host such as `llmsrv`.

## Goal

Run three independent Hermes agents on the same VM:

- `meridian-philip` for PM and backlog orchestration
- `meridian-fatih` for implementation
- `meridian-matthew` for review, architecture, and security triage

Keep **one** Hermes gateway service for Telegram and cron delivery.

## Why this shape

The old setup packed Philip, Fatih, and Matthew into one long-running prompt.
That makes review starvation likely:

- one role can monopolize the loop
- "do review when idle" is weaker than "your only job is review"
- dirty local state can block the whole orchestration path

Separate agents on the same VM fix the coordination problem without adding the cost and operational weight of separate VMs.

## Architecture

### Shared

- one repo checkout
- one Meridian workspace
- one LLM server / model endpoint
- one Telegram gateway service

### Isolated

- one Hermes profile per role
- one headless loop per role
- one log file per role
- independent memory/session state per role

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

- backlog grooming
- scope tightening
- prioritization
- promotion into `tasks/ready`
- human-facing planning summaries

Philip must not implement or approve code.

### Fatih

- only claims from `tasks/ready`
- implements
- verifies
- creates task-related commits
- hands off into `tasks/review`

Fatih must not self-approve.

### Matthew

- only reviews `tasks/review` first
- approves or requests changes explicitly
- creates debt/investigation tasks with evidence
- performs read-only architecture/security patrol only when review is quiet

Matthew must not silently stall on missing pushes or vague handoffs; he should send work back explicitly.

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

## Operational notes

- If Telegram stops responding, inspect and restart `hermes-gateway.service`.
- If Matthew "is not reviewing", check `scripts/meridian-multi-agent.sh status` and the Matthew loop log first.
- If Fatih or Matthew are blocked by a dirty workspace, fix the workflow contract rather than folding the role back into Philip.
