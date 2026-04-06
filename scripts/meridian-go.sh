nohup bash -lc '
pkill -f "hermes chat --quiet --yolo --max-turns 40 -q"
cd /home/umut/Hermes-Agent || exit 1
source venv/bin/activate
while true; do
  echo "=== $(date -Is) ==="
  hermes chat --quiet --yolo --max-turns 40 -q "You are Hermes acting as the Meridian orchestrator. Use the configured SSH terminal backend to inspect and operate on the Meridian workspace. Treat Philip as the default human-facing persona. Prioritize work in this order: 1) active review and request-changes loops, 2) ready tasks for Fatih, 3) backlog shaping and planning for Philip, 4) architecture/security patrol for Matthew when Fatih is idle. Require task-related commits before work moves to review. Make one orchestration pass, perform any immediate actions that are clearly needed, then stop cleanly when no immediate next action remains."
  sleep 90
done
' > ~/meridian-agent-loop.log 2>&1 &
