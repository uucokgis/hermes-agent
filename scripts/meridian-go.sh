#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "scripts/meridian-go.sh is deprecated."
echo "Using the single Meridian runtime entrypoint instead."
echo

if [[ "${1:-}" == "run-pass" ]]; then
  shift
  exec bash "$SCRIPT_DIR/meridian-single-agent.sh" run-pass "${1:-implement}"
fi

exec bash "$SCRIPT_DIR/meridian-single-agent.sh" status
