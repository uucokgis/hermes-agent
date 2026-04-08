# Meridian Project-Machine Worktree Runbook

Use this on the machine that holds the real Meridian git checkout.

In your setup that is the project machine, not the LLM machine:

- `106`: Hermes loops, gateway, cron
- `107`: Meridian repo and git state

## Why

`git worktree` is a native git feature that lets one repository expose multiple checked-out working trees at once.

That is exactly what we need when we want:

- one shared repo history
- separate role-specific working directories
- less branch collision than "everyone edits the same checkout"

## Current Safe Rule

Right now the safest posture is still:

- Philip: planning, task, and support artifacts
- Matthew: review and debt artifacts
- Fatih: production code writer

So worktrees help most when Fatih needs clean isolation from the integration checkout.

## Bootstrap Script

Use [`scripts/meridian-worktree-bootstrap.sh`](/Users/umut/Projects/hermes-agent/scripts/meridian-worktree-bootstrap.sh) on the project machine:

```bash
cd ~/Hermes-Agent
scripts/meridian-worktree-bootstrap.sh --repo /home/umut/meridian --role fatih
```

Default branch names:

- Philip: `meridian/philip-planning`
- Fatih: `meridian/fatih-active`
- Matthew: `meridian/matthew-review`

The script creates:

- repo-local worktree directory: `<repo>/.worktrees/<role>`
- role branch if it does not already exist

## Recommended Layout

For `/home/umut/meridian`, the result looks like:

```text
/home/umut/meridian
/home/umut/meridian/.worktrees/fatih
/home/umut/meridian/.worktrees/philip
/home/umut/meridian/.worktrees/matthew
```

Suggested use:

- main checkout: shared control plane and integration view
- Fatih worktree: active implementation
- Matthew worktree: optional review reproduction or targeted audit branch
- Philip worktree: only if Philip truly needs isolated edits to planning artifacts

## Important Caveat

Worktrees reduce git collision, but they do not solve workflow confusion by themselves.

Do not interpret worktrees as permission for all three personas to write production code in parallel.
Keep the role contract:

- Philip does not code
- Matthew does not quietly patch instead of review
- Fatih is the primary implementation writer

## Pairing With Hermes

Longer-term model:

1. shared control plane under the Meridian workspace
2. isolated role worktrees on the project machine
3. Hermes role profiles point their SSH terminal `cwd` to the correct worktree when code writing is needed

That gives us better isolation without needing separate VMs per role.
