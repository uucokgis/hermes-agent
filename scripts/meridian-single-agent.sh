#!/usr/bin/env bash
# Meridian single-agent runtime loop.
# One task at a time: implement fully → review → close. Never switches queues mid-task.

set -euo pipefail

ACTION="${1:-status}"
MODE="${2:-}"
WORKSPACE="${HERMES_MERIDIAN_WORKSPACE:-/home/umut/meridian}"
PROFILE="${HERMES_MERIDIAN_PROFILE:-meridian}"
TIMEZONE_NAME="${HERMES_MERIDIAN_TIMEZONE:-Europe/Madrid}"
ACTIVE_SLEEP_SECONDS="${HERMES_MERIDIAN_ACTIVE_SLEEP_SECONDS:-45}"
IDLE_SLEEP_SECONDS="${HERMES_MERIDIAN_IDLE_SLEEP_SECONDS:-180}"
PASS_TIMEOUT_SECONDS="${HERMES_MERIDIAN_PASS_TIMEOUT_SECONDS:-1800}"
STARTUP_JITTER_SECONDS="${HERMES_MERIDIAN_STARTUP_JITTER_SECONDS:-10}"
SERIALIZE_MODEL_ACCESS="${HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS:-1}"
ALLOWED_TASK_IDS_RAW="${HERMES_MERIDIAN_ALLOWED_TASK_IDS:-}"
MODEL_LOCK_FILE="${HERMES_MERIDIAN_MODEL_LOCK_FILE:-$HOME/.hermes/meridian/runtime/model-provider.lock}"
STATE_DIR="${HOME}/.hermes/meridian/runtime"
PID_FILE="${STATE_DIR}/meridian.pid"
LOG_FILE="${STATE_DIR}/meridian.log"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="$ROOT_DIR/venv/bin/hermes"

mkdir -p "$STATE_DIR"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes binary not found: $HERMES_BIN" >&2
  exit 1
fi

load_optional_env_file() {
  local env_file="$1"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

load_optional_env_file "$HOME/.hermes/.env"

expand_path() {
  local raw="$1"
  if [[ "$raw" == "~" ]]; then printf '%s\n' "$HOME"; return; fi
  if [[ "$raw" == "~/"* ]]; then printf '%s/%s\n' "$HOME" "${raw#~/}"; return; fi
  printf '%s\n' "$raw"
}

role_local_clock() {
  TZ="$TIMEZONE_NAME" date '+%Y-%m-%d %H:%M:%S %Z'
}

allowed_task_pattern() {
  local raw="$ALLOWED_TASK_IDS_RAW"
  local token trimmed out=""

  [[ -z "$raw" ]] && return 0

  IFS=',' read -r -a _allowed_tokens <<< "$raw"
  for token in "${_allowed_tokens[@]}"; do
    trimmed="$(printf '%s' "$token" | tr -cd '[:alnum:]_.-')"
    [[ -z "$trimmed" ]] && continue
    if [[ -n "$out" ]]; then
      out="${out}|"
    fi
    out="${out}${trimmed}"
  done

  [[ -n "$out" ]] && printf '%s' "$out"
}

remote_exec() {
  if [[ -d "$WORKSPACE/tasks" ]]; then
    bash -lc "cd \"$WORKSPACE\" && $*"
    return $?
  fi

  local host="${HERMES_MERIDIAN_QUALITY_SSH_HOST:-${TERMINAL_SSH_HOST:-}}"
  local user="${HERMES_MERIDIAN_QUALITY_SSH_USER:-${TERMINAL_SSH_USER:-}}"
  local key="${HERMES_MERIDIAN_QUALITY_SSH_KEY:-${TERMINAL_SSH_KEY:-}}"
  local password="${HERMES_MERIDIAN_QUALITY_SSH_PASSWORD:-${TERMINAL_SSH_PASSWORD:-}}"

  if [[ -n "$key" ]]; then key="$(expand_path "$key")"; fi

  if [[ -z "$host" || -z "$user" ]]; then
    echo "remote workspace requires TERMINAL_SSH_HOST/USER or HERMES_MERIDIAN_QUALITY_SSH_HOST/USER" >&2
    return 1
  fi

  local ssh_cmd=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8)
  if [[ -n "$password" ]]; then
    sshpass -p "$password" "${ssh_cmd[@]}" \
      -o PreferredAuthentications=password \
      -o PubkeyAuthentication=no \
      "$user@$host" "cd '$WORKSPACE' && $*"
    return $?
  fi
  if [[ -n "$key" ]]; then ssh_cmd+=(-i "$key"); fi
  "${ssh_cmd[@]}" "$user@$host" "cd '$WORKSPACE' && $*"
}

