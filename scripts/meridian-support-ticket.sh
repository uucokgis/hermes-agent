#!/usr/bin/env bash

set -euo pipefail

WORKSPACE="${HERMES_MERIDIAN_WORKSPACE:-}"
SOURCE="telegram"
SENDER=""
SUMMARY=""
BODY=""

usage() {
  cat >&2 <<'EOF'
Usage: meridian-support-ticket.sh --workspace PATH --summary TEXT [options]

Options:
  --workspace PATH   Meridian coordination workspace root
  --source NAME      Ticket source label (default: telegram)
  --sender NAME      Human or chat identifier
  --summary TEXT     Short one-line summary
  --body TEXT        Longer request body
EOF
  exit 1
}

slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="${2:-}"
      shift 2
      ;;
    --source)
      SOURCE="${2:-}"
      shift 2
      ;;
    --sender)
      SENDER="${2:-}"
      shift 2
      ;;
    --summary)
      SUMMARY="${2:-}"
      shift 2
      ;;
    --body)
      BODY="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [[ -z "$WORKSPACE" || -z "$SUMMARY" ]]; then
  usage
fi

TIMESTAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
DATE_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
SLUG="$(slugify "$SUMMARY")"
if [[ -z "$SLUG" ]]; then
  SLUG="ticket"
fi

SUPPORT_DIR="${WORKSPACE%/}/customer_support/inbox"
mkdir -p "$SUPPORT_DIR"

FILE_PATH="${SUPPORT_DIR}/${TIMESTAMP}-${SLUG}.md"

cat >"$FILE_PATH" <<EOF
---
id: support-${TIMESTAMP}
source: ${SOURCE}
sender: ${SENDER}
status: pending_philip
created_at: ${DATE_ISO}
updated_at: ${DATE_ISO}
summary: ${SUMMARY}
---

# Request

${BODY:-No additional body provided.}

# Philip Response Draft

Pending.

# Notes

- Captured by meridian-support-ticket.sh
EOF

printf '%s\n' "$FILE_PATH"
