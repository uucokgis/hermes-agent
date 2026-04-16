#!/bin/bash
# Meridian Multi-Agent Cron Jobs Setup
# Run on machine 106: bash setup-cron-jobs.sh
#
# Creates 3 cron jobs:
#   1. Fatih   — every 2h: check tasks/ready/, implement if work is available
#   2. Matthew — every 4h: check tasks/review/, review if 3+ tasks are queued
#   3. Philip  — daily at 09:00: backlog sweep and daily status report

set -e

HERMES_DIR="/home/umut/Hermes-Agent"
cd "$HERMES_DIR"

echo "=== Meridian Cron Jobs Setup ==="

# croniter is required for cron expressions (e.g. "0 9 * * *").
# It is listed in requirements.txt but may not be installed yet.
python3 -c "import croniter" 2>/dev/null || {
    echo "Installing croniter..."
    pip install --quiet --break-system-packages croniter
}

# Remove any existing Meridian jobs to avoid duplicates on re-run.
python3 -c "
import sys
sys.path.insert(0, '.')
from cron.jobs import list_jobs, remove_job
removed = 0
for j in list_jobs(include_disabled=True):
    if 'Meridian' in j.get('name', ''):
        remove_job(j['id'])
        removed += 1
if removed:
    print(f'Removed {removed} existing Meridian job(s).')
"

# ---------------------------------------------------------------------------
# 1. FATIH — Developer loop (every 2 hours)
#    Returns [SILENT] if tasks/ready/ is empty — no Telegram message sent
# ---------------------------------------------------------------------------
python3 -c "
import sys
sys.path.insert(0, '.')
from cron.jobs import create_job

job = create_job(
    name='Meridian — Fatih Developer Loop',
    prompt='''You are Fatih, the implementation developer for the Meridian project.

First, check the tasks/ready/ directory:
  ls /home/umut/Meridian/tasks/ready/

If ready/ is EMPTY: respond with exactly \"[SILENT]\" and nothing else.

If ready/ contains task files:
- Select the highest priority task (then oldest date if tied)
- Follow the meridian-fatih skill to implement it:
  tasks/ready/ → tasks/in_progress/ → implement → verify.sh → tasks/review/
- When done, give a short summary (branch name, verify.sh status, what changed)''',
    schedule='every 2h',
    skills=['meridian-fatih'],
    deliver='telegram',
)
print(f'Fatih job created: {job[\"id\"]}')
"

# ---------------------------------------------------------------------------
# 2. MATTHEW — Reviewer batch (every 4 hours)
#    Returns [SILENT] if fewer than 3 tasks are in review/
# ---------------------------------------------------------------------------
python3 -c "
import sys
sys.path.insert(0, '.')
from cron.jobs import create_job

job = create_job(
    name='Meridian — Matthew Reviewer Batch',
    prompt='''You are Matthew, the reviewer and architect for the Meridian project.

First, count the tasks in tasks/review/:
  ls /home/umut/Meridian/tasks/review/ | wc -l

If there are FEWER than 3 tasks: respond with exactly \"[SILENT]\" and nothing else.

If there are 3 or more tasks:
- Follow the meridian-matthew skill to review all of them in sequence
- For each task: approve → tasks/done/ or request-changes → tasks/backlog/
- Write any new debt tasks to tasks/debt/ as you find issues
- When done, give a batch summary: how many approved, how many returned, how many debt tasks created''',
    schedule='every 4h',
    skills=['meridian-matthew'],
    deliver='telegram',
)
print(f'Matthew job created: {job[\"id\"]}')
"

# ---------------------------------------------------------------------------
# 3. PHILIP — Morning sweep (every day at 09:00)
#    Reviews backlog, promotes tasks to ready if criteria are met
#    Returns [SILENT] if there is nothing to act on
# ---------------------------------------------------------------------------
python3 -c "
import sys
sys.path.insert(0, '.')
from cron.jobs import create_job

job = create_job(
    name='Meridian — Philip Morning Sweep',
    prompt='''You are Philip, the PM and task manager for the Meridian project.

Do the following:
1. Read the current tasks in tasks/backlog/ and tasks/ready/
2. If ready/ is empty and backlog/ contains tasks with clear scope (high or medium priority):
   - Move 1-2 of the best-specified tasks to tasks/ready/
3. Send a short daily status report:
   - How many tasks are in backlog, ready, in_progress, review, done
   - What is the top priority item for today

If there is nothing to act on (all queues are empty or all criteria are already satisfied):
respond with exactly \"[SILENT]\" and nothing else.''',
    schedule='0 9 * * *',
    skills=['meridian-philip'],
    deliver='telegram',
)
print(f'Philip job created: {job[\"id\"]}')
"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Active cron jobs:"
python3 -c "
import sys
sys.path.insert(0, '.')
from cron.jobs import list_jobs
jobs = list_jobs()
for j in jobs:
    print(f'  [{j[\"id\"]}] {j[\"name\"]} — {j[\"schedule_display\"]}')
"

echo ""
echo "To trigger a job manually:"
echo "  python3 -c \"import sys; sys.path.insert(0,'.'); from cron.jobs import trigger_job; trigger_job('<JOB_ID>')\""
echo ""
echo "To run a cron tick immediately:"
echo "  python3 -c \"import sys; sys.path.insert(0,'.'); from cron.scheduler import tick; tick()\""