ensure_workspace_access() {
  if [[ -d "$WORKSPACE/tasks" ]]; then return; fi
  if remote_exec "test -d tasks"; then return; fi
  echo "Meridian workspace is not accessible: $WORKSPACE" >&2
  exit 1
}

# Count files in a queue by frontmatter status field
task_queue_count() {
  local queue="$1"
  local status_pattern=""
  local allowed_pattern=""
  local find_expr=""
  case "$queue" in
    review)       status_pattern='^(status:[[:space:]]*(review|ready_for_review|READY_FOR_REVIEW))$' ;;
    ready)        status_pattern='^(status:[[:space:]]*ready)$' ;;
    waiting_human) status_pattern='^(status:[[:space:]]*waiting_human)$' ;;
    in_progress)  status_pattern='^(status:[[:space:]]*(in_progress|in-progress|claimed))$' ;;
  esac

  allowed_pattern="$(allowed_task_pattern)"
  if [[ -n "$allowed_pattern" ]]; then
    find_expr=" | grep -E '/(${allowed_pattern})([-.].*)?\\.md$'"
  fi

  if [[ -n "$status_pattern" ]]; then
    remote_exec "find tasks/in_progress tasks/in-progress -maxdepth 1 -type f -name '*.md' ! -name 'README.md' 2>/dev/null${find_expr} | xargs -I{} sh -c 'grep -lE \"$status_pattern\" \"\$1\" 2>/dev/null || true' sh {} 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
    return
  fi

  remote_exec "find tasks/$queue -maxdepth 1 -type f -name '*.md' ! -name 'README.md' -exec sh -c 'for f do IFS= read -r first <\"\$f\" || true; [ \"\$first\" = \"---\" ] && echo \"\$f\"; done' sh {} + 2>/dev/null${find_expr} | wc -l" 2>/dev/null | tr -dc '0-9'
}

customer_support_count() {
  remote_exec "find customer_support/inbox -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

# Single-slot rule: if a task is actively in_progress, never switch to review.
# Only check review queue when no task is in flight.
pick_mode() {
  local in_progress_count review_count ready_count waiting_count inbox_count

  # Check for a task currently in_progress/claimed — stay in implement until done.
  in_progress_count="$(task_queue_count in_progress)"
  if [[ "$in_progress_count" =~ ^[0-9]+$ ]] && (( in_progress_count > 0 )); then
    echo "implement"
    return
  fi

  # Nothing in flight — now check queues in priority order.
  review_count="$(task_queue_count review)"
  if [[ "$review_count" =~ ^[0-9]+$ ]] && (( review_count > 0 )); then
    echo "review"
    return
  fi

  ready_count="$(task_queue_count ready)"
  if [[ "$ready_count" =~ ^[0-9]+$ ]] && (( ready_count > 0 )); then
    echo "implement"
    return
  fi

  # waiting_human and inbox items are NOT auto-actionable in the always-on
  # runtime. They require an explicit human-triggered planning pass so the
  # agent does not invent new work or reopen stale threads on its own.
  waiting_count="$(task_queue_count waiting_human)"
  inbox_count="$(customer_support_count)"
  if { [[ "$waiting_count" =~ ^[0-9]+$ ]] && (( waiting_count > 0 )); } ||
     { [[ "$inbox_count"   =~ ^[0-9]+$ ]] && (( inbox_count > 0 )); }; then
    echo "idle"
    return
  fi

  echo "idle"
}

mode_max_turns() {
  case "$1" in
    implement) echo "${HERMES_MERIDIAN_IMPLEMENT_MAX_TURNS:-12}" ;;
    review)    echo "${HERMES_MERIDIAN_REVIEW_MAX_TURNS:-8}" ;;
    plan)      echo "${HERMES_MERIDIAN_PLAN_MAX_TURNS:-6}" ;;
    *) echo "Unknown mode: $1" >&2; exit 1 ;;
  esac
}

build_prompt() {
  local mode="$1"
  local local_clock
  local_clock="$(role_local_clock)"

  case "$mode" in
    implement)
      cat <<EOF
You are running the Meridian single-runtime implementation pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: implement
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this is the ONLY always-on Meridian runtime — there is no separate reviewer or planner daemon
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system; tasks/ is the execution and review artifact system
- this invocation is an isolated implementation session
- Be terse. Do the work or stop. Do not narrate long plans.
- If `HERMES_MERIDIAN_ALLOWED_TASK_IDS` is set, you may act ONLY on those task IDs.

