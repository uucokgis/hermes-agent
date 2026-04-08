#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-status}"
ROLE_FILTER="${2:-all}"
WORKSPACE="${HERMES_MERIDIAN_WORKSPACE:-/home/umut/meridian}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="$ROOT_DIR/venv/bin/hermes"
ROLE_LOOP="$ROOT_DIR/scripts/meridian-role-loop.sh"
STATE_DIR="${HOME}/.hermes/meridian/loops"
SERIALIZE_MODEL_ACCESS="${HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS:-0}"

mkdir -p "$STATE_DIR"

roles() {
  if [[ "$ROLE_FILTER" == "all" ]]; then
    printf '%s\n' philip fatih matthew
  else
    printf '%s\n' "$ROLE_FILTER"
  fi
}

role_profile() {
  case "$1" in
    philip) echo "meridian-philip" ;;
    fatih) echo "meridian-fatih" ;;
    matthew) echo "meridian-matthew" ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_log() {
  echo "${STATE_DIR}/$1.loop.log"
}

role_pid() {
  echo "${STATE_DIR}/$1.loop.pid"
}

ensure_profile() {
  local role="$1"
  local profile
  profile="$(role_profile "$role")"
  if "$HERMES_BIN" profile show "$profile" >/dev/null 2>&1; then
    echo "Profile exists: $profile"
    return
  fi
  "$HERMES_BIN" profile create "$profile" --clone
}

start_role() {
  local role="$1"
  local pid_file log_file pid
  pid_file="$(role_pid "$role")"
  log_file="$(role_log "$role")"
  ensure_profile "$role"

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "$role already running (pid $pid)"
      return
    fi
    rm -f "$pid_file"
  fi

  nohup setsid env HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS="$SERIALIZE_MODEL_ACCESS" \
    bash "$ROLE_LOOP" "$role" "$WORKSPACE" >>"$log_file" 2>&1 &
  pid=$!
  echo "$pid" >"$pid_file"
  echo "Started $role (pid $pid, log $log_file)"
}

stop_role() {
  local role="$1"
  local pid_file pid
  pid_file="$(role_pid "$role")"
  if [[ ! -f "$pid_file" ]]; then
    echo "$role not running"
    return
  fi
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    # Stop the whole process group so the wrapper bash and its child
    # hermes chat process do not leak across restarts.
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    echo "Stopped $role (pid $pid)"
  else
    echo "$role stale pid file removed"
  fi
  rm -f "$pid_file"
}

status_role() {
  local role="$1"
  local pid_file log_file pid status profile
  pid_file="$(role_pid "$role")"
  log_file="$(role_log "$role")"
  profile="$(role_profile "$role")"
  status="stopped"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      status="running pid=$pid"
    else
      status="stale-pid"
    fi
  fi
  echo "$role | profile=$profile | $status | log=$log_file"
}

case "$ACTION" in
  setup-profiles)
    for role in $(roles); do
      ensure_profile "$role"
    done
    ;;
  start)
    for role in $(roles); do
      start_role "$role"
    done
    ;;
  stop)
    for role in $(roles); do
      stop_role "$role"
    done
    ;;
  restart)
    for role in $(roles); do
      stop_role "$role"
      start_role "$role"
    done
    ;;
  status)
    echo "Gateway:"
    systemctl show hermes-gateway.service -p ActiveState -p SubState --no-pager 2>/dev/null || true
    echo "---"
    for role in $(roles); do
      status_role "$role"
    done
    ;;
  *)
    echo "Usage: $0 <setup-profiles|start|stop|restart|status> [all|philip|fatih|matthew]" >&2
    exit 1
    ;;
esac
