#!/usr/bin/env bash
# Restart Hermes gateway + Meridian multi-agent loops
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/3] Resetting any failed state..."
sudo systemctl reset-failed hermes-gateway.service 2>/dev/null || true

echo "[2/3] Restarting hermes-gateway service..."
sudo systemctl restart hermes-gateway.service

echo "[3/3] Restarting Meridian agent loops (philip, fatih, matthew)..."
bash "$SCRIPT_DIR/meridian-multi-agent.sh" restart

echo ""
echo "Done. Status:"
systemctl status hermes-gateway.service --no-pager | head -6
echo "---"
bash "$SCRIPT_DIR/meridian-multi-agent.sh" status