Single-slot rules (STRICT):
- Claim and implement EXACTLY ONE task per pass. Do not pick a second task.
- If you already have a task in tasks/in_progress or tasks/in-progress, continue that task first.
- If the in_progress task is marked waiting_human or otherwise blocked, stop cleanly and do NOT pick anything else.
- Complete the task end-to-end: implement, pass verify.sh, commit, move to tasks/review.
- Do NOT stop mid-task and leave it in_progress. Finish before this pass ends.
- Do NOT check the review queue or switch to review work in this pass.
- Only pick from tasks/ready unless a review artifact explicitly sends a task back via request_changes.
- If nothing actionable is ready and nothing is in_progress, stop cleanly.
- If more than one task looks possible, prefer the existing in_progress task; otherwise stop and report ambiguity briefly.
- Keep changes narrow, production-safe, and easy to review.
- Do not do backlog shaping, support triage, or broad PM work in this pass.
- Do not read the whole repo. Read only files needed for the active task.
- Do not create new tasks, debt, investigations, or follow-up projects.
- Allowed outcomes only:
  1. task moved to review with passing verification
  2. task moved to waiting_human with a precise blocker note
  3. clean stop because nothing actionable exists

Blocker escalation rule (IMPORTANT):
If you hit an infrastructure or environment blocker that you CANNOT resolve yourself — such as:
  - missing system library (GDAL, GEOS, PROJ, etc.) not installable inside the container
  - SSL/TLS certificate issue outside your control
  - missing secret, credential, or API key you don't have access to
  - broken CI environment or test runner that is a machine-level issue
  - any other "needs human hands on the machine" problem
Then you MUST:
  1. transition the task from in_progress → waiting_human using task_transition
  2. write a clear blocker note in the task file:
     - exactly what you tried (commands, error output)
     - what is missing and why you can't install/fix it yourself
     - what the human needs to do to unblock you
  3. stop this pass cleanly — do NOT keep retrying the same failing approach
  4. do NOT pretend the task is done or move it to review with a broken verification
The human (Umut) monitors waiting_human via Telegram and will resolve blockers directly.
EOF
      ;;
    review)
      cat <<EOF
You are running the Meridian single-runtime review pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: review
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this review runs as a separate Hermes chat invocation from implementation work
- treat this pass as an isolated review session
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system; tasks/ is the execution and review artifact system
- Be terse. Review the current task only.
- If `HERMES_MERIDIAN_ALLOWED_TASK_IDS` is set, review ONLY those task IDs.

Review mode rules:
- Start with tasks/review; only treat files with frontmatter status review or ready_for_review as actionable.
- Ignore legacy summaries, patrol notes, approvals, and historical artifacts in tasks/review.
- Your job: approve low-risk review-ready work, or send it back with precise requested changes.
- Do NOT implement review fixes in the always-on runtime. If code changes are needed, send the task back.
- Do NOT create new debt, patrol, or investigation tasks from this pass unless the current reviewed task explicitly requires that artifact.
- When a task is approved, mark it done/closed. When it needs changes, keep the outcome narrowly tied to the current task.
- If no review-ready work exists, stop cleanly.
- Do not invent patrol work.
- Review only the task, branch, diff, and verification evidence. Do not wander.
- Do not create side quests. Put non-blocking notes in the review output and stop.
EOF
      ;;
    plan)
      cat <<EOF
You are running the Meridian single-runtime planning and intake pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: plan
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this runtime is not a constantly running Planner daemon
- this plan pass only wakes for waiting-human or inbox/intake work
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system
- tasks/ is only for execution packets, review notes, debt evidence, waiting_human items, and similar delivery artifacts
- Be terse. Only process explicit inbox or waiting-human work.
- If `HERMES_MERIDIAN_ALLOWED_TASK_IDS` is set, do not create or update task artifacts outside that allowlist unless the human explicitly asks.

Plan mode rules:
- Inspect customer_support/inbox and tasks/waiting_human first.
- Shape work, clarify scope, and create or update execution artifacts only when needed.
- Do not mirror the entire Jira backlog into markdown.
- This mode is for explicit human-triggered intake only; do not treat inbox or waiting_human as auto-actionable in the always-on loop.
- Do not create new tasks unless an explicit human request or inbox item requires one.
- Do not write production code.
- If there is no meaningful intake or clarification work, stop cleanly.
EOF
      ;;
    *)
      echo "Unknown mode: $mode" >&2; exit 1 ;;
  esac
}

