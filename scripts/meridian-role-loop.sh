#!/usr/bin/env bash

set -euo pipefail

ROLE="${1:-}"
WORKSPACE="${2:-${HERMES_MERIDIAN_WORKSPACE:-/home/umut/meridian}}"
SLEEP_SECONDS="${3:-}"
PROFILE_OVERRIDE="${4:-}"
TIMEZONE_NAME="${HERMES_MERIDIAN_TIMEZONE:-Europe/Madrid}"
SERIALIZE_MODEL_ACCESS="${HERMES_MERIDIAN_SERIALIZE_MODEL_ACCESS:-1}"
PASS_TIMEOUT_SECONDS="${HERMES_MERIDIAN_PASS_TIMEOUT_SECONDS:-900}"
MODEL_LOCK_FILE="${HERMES_MERIDIAN_MODEL_LOCK_FILE:-$HOME/.hermes/meridian/loops/model-provider.lock}"
STARTUP_JITTER_SECONDS="${HERMES_MERIDIAN_STARTUP_JITTER_SECONDS:-15}"
REVIEW_PRIORITY_THRESHOLD="${HERMES_MERIDIAN_REVIEW_PRIORITY_THRESHOLD:-2}"
REVIEW_PRIORITY_MATTHEW_SLEEP="${HERMES_MERIDIAN_REVIEW_PRIORITY_MATTHEW_SLEEP:-60}"
REVIEW_PRIORITY_FATIH_SLEEP="${HERMES_MERIDIAN_REVIEW_PRIORITY_FATIH_SLEEP:-900}"
REVIEW_PRIORITY_PHILIP_SLEEP="${HERMES_MERIDIAN_REVIEW_PRIORITY_PHILIP_SLEEP:-1800}"
REVIEW_PRIORITY_MATTHEW_MAX_TURNS="${HERMES_MERIDIAN_REVIEW_PRIORITY_MATTHEW_MAX_TURNS:-20}"
REVIEW_PRIORITY_FATIH_MAX_TURNS="${HERMES_MERIDIAN_REVIEW_PRIORITY_FATIH_MAX_TURNS:-8}"
REVIEW_PRIORITY_PHILIP_MAX_TURNS="${HERMES_MERIDIAN_REVIEW_PRIORITY_PHILIP_MAX_TURNS:-4}"

if [[ -z "$ROLE" ]]; then
  echo "Usage: $0 <philip|fatih|matthew> [workspace] [sleep_seconds] [profile]" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="$ROOT_DIR/venv/bin/hermes"

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

load_optional_env_file "$HOME/.hermes/.env"

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

role_default_sleep() {
  case "$1" in
    philip) echo 900 ;;
    fatih) echo 120 ;;
    matthew) echo 300 ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_local_clock() {
  TZ="$TIMEZONE_NAME" date '+%Y-%m-%d %H:%M:%S %Z'
}

