#!/usr/bin/env bash
#
# Deprecated compatibility wrapper.
# Meridian should now be started and managed through scripts/meridian-single-agent.sh.
#

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SINGLE_AGENT_SCRIPT="$ROOT_DIR/scripts/meridian-single-agent.sh"

if [[ ! -x "$SINGLE_AGENT_SCRIPT" ]]; then
  echo "Meridian single-agent script not found: $SINGLE_AGENT_SCRIPT" >&2
  exit 1
fi

ROLE="${1:-}"
WORKSPACE="${2:-${HERMES_MERIDIAN_WORKSPACE:-$HOME/Meridian}}"

if [[ -z "$ROLE" ]]; then
  echo "Usage: $0 <planner|developer|reviewer> [workspace]" >&2
  echo "Legacy aliases: philip=planner, fatih=developer, matthew=reviewer" >&2
  exit 1
fi

case "$ROLE" in
  philip|planner)
    MODE="plan"
    ;;
  fatih|developer)
    MODE="implement"
    ;;
  matthew|reviewer)
    MODE="review"
    ;;
  *)
    echo "Unknown role: $ROLE" >&2
    exit 1
    ;;
esac

echo "scripts/meridian-role-loop.sh is deprecated."
echo "Forwarding to scripts/meridian-single-agent.sh run-pass $MODE"

exec env HERMES_MERIDIAN_WORKSPACE="$WORKSPACE" bash "$SINGLE_AGENT_SCRIPT" run-pass "$MODE"