run_with_timeout() {
  local timeout_seconds="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" "$@"
    return $?
  fi
  "$@"
}

ensure_profile() {
  if "$HERMES_BIN" profile show "$PROFILE" >/dev/null 2>&1; then
    echo "Profile exists: $PROFILE"; return
  fi
  "$HERMES_BIN" profile create "$PROFILE" --clone >/dev/null
  echo "Created profile: $PROFILE"
}

run_chat_pass() {
  local mode="$1"
  local max_turns
  max_turns="$(mode_max_turns "$mode")"

  if [[ "$SERIALIZE_MODEL_ACCESS" == "1" ]] && command -v flock >/dev/null 2>&1; then
    mkdir -p "$(dirname "$MODEL_LOCK_FILE")"
    (
      flock -w 600 9 || {
        echo "[meridian] failed to acquire model lock: $MODEL_LOCK_FILE" >&2
        exit 124
      }
      run_with_timeout "$PASS_TIMEOUT_SECONDS" \
        "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$mode")"
    ) 9>"$MODEL_LOCK_FILE"
    return $?
  fi

  run_with_timeout "$PASS_TIMEOUT_SECONDS" \
    "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$mode")"
}

status_runtime() {
  local status="stopped"
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      status="running pid=$pid"
    else
      status="stale-pid"
    fi
  fi
  echo "meridian | profile=$PROFILE | $status | workspace=$WORKSPACE | log=$LOG_FILE"
  if [[ -n "$ALLOWED_TASK_IDS_RAW" ]]; then
    echo "allowed_task_ids=$ALLOWED_TASK_IDS_RAW"
  fi
  echo "queues: in_progress=$(task_queue_count in_progress) review=$(task_queue_count review) ready=$(task_queue_count ready) waiting_human=$(task_queue_count waiting_human) inbox=$(customer_support_count)"
}

start_runtime() {
  local pid=""
  ensure_workspace_access
  ensure_profile

  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "meridian runtime already running (pid $pid)"
      return
    fi
    rm -f "$PID_FILE"
  fi

  nohup setsid env \
    HERMES_MERIDIAN_WORKSPACE="$WORKSPACE" \
    HERMES_MERIDIAN_PROFILE="$PROFILE" \
    HERMES_MERIDIAN_TIMEZONE="$TIMEZONE_NAME" \
    HERMES_MERIDIAN_ALLOWED_TASK_IDS="$ALLOWED_TASK_IDS_RAW" \
    HERMES_MERIDIAN_ACTIVE_SLEEP_SECONDS="$ACTIVE_SLEEP_SECONDS" \
    HERMES_MERIDIAN_IDLE_SLEEP_SECONDS="$IDLE_SLEEP_SECONDS" \
    HERMES_MERIDIAN_PASS_TIMEOUT_SECONDS="$PASS_TIMEOUT_SECONDS" \
    HERMES_MERIDIAN_STARTUP_JITTER_SECONDS="$STARTUP_JITTER_SECONDS" \
    HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS="$SERIALIZE_MODEL_ACCESS" \
    HERMES_MERIDIAN_MODEL_LOCK_FILE="$MODEL_LOCK_FILE" \
    bash "$0" run-loop >>"$LOG_FILE" 2>&1 &

  pid=$!
  echo "$pid" >"$PID_FILE"
  echo "Started meridian runtime (pid $pid, log $LOG_FILE)"
}

stop_runtime() {
  local pid=""
  if [[ ! -f "$PID_FILE" ]]; then
    echo "meridian runtime not running"
    return
  fi
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "Stopped meridian runtime (pid $pid)"
  else
    echo "Removed stale meridian runtime pid file"
  fi
  rm -f "$PID_FILE"
}

run_loop() {
  ensure_workspace_access
  ensure_profile

  if [[ "$STARTUP_JITTER_SECONDS" =~ ^[0-9]+$ ]] && (( STARTUP_JITTER_SECONDS > 0 )); then
    sleep $(( RANDOM % (STARTUP_JITTER_SECONDS + 1) ))
  fi

  while true; do
    local mode
    mode="$(pick_mode)"
    echo "=== $(date -Is) [meridian] profile=$PROFILE workspace=$WORKSPACE mode=$mode ==="

    if [[ "$mode" == "idle" ]]; then
      echo "[meridian] No actionable work — sleeping ${IDLE_SLEEP_SECONDS}s"
      sleep "$IDLE_SLEEP_SECONDS"
      continue
    fi

    run_chat_pass "$mode" || true
    sleep "$ACTIVE_SLEEP_SECONDS"
  done
}