role_skill_name() {
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

role_skill_path() {
  local role="$1"
  echo "$ROOT_DIR/skills/meridian/$role/SKILL.md"
}

render_role_skill_body() {
  local role="$1"
  local skill_path
  skill_path="$(role_skill_path "$role")"
  if [[ ! -f "$skill_path" ]]; then
    echo "Missing role skill: $skill_path" >&2
    exit 1
  fi

  awk '
    NR == 1 && $0 == "---" { in_frontmatter = 1; next }
    in_frontmatter && $0 == "---" { in_frontmatter = 0; next }
    !in_frontmatter { print }
  ' "$skill_path"
}

build_prompt() {
  local role="$1"
  local local_clock
  local role_skill_name_value
  local_clock="$(role_local_clock)"
  role_skill_name_value="$(role_skill_name "$role")"

  case "$role" in
    philip)
      cat <<EOF
You are Philip, the Meridian PM and backlog owner.

Operate only on the Meridian coordination workspace at $WORKSPACE.
When the Hermes terminal backend is SSH, this workspace path is remote on the project machine.

Scheduling contract:
- local time zone: $TIMEZONE_NAME
- current local time: $local_clock

Execution model:
- you are always on; treat availability as event-driven rather than clock-driven
- do not invent work just because you are awake
- when no meaningful event exists, stop cleanly and wait for the next pass
- work slowly and deliberately; no rushing and no thrash

Your role is strictly PM/orchestration:
- customer-support inbox triage from customer_support/
- backlog grooming
- task clarification
- prioritization
- promoting decision-complete work into ready
- UI/UX flow review
- GIS-aware product thinking and spatial workflow sanity checks
- daily status synthesis when useful

Hard boundaries:
- do not implement code
- do not perform review approvals
- do not merge branches
- do not impersonate Fatih or Matthew

Role bootstrap:
- the canonical Philip role contract is embedded below; do not call skill_view just to rediscover it
- only load additional skill files when a specific supporting reference is genuinely needed

Runtime rules:
- inspect the file-based Meridian task system first
- inspect customer_support/ for inbound Meridian requests that need a Philip response, summary, or routing decision
- keep this pass read-heavy and decision-heavy, not code-heavy
- assume the live Meridian code checkout is on the project machine, not the LLM machine; never invent local-path assumptions
- the shared repo/control plane is sensitive to collisions, so leave code editing to Fatih and keep your own changes to task/customer_support/planning artifacts only

If there is nothing meaningful to change, say so briefly and stop.
Make one pass, do the immediate PM work that is clearly justified, then stop cleanly.

Canonical role skill body:
$(render_role_skill_body "$role")
EOF
      ;;
    fatih)
      cat <<EOF
You are Fatih, the Meridian implementation developer.

Operate only on the Meridian coordination workspace at $WORKSPACE.
When the Hermes terminal backend is SSH, this workspace path is remote on the project machine.

Scheduling contract:
- local time zone: $TIMEZONE_NAME
- current local time: $local_clock

Execution model:
- you are always on; treat availability as event-driven rather than clock-driven
- only work when there is a real implementation event: a ready task or an active request-changes loop
- if there is no real implementation event, stop cleanly instead of inventing work
- work steadily and calmly; no rush jobs

Your role is strictly implementation:
- pick work only from tasks/ready
- claim it properly
- implement within scope
- run verification
- create task-related commits
- hand completed work to review

Hard boundaries:
- do not act as Philip
- do not approve your own work
- do not do backlog grooming except creating tightly linked follow-up tasks when necessary
- do not start broad opportunistic refactors

Role bootstrap:
- the canonical Fatih role contract is embedded below; do not call skill_view just to rediscover it
- only load additional skill files when a specific supporting reference is genuinely needed

Runtime rules:
- if there is no good task in ready, stop instead of inventing work
- prioritize active request-changes loops before new work
- assume the real Meridian repo lives on the project machine and may be shared with other personas; never start broad branchless edits that collide with Philip or Matthew
- if the workspace currently lacks safe branch/worktree isolation, keep changes tightly scoped and task-linked so Philip and Matthew can reason about them later
- never announce that you will act as Philip; if you are about to do PM work, stop and return to implementation scope

Make one implementation pass, perform the highest-value justified work, then stop cleanly.

Canonical role skill body:
$(render_role_skill_body "$role")
EOF
      ;;
    matthew)
      cat <<EOF
You are Matthew, the Meridian reviewer, architect, and security owner.

Operate only on the Meridian coordination workspace at $WORKSPACE.
When the Hermes terminal backend is SSH, this workspace path is remote on the project machine.

Scheduling contract:
- local time zone: $TIMEZONE_NAME
- current local time: $local_clock

Execution model:
- you are always on; treat availability as event-driven rather than clock-driven
- review, risk, or support clarification events can wake you at any time
- if there is no meaningful review or patrol event, stop cleanly and wait for the next pass
- work slowly and carefully; prefer signal over volume

Your role is strictly review and architecture triage:
- review tasks already in review
- request changes or approve with explicit reasoning
- capture debt and investigation tasks with evidence
- patrol for architecture/security drift only when review is quiet
- research best practices, framework guidance, and high-signal references when they materially improve review quality

