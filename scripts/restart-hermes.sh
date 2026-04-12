#!/usr/bin/env bash
# Restart Hermes gateway + Meridian multi-agent loops + Board dashboard
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD_HOST="${HERMES_MERIDIAN_NOTIFY_SSH_HOST:-${TERMINAL_SSH_HOST:-192.168.1.107}}"
BOARD_SSH_KEY="${TERMINAL_SSH_KEY:-~/.ssh/id_ed25519}"
BOARD_SSH_USER="${TERMINAL_SSH_USER:-umut}"

echo "[1/4] Resetting any failed state..."
sudo systemctl reset-failed hermes-gateway.service 2>/dev/null || true

echo "[2/4] Restarting hermes-gateway service..."
sudo systemctl restart hermes-gateway.service

echo "[3/4] Restarting Meridian agent loops (philip, fatih, matthew)..."
bash "$SCRIPT_DIR/meridian-multi-agent.sh" restart

echo "[4/4] Restarting Meridian board dashboard on $BOARD_HOST..."
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    -i "$(eval echo $BOARD_SSH_KEY)" \
    "${BOARD_SSH_USER}@${BOARD_HOST}" \
    'echo figo1190 | sudo -S systemctl restart meridian-board.service 2>/dev/null && echo "  Board: restarted" || echo "  Board: skipped (not configured)"' \
    2>/dev/null || echo "  Board: SSH unreachable, skipping"

echo ""
echo "=== Status ==="
systemctl status hermes-gateway.service --no-pager | head -5
echo "---"
bash "$SCRIPT_DIR/meridian-multi-agent.sh" status
