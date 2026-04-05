# Meridian Skills — Deploy Guide

Deploy the skills in this directory to Hermes on machine 106:

```bash
# Run from your Mac
SKILLS_DIR="/Users/umut/Projects/hermes-agent/optional-skills/meridian"
REMOTE="umut@192.168.1.106"
REMOTE_DIR="~/.hermes/skills/meridian"

ssh $REMOTE "mkdir -p $REMOTE_DIR/pm $REMOTE_DIR/developer $REMOTE_DIR/reviewer"

scp $SKILLS_DIR/pm/SKILL.md        $REMOTE:$REMOTE_DIR/pm/SKILL.md
scp $SKILLS_DIR/developer/SKILL.md $REMOTE:$REMOTE_DIR/developer/SKILL.md
scp $SKILLS_DIR/reviewer/SKILL.md  $REMOTE:$REMOTE_DIR/reviewer/SKILL.md

echo "Skills deployed."
```

## Skill Triggers

| Skill | Persona | Trigger phrases |
|---|---|---|
| pm/SKILL.md | Philip | "act as Philip", "walk through meridian", "open a task" |
| developer/SKILL.md | Fatih | "act as Fatih", "act as developer", "pick up a task" |
| reviewer/SKILL.md | Matthew | "act as Matthew", "review the PR", "review the code" |

## Deploy Meridian Docs to 107

```bash
MERIDIAN_DOCS="/Users/umut/Projects/hermes-agent/docs/meridian"
REMOTE107="umut@192.168.1.107"
MERIDIAN_PROJ="/home/umut/meridian/docs/llm"

ssh $REMOTE107 "mkdir -p $MERIDIAN_PROJ"
scp $MERIDIAN_DOCS/agentic-workflow.md $REMOTE107:$MERIDIAN_PROJ/agentic-workflow.md
scp $MERIDIAN_DOCS/agent-prompts.md    $REMOTE107:$MERIDIAN_PROJ/agent-prompts.md
```

## Cron Jobs Setup

After deploying the skills, register the cron jobs on machine 106:

```bash
# Copy setup script to 106 and run it
scp $SKILLS_DIR/setup-cron-jobs.sh $REMOTE:~/Hermes-Agent/optional-skills/meridian/setup-cron-jobs.sh
ssh $REMOTE "cd ~/Hermes-Agent && bash optional-skills/meridian/setup-cron-jobs.sh"
```
