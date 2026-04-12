#!/usr/bin/env bash

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
  if [[ "$raw" == "~" ]]; then
    printf '%s\n' "$HOME"
    return
  fi
  if [[ "$raw" == "~/"* ]]; then
    printf '%s/%s\n' "$HOME" "${raw#~/}"
    return
  fi
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

  if [[ -n "$key" ]]; then
    key="$(expand_path "$key")"
  fi

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
  if [[ -n "$key" ]]; then
    ssh_cmd+=(-i "$key")
  fi
  "${ssh_cmd[@]}" "$user@$host" "cd '$WORKSPACE' && $*"
}

ensure_workspace_access() {
  if [[ -d "$WORKSPACE/tasks" ]]; then
    return
  fi
  if remote_exec "test -d tasks"; then
    return
  fi
  echo "Meridian workspace is not accessible: $WORKSPACE" >&2
  exit 1
}

queue_count() {
  local queue="$1"
  remote_exec "find tasks/$queue -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

customer_support_count() {
  remote_exec "find customer_support/inbox -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

pick_mode() {
  local review_count ready_count waiting_count inbox_count
  review_count="$(queue_count review)"
  ready_count="$(queue_count ready)"
  waiting_count="$(queue_count waiting_human)"
  inbox_count="$(customer_support_count)"

  if [[ "$review_count" =~ ^[0-9]+$ ]] && (( review_count > 0 )); then
    echo "review"
    return
  fi
  if [[ "$ready_count" =~ ^[0-9]+$ ]] && (( ready_count > 0 )); then
    echo "implement"
    return
  fi
  if [[ "$waiting_count" =~ ^[0-9]+$ ]] && (( waiting_count > 0 )); then
    echo "plan"
    return
  fi
  if [[ "$inbox_count" =~ ^[0-9]+$ ]] && (( inbox_count > 0 )); then
    echo "plan"
    return
  fi
  echo "idle"
}

mode_max_turns() {
  case "$1" in
    implement) echo "${HERMES_MERIDIAN_IMPLEMENT_MAX_TURNS:-18}" ;;
    review) echo "${HERMES_MERIDIAN_REVIEW_MAX_TURNS:-14}" ;;
    plan) echo "${HERMES_MERIDIAN_PLAN_MAX_TURNS:-10}" ;;
    *)
      echo "Unknown mode: $1" >&2
      exit 1
      ;;
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
- this is the only always-on Meridian runtime
- the live project checkout is on this machine at $WORKSPACE
- do not assume there is a second writable Meridian checkout elsewhere
- Jira is the primary backlog system; tasks/ is the execution and review artifact system
- this invocation is an isolated implementation session, not a shared review context

Implement mode rules:
- behave as Fatih
- only pick from tasks/ready unless a review artifact explicitly sends a task back into an active request-changes loop
- move work through tasks/in_progress and then tasks/review
- do not self-approve
- if nothing actionable is ready, stop cleanly
- keep changes narrow, production-safe, and easy to review
- do not do backlog shaping, support triage, or broad PM work in this pass

Canonical Fatih role body:
$(render_skill_body fatih)
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
- treat this pass as an isolated review session, not a continuation of the implementation context
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system; tasks/ is the execution and review artifact system

Review mode rules:
- behave as Matthew
- start with tasks/review and keep scope narrow
- your default job is review-only: approve, request changes, create debt, or move to waiting_human
- do not quietly implement the fix yourself just because you can
- only use a tiny review-contained fix exception when it is obviously lower risk than a bounce-back
- if no review-ready work exists, stop cleanly
- do not invent patrol work while review items exist

Canonical Matthew role body:
$(render_skill_body matthew)
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
- this runtime is not a constantly running Philip daemon
- this plan pass only wakes for waiting-human or inbox/intake work
- the live project checkout is on this machine at $WORKSPACE
- Jira is the primary backlog system
- tasks/ is only for execution packets, review notes, debt evidence, waiting_human items, and similar delivery artifacts

Plan mode rules:
- behave as Philip
- inspect customer_support/inbox and tasks/waiting_human first
- shape work, clarify scope, and create or update execution artifacts only when needed
- do not mirror the entire Jira backlog into markdown
- do not write production code
- if there is no meaningful intake or clarification work, stop cleanly

Canonical Philip role body:
$(render_skill_body philip)
EOF
      ;;
    *)
      echo "Unknown mode: $mode" >&2
      exit 1
      ;;
  esac
}

run_with_timeout() {
  local timeout_seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" "$@"
    return $?
  fi
  "$@"
}

ensure_profile() {
  if "$HERMES_BIN" profile show "$PROFILE" >/dev/null 2>&1; then
    echo "Profile exists: $PROFILE"
    return
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
  echo "queues: review=$(queue_count review) ready=$(queue_count ready) waiting_human=$(queue_count waiting_human) inbox=$(customer_support_count)"
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
      echo "[meridian] No actionable review, implementation, or planning event"
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
  setup-profile)
    ensure_profile
    ;;
  start)
    start_runtime
    ;;
  stop)
    stop_runtime
    ;;
  restart)
    stop_runtime
    start_runtime
    ;;
  status)
    status_runtime
    ;;
  run-loop)
    run_loop
    ;;
  run-pass)
    if [[ -z "$MODE" ]]; then
      echo "Usage: $0 run-pass <implement|review|plan>" >&2
      exit 1
    fi
    run_pass_action "$MODE"
    ;;
  *)
    echo "Usage: $0 <setup-profile|start|stop|restart|status|run-loop|run-pass> [implement|review|plan]" >&2
    exit 1
    ;;
esac