run_pass_action() {
  local mode="$1"
  ensure_workspace_access
  ensure_profile
  run_chat_pass "$mode"
}

case "$ACTION" in
  setup-profile)  ensure_profile ;;
  start)          start_runtime ;;
  stop)           stop_runtime ;;
  restart)        stop_runtime; start_runtime ;;
  status)         status_runtime ;;
  run-loop)       run_loop ;;
  run-pass)
    if [[ -z "$MODE" ]]; then
      echo "Usage: $0 run-pass <implement|review|plan>" >&2; exit 1
    fi
    run_pass_action "$MODE"
    ;;
  *)
    echo "Usage: $0 <setup-profile|start|stop|restart|status|run-loop|run-pass> [implement|review|plan]" >&2
    exit 1
    ;;
esac

#!/usr/bin/env bash
# Meridian single-agent runtime loop.
# One task at a time: implement fully → review → close. Never switches queues mid-task.

set -euo pipefail

ACTION="${1:-status}"
MODE="${2:-}"
WORKSPACE="${HERMES_MERIDIAN_WORKSPACE:-/home/umut/meridian}"
PROFILE="${HERMES_MERIDIAN_PROFILE:-meridian}"
TIMEZONE_NAME="${HERMES_MERIDIAN_TIMEZONE:-Europe/Madrid}"
ACTIVE_SLEEP_SECONDS="${HERMES_MERIDIAN_ACTIVE_SLEEP_SECONDS:-45}"
IDLE_SLEEP_SECONDS="${HERMES_MERIDIAN_IDLE_SLEEP_SECONDS:-180}"
PASS_TIMEOUT_SECONDS="${HERMES_MERIDIAN_PASS_TIMEOUT_SECONDS:-1800}"
STARTUP_JITTER_SECONDS="${HERMES_MERIDIAN_STARTUP_JITTER_SECONDS:-10}"
SERIALIZE_MODEL_ACCESS="${HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS:-1}"
MODEL_LOCK_FILE="${HERMES_MERIDIAN_MODEL_LOCK_FILE:-$HOME/.hermes/meridian/runtime/model-provider.lock}"
STATE_DIR="${HOME}/.hermes/meridian/runtime"
PID_FILE="${STATE_DIR}/meridian.pid"
LOG_FILE="${STATE_DIR}/meridian.log"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="$ROOT_DIR/venv/bin/hermes"

mkdir -p "$STATE_DIR"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes binary not found: $HERMES_BIN" >&2
  exit 1
fi

load_optional_env_file() {
  local env_file="$1"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

load_optional_env_file "$HOME/.hermes/.env"

expand_path() {
  local raw="$1"
  if [[ "$raw" == "~" ]]; then printf '%s\n' "$HOME"; return; fi
  if [[ "$raw" == "~/"* ]]; then printf '%s/%s\n' "$HOME" "${raw#~/}"; return; fi
  printf '%s\n' "$raw"
}

role_local_clock() {
  TZ="$TIMEZONE_NAME" date '+%Y-%m-%d %H:%M:%S %Z'
}

render_skill_body() {
  local role="$1"
  local skill_path="$ROOT_DIR/skills/meridian/$role/SKILL.md"
  if [[ ! -f "$skill_path" ]]; then
    echo "Missing Meridian role skill: $skill_path" >&2
    exit 1
  fi
  awk '
    NR == 1 && $0 == "---" { in_frontmatter = 1; next }
    in_frontmatter && $0 == "---" { in_frontmatter = 0; next }
    !in_frontmatter { print }
  ' "$skill_path"
}

remote_exec() {
  if [[ -d "$WORKSPACE/tasks" ]]; then
    bash -lc "cd \"$WORKSPACE\" && $*"
    return $?
  fi

  local host="${HERMES_MERIDIAN_QUALITY_SSH_HOST:-${TERMINAL_SSH_HOST:-}}"
  local user="${HERMES_MERIDIAN_QUALITY_SSH_USER:-${TERMINAL_SSH_USER:-}}"
  local key="${HERMES_MERIDIAN_QUALITY_SSH_KEY:-${TERMINAL_SSH_KEY:-}}"
  local password="${HERMES_MERIDIAN_QUALITY_SSH_PASSWORD:-${TERMINAL_SSH_PASSWORD:-}}"

  if [[ -n "$key" ]]; then key="$(expand_path "$key")"; fi

  if [[ -z "$host" || -z "$user" ]]; then
    echo "remote workspace requires TERMINAL_SSH_HOST/USER or HERMES_MERIDIAN_QUALITY_SSH_HOST/USER" >&2
    return 1
  fi

  local ssh_cmd=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8)
  if [[ -n "$password" ]]; then
    sshpass -p "$password" "${ssh_cmd[@]}" \
      -o PreferredAuthentications=password \
      -o PubkeyAuthentication=no \
      "$user@$host" "cd '$WORKSPACE' && $*"
    return $?
  fi
  if [[ -n "$key" ]]; then ssh_cmd+=(-i "$key"); fi
  "${ssh_cmd[@]}" "$user@$host" "cd '$WORKSPACE' && $*"
}

