#!/usr/bin/env bash
set -euo pipefail

repo_dir="${1:-$(pwd)}"
main_branch="${MAIN_BRANCH:-main}"
upstream_remote="${UPSTREAM_REMOTE:-upstream}"
origin_remote="${ORIGIN_REMOTE:-origin}"
sync_strategy="${SYNC_STRATEGY:-merge}"

die() {
  echo "Error: $*" >&2
  exit 1
}

require_remote() {
  local remote_name="$1"

  if ! git remote get-url "$remote_name" >/dev/null 2>&1; then
    die "Remote '$remote_name' is not configured. Add it first with: git remote add $remote_name <url>"
  fi
}

branch_exists() {
  local ref="$1"
  git show-ref --verify --quiet "refs/remotes/$ref"
}

validate_strategy() {
  case "$sync_strategy" in
    merge|rebase)
      ;;
    *)
      die "Unsupported SYNC_STRATEGY '$sync_strategy'. Use 'merge' or 'rebase'."
      ;;
  esac
}

if [ ! -d "$repo_dir/.git" ]; then
  die "Not a git repository: $repo_dir"
fi

cd "$repo_dir"

current_branch="$(git branch --show-current)"

if [ "$current_branch" != "$main_branch" ]; then
  die "Current branch is '$current_branch'. Switch to '$main_branch' before syncing."
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Worktree is dirty. Commit, stash, or clean changes before syncing." >&2
  git status --short
  exit 2
fi

require_remote "$upstream_remote"
require_remote "$origin_remote"
validate_strategy

echo "Fetching remotes..."
git fetch "$upstream_remote" "$main_branch"
git fetch "$origin_remote" "$main_branch"

upstream_ref="$upstream_remote/$main_branch"
origin_ref="$origin_remote/$main_branch"

branch_exists "$upstream_ref" || die "Remote branch '$upstream_ref' does not exist."
branch_exists "$origin_ref" || die "Remote branch '$origin_ref' does not exist."

local_sha="$(git rev-parse HEAD)"
origin_sha="$(git rev-parse "$origin_ref")"

if [ "$local_sha" != "$origin_sha" ]; then
  die "Local '$main_branch' is not aligned with '$origin_ref'. Run 'git pull --ff-only $origin_remote $main_branch' or resolve local divergence first."
fi

if git merge-base --is-ancestor "$upstream_ref" "$origin_ref"; then
  echo "'$origin_ref' already contains '$upstream_ref'. Nothing to sync."
  git log --oneline --decorate -n 3
  exit 0
fi

if git merge-base --is-ancestor "$origin_ref" "$upstream_ref"; then
  echo "Fast-forwarding '$main_branch' to '$upstream_ref'..."
  git merge --ff-only "$upstream_ref"
else
  echo "'$origin_ref' and '$upstream_ref' have diverged."
  echo "Preserving your commits while incorporating upstream changes with strategy: $sync_strategy"

  if [ "$sync_strategy" = "rebase" ]; then
    echo "Rebasing your '$main_branch' commits onto '$upstream_ref'..."
    git rebase "$upstream_ref"
  else
    echo "Merging '$upstream_ref' into '$main_branch'..."
    git merge --no-edit "$upstream_ref"
  fi
fi

echo "Pushing updated '$main_branch' to '$origin_ref'..."
git push "$origin_remote" "$main_branch"

echo
echo "Sync complete."
git log --oneline --decorate -n 5
