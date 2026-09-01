#!/bin/sh
# Put the ticket on a branch and tell the engine which checkout to work in.
#
# TICKET_USE_WORKTREES=1  -> a checkout of its own under TICKET_WORKTREE_ROOT
# TICKET_USE_WORKTREES=0  -> the clone itself, on TICKET_BRANCH
#
# Either way the last line is what the engine reads.
set -eu

repo_path="${TICKET_REPO_PATH:?set repos.<repo>.path in ~/.ticket/config.yml}"
branch="${TICKET_BRANCH}"

cd "$repo_path"
git fetch --prune --quiet

if [ "${TICKET_USE_WORKTREES}" = "0" ]; then
  git checkout -q "$branch" 2>/dev/null || git checkout -q -b "$branch"
  echo "ticket-worktree: ${repo_path}"
  exit 0
fi

path="${TICKET_WORKTREE_ROOT}/${TICKET_KEY}"
if [ ! -d "$path" ]; then
  mkdir -p "${TICKET_WORKTREE_ROOT}"
  git worktree add -b "$branch" "$path" 2>/dev/null \
    || git worktree add "$path" "$branch"
fi

echo "ticket-worktree: ${path}"
