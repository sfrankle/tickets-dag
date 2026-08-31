# tickets-dag

A lightweight, config-driven DAG for driving a Jira ticket to a merged PR.
Steps and reviews live in YAML; the CLI verb set does not grow when they do.

## Install

```bash
uv tool install .
```

## Configure

```bash
mkdir -p ~/.ticket
cp examples/config.yml ~/.ticket/config.yml
cp -r examples/prompts examples/scripts ~/.ticket/
```

Edit `~/.ticket/config.yml`. `$TICKET_CONFIG` overrides the location and
`$TICKET_STORE` overrides the store, for throwaway or test runs. The store
defaults to whichever directory the config file was found in, so pointing
`$TICKET_CONFIG` at another config moves the state along with it.

## Use

```bash
ticket                            # the queue
ticket ABC-123                    # one row: steps, next action, findings
ticket track ABC-123 --repo acme/api
ticket refresh                    # fetch, fast-forward, and re-read every row
ticket next ABC-123               # run whatever the resolver says is next
ticket run spec ABC-123           # run or re-run a named step
ticket skip describe ABC-123      # walk past a step (or a review)
ticket release review-spec ABC-123
ticket review ABC-123             # dispatch the next review
ticket collect ABC-123            # ingest reviews and comments not yet seen
ticket findings ABC-123
ticket reviews ABC-123            # every review on the PR, ours and theirs
ticket fix ABC-123                # work the next finding, routed by effort
ticket fix ABC-123 --all          # work the whole queue, waiting between each
ticket open ABC-123               # the PR in a browser
ticket effort ABC-123 f02 hard     # override how a finding gets fixed
ticket decide ABC-123 f03 "covered by ABC-140"
ticket reset ABC-123 implement    # re-run a step and everything below it
ticket --no-sync collect ABC-123  # skip the fetch, for working offline
```

## How it works

State records what has happened. What to do next is recomputed on every call by
`resolve.py`, a pure function of stored state — so there is no cursor to keep in
sync, and skipping is a flag rather than a state transition.

The engine executes exactly three kinds of step: a **script** (`run:`), a
**gate** (`gate: true`, advanced by `ticket release`), and a **handoff**
(`model:` + `prompt:` + optional `args:` for agent mode, a Claude session).
Everything else is a script or a prompt the config points at, so adding a step
is a YAML edit.

Steps run **in the ticket's checkout**. `worktrees.enabled` decides whether that
is a worktree of its own under `worktrees.root` or the clone itself on
`worktrees.branch`; `worktree.sh` reads the setting and announces the path it
chose. The engine only ever learns the path.

**Everything fetches.** `git fetch --prune`, and a fast-forward when one is
possible, runs before every command that reads a checkout. The gh bot commits on
the remote, so a stale checkout would mean trailer scanning never closes a
finding. It never rebases, never merges non-fast-forward, and never touches
uncommitted work — it says what it could not do and carries on. `--no-sync`
opts out.

**Severity and effort are different questions.** Severity (🔴 blocking,
🟡 maintenance, 🔵 architecture) says how important a finding is. Effort
(`easy`/`hard`) says how contained the fix is: `easy` goes back to the gh Claude
bot as an `/edit` comment, `hard` gets a local Claude session. A blocking finding
can be a one-line fix. Effort is set by Haiku at ingestion and overridden with
`ticket effort`; it is never derived from severity. The local session a `hard`
fix runs is configured under `fix:` — `model:` (defaults to `defaults.model`)
and `args:`, which is where agent mode goes. Without it the session can read
but not write, and every hard fix ends in "changed nothing".

Resolution is a `git log` scan for `Finding: fNN` trailers — one commit per
finding, zero tokens, and it works for the bot's commits too.

**Trust boundary.** Findings are minted from PR reviews and PR comments, so
their text is written by whoever can comment on the PR. That text is spliced
into the `/edit` instruction sent to the gh bot, and into the prompt of the
local Claude session that a `hard` fix runs — after which this commits whatever
the session changed. A handoff's stdout is a control channel too: `ticket-pr:`
registers a PR and `ticket-worktree:` sets the directory later commands run in.
Run this against repos whose commenters you trust.

Design notes, the decision log, and rejected alternatives:
[v1: notes](https://github.com/sfrankle/tickets-dag/wiki/v1:-notes) in the wiki.

## Tests

```bash
uv run pytest
```

The suite runs offline. Everything that shells out is tested against fake
`gh`/`git`/`claude` binaries on `PATH`.
