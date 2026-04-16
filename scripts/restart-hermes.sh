#!/usr/bin/env bash
# Restart Hermes gateway + Board dashboard
set -euo pipefail

BOARD_HOST="${HERMES_MERIDIAN_NOTIFY_SSH_HOST:-${TERMINAL_SSH_HOST:-localhost}}"
BOARD_SSH_KEY="${TERMINAL_SSH_KEY:-~/.ssh/id_ed25519}"
BOARD_SSH_USER="${TERMINAL_SSH_USER:-umut}"

echo "[1/3] Resetting any failed state..."
sudo systemctl reset-failed hermes-gateway.service 2>/dev/null || true

echo "[2/3] Restarting hermes-gateway service..."
sudo systemctl restart hermes-gateway.service

echo "[3/3] Restarting Meridian board dashboard on $BOARD_HOST..."
if [[ "$BOARD_HOST" == "localhost" || "$BOARD_HOST" == "127.0.0.1" ]]; then
  if sudo systemctl restart meridian-board.service 2>/dev/null; then
    echo "  Board: restarted"
  else
    echo "  Board: skipped (not configured)"
  fi
else
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
      -i "$(eval echo $BOARD_SSH_KEY)" \
      "${BOARD_SSH_USER}@${BOARD_HOST}" \
      'echo figo1190 | sudo -S systemctl restart meridian-board.service 2>/dev/null && echo "  Board: restarted" || echo "  Board: skipped (not configured)"' \
      2>/dev/null || echo "  Board: SSH unreachable, skipping"
fi

echo ""
echo "=== Status ==="
systemctl status hermes-gateway.service --no-pager | head -5