ensure_workspace_access() {
  if [[ -d "$WORKSPACE/tasks" ]]; then return; fi
  if remote_exec "test -d tasks"; then return; fi
  echo "Meridian workspace is not accessible: $WORKSPACE" >&2
  exit 1
}

# Count files in a queue by frontmatter status field
task_queue_count() {
  local queue="$1"
  local status_pattern=""
  case "$queue" in
    review)       status_pattern='^(status:[[:space:]]*(review|ready_for_review|READY_FOR_REVIEW))$' ;;
    ready)        status_pattern='^(status:[[:space:]]*ready)$' ;;
    waiting_human) status_pattern='^(status:[[:space:]]*waiting_human)$' ;;
    in_progress)  status_pattern='^(status:[[:space:]]*(in_progress|in-progress|claimed))$' ;;
  esac

  if [[ -n "$status_pattern" ]]; then
    remote_exec "find tasks/in_progress tasks/in-progress -maxdepth 1 -type f -name '*.md' ! -name 'README.md' 2>/dev/null | xargs -I{} sh -c 'grep -lE \"$status_pattern\" \"\$1\" 2>/dev/null || true' sh {} 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
    return
  fi

  remote_exec "find tasks/$queue -maxdepth 1 -type f -name '*.md' ! -name 'README.md' -exec sh -c 'for f do IFS= read -r first <\"\$f\" || true; [ \"\$first\" = \"---\" ] && echo \"\$f\"; done' sh {} + 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

customer_support_count() {
  remote_exec "find customer_support/inbox -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

# Single-slot rule: if a task is actively in_progress, never switch to review.
# Only check review queue when no task is in flight.
pick_mode() {
  local in_progress_count review_count ready_count waiting_count inbox_count

  # Check for a task currently in_progress/claimed — stay in implement until done.
  in_progress_count="$(task_queue_count in_progress)"
  if [[ "$in_progress_count" =~ ^[0-9]+$ ]] && (( in_progress_count > 0 )); then
    echo "implement"
    return
  fi

  # Nothing in flight — now check queues in priority order.
  review_count="$(task_queue_count review)"
  if [[ "$review_count" =~ ^[0-9]+$ ]] && (( review_count > 0 )); then
    echo "review"
    return
  fi

  ready_count="$(task_queue_count ready)"
  if [[ "$ready_count" =~ ^[0-9]+$ ]] && (( ready_count > 0 )); then
    echo "implement"
    return
  fi

  waiting_count="$(task_queue_count waiting_human)"
  inbox_count="$(customer_support_count)"
  if { [[ "$waiting_count" =~ ^[0-9]+$ ]] && (( waiting_count > 0 )); } ||
     { [[ "$inbox_count"   =~ ^[0-9]+$ ]] && (( inbox_count > 0 )); }; then
    echo "plan"
    return
  fi

  echo "idle"
}

mode_max_turns() {
  case "$1" in
    implement) echo "${HERMES_MERIDIAN_IMPLEMENT_MAX_TURNS:-22}" ;;
    review)    echo "${HERMES_MERIDIAN_REVIEW_MAX_TURNS:-14}" ;;
    plan)      echo "${HERMES_MERIDIAN_PLAN_MAX_TURNS:-10}" ;;
    *) echo "Unknown mode: $1" >&2; exit 1 ;;
  esac
}

build_prompt() {
  local mode="$1"
  local local_clock
  local_clock="$(role_local_clock)"

  case "$mode" in
    implement)
      cat <<EOF
You are running the Meridian single-runtime implementation pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: implement
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this is the ONLY always-on Meridian runtime — there is no separate reviewer or planner daemon
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system; tasks/ is the execution and review artifact system
- this invocation is an isolated implementation session

