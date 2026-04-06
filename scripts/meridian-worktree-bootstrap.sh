#!/usr/bin/env bash

set -euo pipefail

REPO_PATH=""
ROLE=""
BRANCH_NAME=""
BASE_REF="HEAD"

usage() {
  cat >&2 <<'EOF'
Usage: meridian-worktree-bootstrap.sh --repo PATH --role ROLE [options]

Options:
  --repo PATH       Meridian git repository path on the project machine
  --role ROLE       philip | fatih | matthew
  --branch NAME     Override branch name
  --base REF        Base ref for the new worktree (default: HEAD)
EOF
  exit 1
}

default_branch() {
  case "$1" in
    philip) echo "meridian/philip-planning" ;;
    fatih) echo "meridian/fatih-active" ;;
    matthew) echo "meridian/matthew-review" ;;
    *)
      echo "Unknown role: $1" >&2
      exit 1
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_PATH="${2:-}"
      shift 2
      ;;
    --role)
      ROLE="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH_NAME="${2:-}"
      shift 2
      ;;
    --base)
      BASE_REF="${2:-}"
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

if [[ -z "$REPO_PATH" || -z "$ROLE" ]]; then
  usage
fi

REPO_PATH="$(cd "$REPO_PATH" && pwd)"
if [[ ! -d "$REPO_PATH/.git" ]]; then
  echo "Not a git repository: $REPO_PATH" >&2
  exit 1
fi

if [[ -z "$BRANCH_NAME" ]]; then
  BRANCH_NAME="$(default_branch "$ROLE")"
fi

WORKTREES_DIR="$REPO_PATH/.worktrees"
WORKTREE_PATH="$WORKTREES_DIR/$ROLE"

mkdir -p "$WORKTREES_DIR"

if git -C "$REPO_PATH" worktree list --porcelain | grep -F "worktree $WORKTREE_PATH" >/dev/null 2>&1; then
  echo "Worktree already exists: $WORKTREE_PATH"
  git -C "$WORKTREE_PATH" status --short --branch
  exit 0
fi

if git -C "$REPO_PATH" show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
  git -C "$REPO_PATH" worktree add "$WORKTREE_PATH" "$BRANCH_NAME"
else
  git -C "$REPO_PATH" worktree add -b "$BRANCH_NAME" "$WORKTREE_PATH" "$BASE_REF"
fi

cat <<EOF
Created Meridian worktree
- role: $ROLE
- repo: $REPO_PATH
- worktree: $WORKTREE_PATH
- branch: $BRANCH_NAME

Next steps:
  cd "$WORKTREE_PATH"
  git status --short --branch
EOF
