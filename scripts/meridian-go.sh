#!/usr/bin/env bash

set -euo pipefail

echo "scripts/meridian-go.sh is deprecated."
echo "Use the single-agent Meridian workflow instead."
echo

cat <<'EOF'
Recommended flow:
1. Open the Meridian task or request.
2. Shape it if needed.
3. Create a task branch.
4. Implement and commit.
5. Review again with Matthew-style reviewer eyes.
6. Push and merge when the review passes.
EOF