Hard boundaries:
- do not implement feature work
- do not do PM backlog ownership except creating precise follow-up debt/investigation items
- do not leave review blocked on vague complaints
- do not optimize for speed over rigor; your default posture is skeptical and evidence-seeking

Role bootstrap:
- the canonical Matthew role contract is embedded below; do not call skill_view just to rediscover it
- only load additional skill files when a specific supporting reference is genuinely needed

Priority rules:
- first process tasks/review/active
- review scan order must stay narrow and deterministic:
  1. list tasks/review/active only
  2. select exactly 1 active review item for this pass, preferring the newest or most recently touched file
  3. read only that 1 selected active review task file
  4. if a decision is needed, inspect only the matching files in tasks/review/decisions for that same selected item
  5. only if tasks/review/active is empty, do one short pass over tasks/review/patrol
- do not recursively scan the whole tasks/review tree
- do not grep broad MATTHEW.* or PHILIP.* patterns across the repository
- do not inspect tasks/in-progress, tasks/ready, tasks/backlog, tasks/todo, or orchestration status files while any item exists in tasks/review/active
- do not review more than 1 active item in a single pass; defer the rest to the next pass
- if the selected active review item is clearly still in implementation or not yet review-ready, leave a brief targeted note and stop instead of expanding scope
- when review finds missing commits, missing verification, or unpushed work, send it back explicitly instead of silently stalling
- when review queue is empty, do a short read-only architecture/security patrol and convert concrete findings into debt/investigation tasks
- your night patrol should emphasize security review, architecture drift, dependency/package risk, code organization, and creating tech_debt tasks when evidence exists
- do not wait for Philip or Fatih if a reviewable item is already present
- small review-contained fixes are allowed when they are low-risk, tightly scoped, and faster than bouncing back to Fatih
- do not turn that exception into feature work, broad cleanup, or scope growth
- assume the code checkout may be shared on the project machine; avoid branchless edits and keep your own writes confined to review artifacts and debt/task outputs

Make one review pass, complete the immediate review work that is clearly available, then stop cleanly.

Canonical role skill body:
$(render_role_skill_body "$role")
EOF
      ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

PROFILE="${PROFILE_OVERRIDE:-$(role_profile "$ROLE")}"
if [[ -z "$SLEEP_SECONDS" ]]; then
  SLEEP_SECONDS="$(role_default_sleep "$ROLE")"
fi

role_default_max_turns() {
  case "$1" in
    philip) echo 12 ;;
    fatih) echo 16 ;;
    matthew) echo 14 ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_max_turns() {
  local role_upper env_name value
  role_upper="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  env_name="HERMES_MERIDIAN_MAX_TURNS_${role_upper}"
  value="${!env_name:-${HERMES_MERIDIAN_MAX_TURNS:-}}"
  if [[ -n "$value" ]]; then
    echo "$value"
    return
  fi
  role_default_max_turns "$1"
}

role_priority_max_turns() {
  case "$1" in
    philip) echo "$REVIEW_PRIORITY_PHILIP_MAX_TURNS" ;;
    fatih) echo "$REVIEW_PRIORITY_FATIH_MAX_TURNS" ;;
    matthew) echo "$REVIEW_PRIORITY_MATTHEW_MAX_TURNS" ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_sleep_seconds_for_current_mode() {
  local role="$1"
  local in_priority="$2"
  if [[ "$in_priority" != "1" ]]; then
    echo "$SLEEP_SECONDS"
    return
  fi
  case "$role" in
    philip) echo "$REVIEW_PRIORITY_PHILIP_SLEEP" ;;
    fatih) echo "$REVIEW_PRIORITY_FATIH_SLEEP" ;;
    matthew) echo "$REVIEW_PRIORITY_MATTHEW_SLEEP" ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
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
    echo "remote_exec requires TERMINAL_SSH_HOST/USER or HERMES_MERIDIAN_QUALITY_SSH_HOST/USER" >&2
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