Single-slot rules (STRICT):
- Claim and implement EXACTLY ONE task per pass. Do not pick a second task.
- If you already have a task in tasks/in_progress or tasks/in-progress, continue that task first.
- Complete the task end-to-end: implement, pass verify.sh, commit, move to tasks/review.
- Do NOT stop mid-task and leave it in_progress. Finish before this pass ends.
- Do NOT check the review queue or switch to review work in this pass.
- Only pick from tasks/ready unless a review artifact explicitly sends a task back via request_changes.
- If nothing actionable is ready and nothing is in_progress, stop cleanly.
- Keep changes narrow, production-safe, and easy to review.
- Do not do backlog shaping, support triage, or broad PM work in this pass.

Blocker escalation rule (IMPORTANT):
If you hit an infrastructure or environment blocker that you CANNOT resolve yourself — such as:
  - missing system library (GDAL, GEOS, PROJ, etc.) not installable inside the container
  - SSL/TLS certificate issue outside your control
  - missing secret, credential, or API key you don't have access to
  - broken CI environment or test runner that is a machine-level issue
  - any other "needs human hands on the machine" problem
Then you MUST:
  1. transition the task from in_progress → waiting_human using task_transition
  2. write a clear blocker note in the task file:
     - exactly what you tried (commands, error output)
     - what is missing and why you can't install/fix it yourself
     - what the human needs to do to unblock you
  3. stop this pass cleanly — do NOT keep retrying the same failing approach
  4. do NOT pretend the task is done or move it to review with a broken verification
The human (Umut) monitors waiting_human via Telegram and will resolve blockers directly.

Canonical Developer role body:
$(render_skill_body developer)
EOF
      ;;
    review)
      cat <<EOF
You are running the Meridian single-runtime review pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: review
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this review runs as a separate Hermes chat invocation from implementation work
- treat this pass as an isolated review session
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system; tasks/ is the execution and review artifact system

Review mode rules:
- Start with tasks/review; only treat files with frontmatter status review or ready_for_review as actionable.
- Ignore legacy summaries, patrol notes, approvals, and historical artifacts in tasks/review.
- Your job: approve, or fix small issues inline, or create debt for large issues.
- INLINE FIX RULE: If the required change is small (≤ ~25 lines across ≤ 3 files), apply the fix yourself:
    1. Make the code change.
    2. Run verify.sh and confirm it passes.
    3. Commit with a message referencing the task.
    4. Move the task to tasks/done (or tasks/review/archive if done/ does not exist).
    5. Do NOT create a request_changes artifact — just close the task directly.
- BOUNCE-BACK rule: Only create a request_changes artifact when the rework is substantial (architectural
  changes, new features, many files). Erring on the side of inline fixes is preferred.
- When a task is approved or fixed inline, mark it done/closed. Do not leave it lingering in review.
- If no review-ready work exists, stop cleanly.
- Do not invent patrol work while review items exist.

Canonical Reviewer role body:
$(render_skill_body reviewer)
EOF
      ;;
    plan)
      cat <<EOF
You are running the Meridian single-runtime planning and intake pass.

Profile contract:
- Hermes profile: $PROFILE
- execution mode: plan
- current local time: $local_clock
- workspace: $WORKSPACE

Runtime shape:
- this runtime is not a constantly running Planner daemon
- this plan pass only wakes for waiting-human or inbox/intake work
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system
- tasks/ is only for execution packets, review notes, debt evidence, waiting_human items, and similar delivery artifacts

Plan mode rules:
- Inspect customer_support/inbox and tasks/waiting_human first.
- Shape work, clarify scope, and create or update execution artifacts only when needed.
- Do not mirror the entire Jira backlog into markdown.
- Do not write production code.
- If there is no meaningful intake or clarification work, stop cleanly.

Canonical Planner role body:
$(render_skill_body planner)
EOF
      ;;
    *)
      echo "Unknown mode: $mode" >&2; exit 1 ;;
  esac
}

run_with_timeout() {
  local timeout_seconds="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" "$@"
    return $?
  fi
  "$@"
}

ensure_profile() {
  if "$HERMES_BIN" profile show "$PROFILE" >/dev/null 2>&1; then
    echo "Profile exists: $PROFILE"; return
  fi
  "$HERMES_BIN" profile create "$PROFILE" --clone >/dev/null
  echo "Created profile: $PROFILE"
}

