#!/bin/sh
# Hand one `easy` finding to the Claude GitHub bot, as an /edit comment.
#
# This is site policy, not engine behaviour: `/edit` is one company's bot protocol.
# `ticket fix` selects the finding and runs whatever `fix.easy.run:` points at, so another shop swaps this file for its own — a queue, a patch mailer, a different bot — and changes nothing in the tool.
#
# The engine runs this inside the ticket's checkout and passes the finding in the environment: TICKET_FINDING_ID, _REF, _TRAILER, _FILE, _SUMMARY, _BODY, _SEVERITY, _EFFORT, alongside the usual TICKET_KEY, TICKET_REPO, TICKET_PR and TICKET_WORKTREE.
# The whole finding also arrives as JSON on stdin.
#
# TICKET_FINDING_ID stays here, in the environment.
# It is a store-local handle and it has no business in a public comment; the comment says what is wrong, and TICKET_FINDING_TRAILER is the opaque per-PR ref the engine scans commit messages for to know the work landed.
set -eu

where=""
if [ -n "${TICKET_FINDING_FILE:-}" ]; then
    where=" in \`${TICKET_FINDING_FILE}\`"
fi

body=$(
    cat <<EOF
/edit ${TICKET_FINDING_SUMMARY}${where}

${TICKET_FINDING_BODY}

Make the smallest change that addresses this, and nothing else. Make one commit
for it, and include this trailer in the commit message, on its own line at the
end:

    ${TICKET_FINDING_TRAILER}
EOF
)

gh pr comment "${TICKET_PR#*#}" --repo "$TICKET_REPO" --body "$body"
echo "asked the bot to fix ${TICKET_FINDING_REF} on ${TICKET_PR}"
