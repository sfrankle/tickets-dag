# tickets-dag

A lightweight, config-driven DAG for driving a tracker ticket to a merged PR.
Steps and reviews live in YAML; the CLI verb set does not grow when they do.

## Install

```bash
uv tool install .
```

From a clone — the config, prompts and scripts `Configure` copies live in `examples/` and are not packaged in the wheel.

## Configure

```bash
mkdir -p ~/.ticket
cp examples/config.yml ~/.ticket/config.yml
cp -r examples/input ~/.ticket/
```

Edit `~/.ticket/config.yml`.
`$TICKET_CONFIG` overrides the location and `$TICKET_STORE` overrides the store, for throwaway or test runs.
The store defaults to whichever directory the config file was found in, so pointing `$TICKET_CONFIG` at another config moves the state along with it.

It is grouped by ticket key: everything one ticket knows sits in one directory.

```
~/.ticket/
  config.yml
  input/
    prompts/                          # what handoffs and reviews are given
    scripts/                          # what script steps run
  tickets/
    ABC-123/
      state.json                      # the ticket: steps, PRs, worktree
      acme-api_115.json               # one PR
      acme-api_115_findings.json      # that PR's findings
      logs/                           # one file per run, timestamped
  locks/                              # one advisory lock per running ticket
```

Recorded log paths are relative to the store root, so moving the store does not strand them.
A store written by an older version was grouped by type (`prs/`, `findings/`, `logs/`); it is migrated into the layout above the first time this version opens it, and nothing is deleted in the process.
A file it cannot place — because something is already at the destination, or because it is not readable as a JSON document — stays where it is, and a PR whose ticket cannot be named is filed under `tickets/_unkeyed/`.
Either way the migration names it on stderr, since nothing that reads the store afterwards looks there.
`$TICKET_NO_MIGRATE` turns the migration off, for looking at an old store without rewriting it; every read then goes to the new layout, so the store reads as empty until it is migrated.

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

State records what has happened.
What to do next is recomputed on every call by `resolve.py`, a pure function of stored state — so there is no cursor to keep in sync, and skipping is a flag rather than a state transition.

The engine executes exactly three kinds of step: a **script** (`run:`), a **gate** (`gate: true`, advanced by `ticket release`), and a **handoff** (`model:` + `prompt:` + optional `args:` for agent mode, a Claude session).
Everything else is a script or a prompt the config points at, so adding a step is a YAML edit.

Steps run **in the ticket's checkout**.
`worktrees.enabled` decides whether that is a worktree of its own under `worktrees.root` or the clone itself on `worktrees.branch`; `worktree.sh` reads the setting and announces the path it chose.
The engine only ever learns the path.

**Everything fetches.** `git fetch --prune`, and a fast-forward when one is possible, runs before every command that reads a checkout.
The gh bot commits on the remote, so a stale checkout would mean trailer scanning never closes a finding.
It never rebases, never merges non-fast-forward, and never touches uncommitted work — it says what it could not do and carries on.
`--no-sync` opts out.

**Severity and effort are different questions.** Severity says how important a finding is.
The names and their markers are yours, declared under `severities:` — 🔴 blocking, 🟡 maintenance, 🔵 architecture is what the example config ships and what you get if you leave the key out, not a set the engine believes in.
Config order is importance order, and one entry is `default:`, the severity a Haiku-parsed finding falls back to when it names one you never declared.
Effort (`easy`/`hard`) says how contained the fix is: `easy` goes back to the gh Claude bot as an `/edit` comment, `hard` gets a local Claude session.
A blocking finding can be a one-line fix.
Effort is set by Haiku at ingestion and overridden with `ticket effort`; it is never derived from severity.
The local session a `hard` fix runs is configured under `fix:` — `model:` (defaults to `defaults.model`) and `args:`, which is where agent mode goes.
Without it the session can read but not write, and every hard fix ends in "changed nothing".

