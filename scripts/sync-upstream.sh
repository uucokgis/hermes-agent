#!/usr/bin/env bash
set -euo pipefail

repo_dir="${1:-/home/umut/Hermes-Agent}"

if [ ! -d "$repo_dir/.git" ]; then
  echo "Not a git repository: $repo_dir" >&2
  exit 1
fi

cd "$repo_dir"

current_branch="$(git branch --show-current)"

if [ "$current_branch" != "main" ]; then
  echo "Current branch is '$current_branch'. Switch to 'main' before syncing." >&2
  exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Worktree is dirty. Commit, stash, or clean changes before syncing." >&2
  git status --short
  exit 3
fi

echo "Fetching remotes..."
git fetch upstream
git fetch origin

echo "Merging upstream/main into main..."
git merge --ff-only upstream/main

echo "Pushing updated main to origin..."
git push origin main

echo
echo "Sync complete."
git log --oneline --decorate -n 3
