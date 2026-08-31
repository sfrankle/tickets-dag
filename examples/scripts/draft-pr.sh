#!/bin/sh
# Push the branch and open a draft PR, then tell the engine its reference.
#
# The engine already runs this inside $TICKET_WORKTREE, so there is nothing to
# cd to. `-u` sets the upstream, which trailer scanning needs: the gh bot's
# commits arrive on the remote and are found through @{upstream}.
set -eu

git push -u origin HEAD

number=$(gh pr create --draft --fill --repo "$TICKET_REPO" | sed 's|.*/||')
echo "ticket-pr: ${TICKET_REPO}#${number}"
