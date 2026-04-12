# Meridian Single-Agent Runbook

This is the recommended Meridian deployment shape for the current 106/107 topology.

## Goal

Run one long-running Meridian runtime instead of three polling daemons.

- `106`: Hermes gateway and optional control scripts
- `107`: live Meridian checkout at `/home/umut/meridian`
- one Hermes profile: `meridian`
- one always-on loop
- separate review passes, not a separate review daemon

## Why this shape

The old Philip/Fatih/Matthew daemon split did not create true parallelism.

- model access was serialized
- three loops competed for one slot
- polling created context churn
- Matthew and Philip woke too often without enough throughput gain

The single-runtime model keeps the role perspectives but removes the daemon overhead.

## Runtime behavior

Priority order:

1. `tasks/review/` -> isolated Matthew review pass
2. `tasks/ready/` -> Fatih implementation pass
3. `tasks/waiting_human/` or `customer_support/inbox/` -> Philip planning pass
4. otherwise -> short idle sleep

Jira remains the primary backlog system.
`tasks/` is only the execution and review artifact system.

## Script

Use [`scripts/meridian-single-agent.sh`](/Users/umut/Projects/hermes-agent/scripts/meridian-single-agent.sh).

### Setup profile

```bash
cd ~/hermes-agent
scripts/meridian-single-agent.sh setup-profile
```

### Start runtime

```bash
cd ~/hermes-agent
export HERMES_MERIDIAN_WORKSPACE=/home/umut/meridian
scripts/meridian-single-agent.sh start
```

### Check status

```bash
cd ~/hermes-agent
scripts/meridian-single-agent.sh status
```

### Restart runtime

```bash
cd ~/hermes-agent
scripts/meridian-single-agent.sh restart
```

### Stop runtime

```bash
cd ~/hermes-agent
scripts/meridian-single-agent.sh stop
```

### Force a one-off pass

```bash
cd ~/hermes-agent
scripts/meridian-single-agent.sh run-pass review
scripts/meridian-single-agent.sh run-pass implement
scripts/meridian-single-agent.sh run-pass plan
```

## Operational notes

- The runtime must work directly against the live checkout on `107`; it should not create or rely on a mirrored repo on `106`.
- Review is separate because each `run-pass review` invocation is its own Hermes chat session.
- Philip is no longer a background daemon; planning wakes only for inbox or waiting-human work.
- If you change Hermes runtime code, restart the gateway and the single runtime.