run_chat_pass() {
  local mode="$1"
  local max_turns
  max_turns="$(mode_max_turns "$mode")"

  if [[ "$SERIALIZE_MODEL_ACCESS" == "1" ]] && command -v flock >/dev/null 2>&1; then
    mkdir -p "$(dirname "$MODEL_LOCK_FILE")"
    (
      flock -w 600 9 || {
        echo "[meridian] failed to acquire model lock: $MODEL_LOCK_FILE" >&2
        exit 124
      }
      run_with_timeout "$PASS_TIMEOUT_SECONDS" \
        "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$mode")"
    ) 9>"$MODEL_LOCK_FILE"
    return $?
  fi

  run_with_timeout "$PASS_TIMEOUT_SECONDS" \
    "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$mode")"
}

status_runtime() {
  local status="stopped"
  local pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      status="running pid=$pid"
    else
      status="stale-pid"
    fi
  fi
  echo "meridian | profile=$PROFILE | $status | workspace=$WORKSPACE | log=$LOG_FILE"
  echo "queues: in_progress=$(task_queue_count in_progress) review=$(task_queue_count review) ready=$(task_queue_count ready) waiting_human=$(task_queue_count waiting_human) inbox=$(customer_support_count)"
}

start_runtime() {
  local pid=""
  ensure_workspace_access
  ensure_profile

  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "meridian runtime already running (pid $pid)"
      return
    fi
    rm -f "$PID_FILE"
  fi

  nohup setsid env \
    HERMES_MERIDIAN_WORKSPACE="$WORKSPACE" \
    HERMES_MERIDIAN_PROFILE="$PROFILE" \
    HERMES_MERIDIAN_TIMEZONE="$TIMEZONE_NAME" \
    HERMES_MERIDIAN_ACTIVE_SLEEP_SECONDS="$ACTIVE_SLEEP_SECONDS" \
    HERMES_MERIDIAN_IDLE_SLEEP_SECONDS="$IDLE_SLEEP_SECONDS" \
    HERMES_MERIDIAN_PASS_TIMEOUT_SECONDS="$PASS_TIMEOUT_SECONDS" \
    HERMES_MERIDIAN_STARTUP_JITTER_SECONDS="$STARTUP_JITTER_SECONDS" \
    HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS="$SERIALIZE_MODEL_ACCESS" \
    HERMES_MERIDIAN_MODEL_LOCK_FILE="$MODEL_LOCK_FILE" \
    bash "$0" run-loop >>"$LOG_FILE" 2>&1 &

  pid=$!
  echo "$pid" >"$PID_FILE"
  echo "Started meridian runtime (pid $pid, log $LOG_FILE)"
}

stop_runtime() {
  local pid=""
  if [[ ! -f "$PID_FILE" ]]; then
    echo "meridian runtime not running"
    return
  fi
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "Stopped meridian runtime (pid $pid)"
  else
    echo "Removed stale meridian runtime pid file"
  fi
  rm -f "$PID_FILE"
}

run_loop() {
  ensure_workspace_access
  ensure_profile

  if [[ "$STARTUP_JITTER_SECONDS" =~ ^[0-9]+$ ]] && (( STARTUP_JITTER_SECONDS > 0 )); then
    sleep $(( RANDOM % (STARTUP_JITTER_SECONDS + 1) ))
  fi

  while true; do
    local mode
    mode="$(pick_mode)"
    echo "=== $(date -Is) [meridian] profile=$PROFILE workspace=$WORKSPACE mode=$mode ==="

    if [[ "$mode" == "idle" ]]; then
      echo "[meridian] No actionable work — sleeping ${IDLE_SLEEP_SECONDS}s"
      sleep "$IDLE_SLEEP_SECONDS"
      continue
    fi

    run_chat_pass "$mode" || true
    sleep "$ACTIVE_SLEEP_SECONDS"
  done
}

run_pass_action() {
  local mode="$1"
  ensure_workspace_access
  ensure_profile
  run_chat_pass "$mode"
}

case "$ACTION" in
  setup-profile)  ensure_profile ;;
  start)          start_runtime ;;
  stop)           stop_runtime ;;
  restart)        stop_runtime; start_runtime ;;
  status)         status_runtime ;;
  run-loop)       run_loop ;;
  run-pass)
    if [[ -z "$MODE" ]]; then
      echo "Usage: $0 run-pass <implement|review|plan>" >&2; exit 1
    fi
    run_pass_action "$MODE"
    ;;
  *)
    echo "Usage: $0 <setup-profile|start|stop|restart|status|run-loop|run-pass> [implement|review|plan]" >&2
    exit 1
    ;;
esac
