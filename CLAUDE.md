This is a fork of https://github.com/nousresearch/hermes-agent.

Upstream: `https://github.com/nousresearch/hermes-agent`
Fork: `https://github.com/uucokgis/hermes-agent`

Both hermes-agent and the Meridian GIS platform run on `192.168.1.106` (umut@192.168.1.106, password: figo1190). SSH access available. Development is done locally on Mac; deploy by pushing to GitHub and pulling on 106.

## Custom additions over upstream

- `skills/meridian/` — Planner, Developer, Reviewer role skills for the Meridian agentic workflow
- `hermes_cli/meridian_workflow.py` — Core workflow primitives (task state machine, branch management)
- `hermes_cli/meridian_notifier.py` — Telegram notifications for waiting/blocked tasks
- `tools/meridian_workflow_tool.py` — Tool registration for workflow operations
- `scripts/meridian-single-agent.sh` — Main entry point for the Meridian single-agent loop
- `scripts/meridian-go.sh` — Convenience wrapper

## Running on 106

```bash
ssh umut@192.168.1.106
cd Hermes-Agent
./scripts/meridian-go.sh
```

## Keeping up with upstream

```bash
git fetch upstream
git merge upstream/main
```