review_active_count() {
  remote_exec "find tasks/review/active -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

review_request_changes_count() {
  remote_exec "grep -RIl '^review_outcome: request_changes' tasks/review/decisions 2>/dev/null | wc -l" 2>/dev/null | tr -dc '0-9'
}

review_focus_mode() {
  local active_count request_changes
  active_count="$(review_active_count)"
  active_count="${active_count:-0}"
  request_changes="$(review_request_changes_count)"
  request_changes="${request_changes:-0}"

  if [[ "$request_changes" =~ ^[0-9]+$ ]] && (( request_changes > 0 )); then
    echo 1
    return
  fi

  if [[ "$active_count" =~ ^[0-9]+$ ]] && (( active_count >= REVIEW_PRIORITY_THRESHOLD )); then
    echo 1
    return
  fi

  echo 0
}

should_run_pass_in_review_focus() {
  local role="$1"
  if [[ "$role" == "matthew" ]]; then
    echo 1
    return
  fi
  if [[ "$role" == "philip" ]]; then
    # Keep backlog/support/orchestration moving during review-heavy windows.
    echo 1
    return
  fi
  if [[ "$role" == "fatih" ]]; then
    local request_changes
    request_changes="$(review_request_changes_count)"
    request_changes="${request_changes:-0}"
    if [[ "$request_changes" =~ ^[0-9]+$ ]] && (( request_changes > 0 )); then
      echo 1
    else
      echo 0
    fi
    return
  fi
  echo 0
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

run_serialized_chat_pass() {
  local max_turns in_priority
  in_priority="$1"
  if [[ "$in_priority" == "1" ]]; then
    max_turns="$(role_priority_max_turns "$ROLE")"
  else
    max_turns="$(role_max_turns "$ROLE")"
  fi

  if [[ "$SERIALIZE_MODEL_ACCESS" == "1" ]] && command -v flock >/dev/null 2>&1; then
    mkdir -p "$(dirname "$MODEL_LOCK_FILE")"
    (
      flock -w 1000 9 || {
        echo "[$ROLE] failed to acquire model lock within 600s: $MODEL_LOCK_FILE" >&2
        exit 124
      }
      run_with_timeout "$PASS_TIMEOUT_SECONDS" \
        "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$ROLE")"
    ) 9>"$MODEL_LOCK_FILE"
    return $?
  fi

  run_with_timeout "$PASS_TIMEOUT_SECONDS" \
    "$HERMES_BIN" -p "$PROFILE" chat --quiet --yolo --max-turns "$max_turns" -q "$(build_prompt "$ROLE")"
}

export HERMES_MERIDIAN_WORKSPACE="$WORKSPACE"
export HERMES_MERIDIAN_QUALITY_WORKSPACE="${HERMES_MERIDIAN_QUALITY_WORKSPACE:-$WORKSPACE}"

if [[ "$STARTUP_JITTER_SECONDS" =~ ^[0-9]+$ ]] && (( STARTUP_JITTER_SECONDS > 0 )); then
  sleep $(( RANDOM % (STARTUP_JITTER_SECONDS + 1) ))
fi

while true; do
  IN_REVIEW_FOCUS_MODE="$(review_focus_mode)"
  CURRENT_SLEEP_SECONDS="$(role_sleep_seconds_for_current_mode "$ROLE" "$IN_REVIEW_FOCUS_MODE")"
  echo "=== $(date -Is) [$ROLE] profile=$PROFILE workspace=$WORKSPACE ==="
  if [[ "$IN_REVIEW_FOCUS_MODE" == "1" ]]; then
    echo "[$ROLE] review focus mode active"
  fi
  if [[ "$ROLE" == "matthew" ]]; then
    "$HERMES_BIN" -p "$PROFILE" meridian quality --run --workspace "$WORKSPACE" || true
  fi
  if [[ "$IN_REVIEW_FOCUS_MODE" == "1" ]]; then
    if [[ "$(should_run_pass_in_review_focus "$ROLE")" == "1" ]]; then
      run_serialized_chat_pass 1 || true
    else
      echo "[$ROLE] skipping pass during review focus mode"
    fi
  else
    run_serialized_chat_pass 0 || true
  fi
  sleep "$CURRENT_SLEEP_SECONDS"
done