**The review format is the tool's contract.** A review this tool dispatches is expected to answer in one `<details>` block per severity — keyed by that severity's configured marker in the `<summary>` — with each finding naming its file in backticks, `None.` in an empty block, and a closing `**Verdict:**` line.
That shape parses to findings for free.
The grammar is deliberately loose about the rest, because the shape is one several bots land on rather than one bot's exact bytes: attributes on the tag (`<details open>`) are fine, a finding is either a `*`/`-` bullet or a paragraph opening with a bolded lead, and a trailing summary table is counted as decoration rather than turned into findings.
A fenced block belongs to the finding it sits in, so a suggested diff stays one finding rather than becoming a bullet per changed line; a section written entirely as a table is one we have no rule for and goes to Haiku.
A `<details>` folded inside a section is read as part of it rather than as the end of it, so evidence a bot tucks away does not cost you the findings under it.
`summary` is derived from the finding — the bolded lead where there is one, otherwise the first sentence, capped — so it stays a line you can print in a row while `body` keeps the full text.
A file is recorded only when the text names something that is actually a path; a bare symbol like `logger.warn` records no file, because a wrong file is worse than none.
Both fields feed the dedupe fingerprint, so on the first collect after upgrading from a store parsed by the old grammar, still-open script-parsed findings fingerprint differently than they did when stored and are re-added once.
It settles after that collect; resolve or wontfix the stale copies.
Anything else — a human comment, a bot whose output you do not control, format drift — is split into findings by Haiku instead, so nothing is lost by a reviewer that will not conform.
A body carrying our markers whose sections we cannot split counts as *not ours* and goes to Haiku too; recording it as ours with zero findings is how a review gets lost silently.
Change `severities:` and both paths follow: the script parser looks for the new markers, and the Haiku prompt asks for the new names.
For a bot that writes something genuinely different, an optional `parse.sources` block overrides the built-in grammar for that author alone (`details:`, `bullet:`, `lead:`, `file:`, `verdict:`, each optional); it exists so a new bot is onboarded without a release, not as the normal way to parse.
A profile is validated at load under the same flags it runs under, and `examples/config.yml` states which those are; `file:` is the one pattern taken at its word, recording its capture with none of the path checks the built-in applies.
The example review prompts under `examples/input/prompts/reviews/` state the format in full, and each one states it on its own: a `bot` review is posted as a PR comment and a `local` review runs in the ticket's checkout, so neither can read a sibling prompt file.

**GitHub is a contract, the tracker is not.** PRs, reviews and comments go through the `gh` CLI, and an `easy` fix rides a Claude GitHub bot's `/review` and `/edit` comment protocol; there is no forge abstraction and none is planned.
The tracker is the other way round — the engine never learns what one is.
Set `tracker.summary` to an argv list and `refresh` runs it, substitutes `{key}`, and keeps the first line of stdout as the summary:

```yaml
tracker:
  summary: [jira, issue, view, "{key}", --plain]
```

Leave it out and there is no lookup.
A command whose binary is missing is skipped the same way — the summary is a convenience, and the rest of `refresh` still has to work.

**A key is a path.** It becomes a state file, a lock file, a log directory and a worktree directory, so the only shape the engine enforces is one safe path segment: no whitespace, no separator, no leading `-`.
`ABC-123`, `4471` and `add-tracker-block` are all keys.
Set `key_pattern:` to a regex if you want the stricter rule your tracker implies.
A bare key is shorthand for `show`, and a verb always wins that ambiguity — `ticket refresh` is the verb even if you have a ticket keyed `refresh`.

Resolution is a `git log` scan for `Finding: fNN` trailers — one commit per finding, zero tokens, and it works for the bot's commits too.

**Trust boundary.** Findings are minted from PR reviews and PR comments, so their text is written by whoever can comment on the PR.
That text is spliced into the `/edit` instruction sent to the gh bot, and into the prompt of the local Claude session that a `hard` fix runs — after which this commits whatever the session changed.
A handoff's stdout is a control channel too: `ticket-pr:` registers a PR and `ticket-worktree:` sets the directory later commands run in.
Run this against repos whose commenters you trust.

Design notes, the decision log, and rejected alternatives: [v1: release notes](https://github.com/sfrankle/tickets-dag/wiki/v1:-release-notes) in the wiki.

## Tests

```bash
uv run pytest
```

The suite runs offline.
Everything that shells out is tested against fake `gh`/`git`/`claude` binaries on `PATH`.
