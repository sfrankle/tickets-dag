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
      api_115.json                    # one PR
      api_115_findings.json           # that PR's findings
      logs/                           # one file per run, timestamped
  locks/                              # one advisory lock per running ticket
```

Recorded log paths are relative to the store root, so moving the store does not strand them.
A store written by an older version was grouped by type (`prs/`, `findings/`, `logs/`); it is migrated into the layout above the first time this version opens it, and nothing is deleted in the process.
A file it cannot place — because something is already at the destination, or because it is not readable as a JSON document — stays where it is, and a PR whose ticket cannot be named is filed under `tickets/_unkeyed/`.
Either way the migration names it on stderr, since nothing that reads the store afterwards looks there.
A PR document written before the repo owner was dropped from these names is called `acme-api_115.json`; it is read where it lies rather than renamed, so both forms resolve.
`$TICKET_NO_MIGRATE` turns the migration off, for looking at an old store without rewriting it; every read then goes to the new layout, so the store reads as empty until it is migrated.

## Use

Two families of verb, and `ticket --help` shows which is which.
**Management** verbs are the engine's own and mean the same thing under every config.
**Stage** verbs are the engine's too, but every name they take as an argument comes from your config — they are listed in their own block at the foot of `--help`, and the names themselves are never in `--help` at all.

```bash
# management
ticket                            # the queue, most recently updated first
ticket ABC-123                    # one row: steps, next action, findings
ticket track ABC-123               # the repo comes from the summary
ticket track ABC-123 --repo acme/api            # ...or say it outright
ticket refresh                    # fetch, fast-forward, and re-read every row
ticket next ABC-123               # run whatever the resolver says is next
ticket next ABC-123 --pr 114      # ...against an older PR, from here on
ticket reset ABC-123 implement    # re-run a step and everything below it
ticket log ABC-123 implement      # what that step's last run wrote
ticket open ABC-123               # the PR in a browser (--pr for an older one)
ticket unlock ABC-123             # clear a lock a dead run left behind
ticket tui                        # the queue as a screen that stays put
ticket stages --list              # the steps and reviews this config declares
ticket config                     # the resolved config
ticket config --validate          # ...and whether it actually works

# stages — the second word is always the ticket key, the third a stage
ticket run ABC-123 spec           # run or re-run a named step
ticket skip ABC-123 describe      # walk past a step (or a review)
ticket release ABC-123 review-spec
ticket review ABC-123             # dispatch the next review
ticket collect ABC-123            # ingest reviews and comments not yet seen
ticket collect ABC-123 --recollect 5049842015   # read one collected source again
ticket findings ABC-123
ticket reviews ABC-123            # every review on the PR, ours and theirs
ticket fix ABC-123                # work the next finding, routed by effort
ticket fix ABC-123 --no-wait      # hand it off without waiting for the commit
                                  # (`next` takes --no-wait too, and waits by default)
ticket effort ABC-123 f02 hard    # override how a finding gets fixed
ticket attribute ABC-123 5049842015 docs-tests   # say which dispatch a source answered
ticket decide ABC-123 f03 "covered by ABC-140"
ticket --no-sync collect ABC-123  # skip the fetch, for working offline
```

**`--pr` selects; it does not override.**
A key can carry several PRs, and `--pr` says which one is being worked — on any verb that takes it, including `next`.
The choice sticks: the commands after it act on that PR too, until something moves the pointer again.
Nothing here records whether a PR is open, merged or abandoned, so which one to work is a person's call rather than something the engine guesses; the newest registration is only the default.
Two things move the pointer without being asked: a step that registers a PR selects it, because opening one is a statement about which PR is being worked, and a `reset` that drops a PR clears a pointer that named it.
A `--dry-run` resolves a `--pr` without moving anything — including a `--pr` the ticket has never had, which it reports rather than leaving for the real run to discover.
The move is written once the command it was given to has returned: one rejected for its own reasons, like an unknown review id, leaves every later run where it was.

**The key comes first, always.**
`run`, `skip` and `release` used to read `<step> <key>` while every other verb read `<key>` first, so `ticket skip ABC-123 evaluate` looked up a ticket named `evaluate` and answered "evaluate is not tracked" about a step `ticket show` had just called `next` (issue #12).
The old order is still accepted — with a note saying where the key moved — but only when the first word names no tracked ticket and the second one does, so it can never quietly pick the wrong ticket.

**A stage exists because config declares it.**
There is no per-key registration and never was: `track` registers a ticket, and every step and review in `config.yml` is available on it from that moment.
The store only records what has happened to a stage, which is why `show`, `next`, `run`, `skip`, `release`, `reset` and `log` all resolve a stage name against the config alone and all accept the same set.
`ticket stages --list` is that set.

**A repo you do not have to type.**
`ticket track ABC-123` with no `--repo` asks the tracker for the ticket's summary and reads the repo out of it, so a key is all a new ticket needs (issue #8).
The config says how: `repos.<repo>.aliases` names the other things a repo is called, `owner:` says which account a bare name belongs to, and `infer.repo.patterns` says what to look for in a summary.

```yaml
owner: acme

repos:
  acme/api:
    aliases: [API]

infer:
  repo:
    patterns:
      - "[{alias}]"
```

With that, `[API] harden the token check` tracks against `acme/api`.
Everything outside a placeholder is matched literally — `[` is a bracket someone typed, not a character class — and matching ignores case.
All three keys are optional; omit them and `track` behaves exactly as it always did.

**Nothing is guessed.**
A summary naming two repos records neither and names both; one naming none says so; a tracker that cannot answer says that.
Every one of those still writes the row and exits 0, because a ticket with no repo is one you can still `refresh`, retitle or point by hand, and the repo can be filled in later with `ticket track ABC-123 --repo acme/api`.
Inference never overwrites a repo already recorded, so re-tracking cannot move a ticket someone pointed by hand.

`--repo` takes an alias or a bare name as well as `owner/repo`, and means the same thing to every verb that accepts it.

## The screen

`ticket tui` is the queue as a screen that stays put and stays current (issue #28).
It exists for the four things the printed queue and `ticket ABC-123` cannot do: stop retyping the key into every verb, switch between tickets without re-running a command, start a step and keep working, and keep the view on screen instead of watching it scroll away.

```
+- tickets ------------------+- ABC-123  acme/api ----------------+
|                            | Fix auth token expiry              |
| >ABC-123 * Fix auth token  | PR: acme/api#115    2 open findings |
|  ABC-140   Rate limit gate |                                    |
|  ABC-155   Migrate store l | [x] evaluate      handoff          |
|                            | [x] review-spec   gate    released |
|                            | [>] implement     handoff running  |
|                            | [ ] draft-pr      script           |
|                            |                                    |
|                            | next: step implement               |
|                            +- log  implement-2026...Z.log ------+
|                            | > opening worktree acme/api        |
+----------------------------+------------------------------------+
 ENTER run implement    f 2 findings    o PR #115
 ready  ·  last sync 4m ago  ·  ? help
```

Left list, right detail, log below the detail.
The list is the queue, one line per ticket, scrolling as a viewport under the cursor so the selection is always visible.
The panel is 32% of the terminal clamped to 24-44 columns, so it grows with the terminal and never with the length of the titles in it; truncation is a stable prefix rather than an ellipsis that moves, and the full summary is always in the detail pane.
A row with no summary shows the repo instead, and a row with neither is just the key.

The right pane is `ticket KEY` plus running state, with steps and reviews in one pipeline in config order, since that is how the resolver walks them.
Each row shows a marker and **the word the store holds** — `released`, `skipped`, `failed`, `collected` — never a display vocabulary invented for the screen, which would drift from the engine's.
The log pane appears when the selected ticket is running or has a last run, auto-tails while running, and collapses with `L`.

**A key is the first letter of the CLI verb it calls**, so there is nothing new to learn.

```
ENTER  next        run whatever the resolver says is next
o      open        the PR
t      track       a new key
f      findings    for this ticket
R      refresh     network, hence the capital
p                  cycle which PR the detail pane inspects
w                  collapse the list to keys only
Tab                focus (or detail, when narrow)
/                  fuzzy search over key, repo and summary
b                  show only tickets that want attention
L                  collapse the log pane
j k ? q
```

`ENTER` rather than `n`, because `n` reads as "new" everywhere.
Safety comes from the contextual line always naming what `ENTER` will do before it is pressed: `ENTER run implement`, `ENTER dispatch correctness`, `ENTER collect`, `ENTER fix f03`.
At a gate `ENTER` does nothing and that line shows the literal command to copy, `ticket release ABC-123 review-spec`; releasing a gate from the screen is deliberately not here.
`b` is the only filter, and it is "does this want me now" rather than ready/blocked — those words do not survive contact with the resolver, since `collect` sounds blocked and is runnable while a gate sounds ready and is the actual stop.

**The TUI is a frontend.**
It never writes to the store.
Every mutation is a subprocess call to `ticket` itself, spawned with `start_new_session=True` so a twenty-minute handoff outlives the screen and survives the terminal closing, and the child takes `store.lock(key)` on its own — so mutual exclusion stays the engine's and there is never a second writer.
The child's output goes to `tickets/<KEY>/logs/spawn-<stamp>.err`, which is where an immediate crash announces itself.
Failure is read back from the store rather than from an exit code: the step records `failed`, the resolver returns it as next, and `ENTER` re-runs it with no special case.

One 1s timer polls mtimes across the store, and a tick that finds nothing changed does no work.
The network is touched by `R` alone.

Under 90 columns it is one pane at a time — list, `Tab` for detail, `Esc` back.
Under 20 rows the log pane does not auto-open.
Nothing ever scrolls horizontally, and everything survives 80x24.

`ticket tui` needs a terminal curses can drive; piped or under CI it says so and names `ticket show` and `ticket next` instead.

## Checking a config

`ticket config` prints the resolved config — store, models, worktrees, severities, the declared steps and reviews, the repos with overrides — and `ticket config --validate` reports only what is wrong with it.
Both exit non-zero when there is a problem, so `--validate` works in a pre-commit hook or CI.

It catches the things that only bite halfway through a run: a `prompt:` or `run:` that is not there, one that cannot be read, a `run:` that is not executable, and a `model:` that is not in `models:`.
Steps, reviews and both `fix:` routes are walked alike, so a `fix.easy.run` that is missing is caught now rather than the first time a finding is routed `easy`.
Anything that stops the file loading at all — a cycle in `needs:`, an unknown model alias, a repo override that strands a dependent — is reported the same way rather than raised as a traceback.

A `repos.<repo>.path` that is not a directory is reported as a warning instead, and does not change the exit code.
The distinction is who owns the missing thing: a prompt or a script is the config's own, so a missing one means the config is wrong, while a clone is the machine's, and one that has not been made yet — or a placeholder like the `~/code/api` the example ships — is the ordinary state of a config on a fresh machine.
Nothing is hidden by being a warning: both lists print, and anything that needs the checkout still fails when it reaches it, naming the path.
A step exits 127 with `No such file or directory` in its log; a `local` review and a hard fix stop with that path in the error.
The warning is the earlier telling of the same thing, not a substitute for it.
That is what lets `examples/config.yml` validate clean out of the box, which is the only reason to trust `--validate` at all.

**An unknown key is an error.**
A key this loader does not know, at the top level or under any block it does know, fails the load and names itself.
`tracker: {sumary: [...]}` used to load clean and do nothing, which is the worst of both (issues #6, #8).

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
Effort (`easy`/`hard`) says how contained the fix is: `easy` is handed to a script you declare, `hard` gets a local Claude session.
A blocking finding can be a one-line fix.
Effort is set by Haiku at ingestion and overridden with `ticket effort`; it is never derived from severity.

**What fixes a finding is yours.** `ticket fix` selects one finding and hands it off; it composes no comment and knows no fixer's protocol.

```yaml
fix:
  easy:
    run: input/scripts/fix-easy.sh   # site policy lives here
  hard:
    model: opus                      # defaults to defaults.model
    prompt: input/prompts/fix.md     # optional; there is a built-in one
    args: [--permission-mode, acceptEdits]
```

The script runs in the ticket's checkout with the finding in its environment — `TICKET_FINDING_ID`, `_REF`, `_TRAILER`, `_FILE`, `_SUMMARY`, `_BODY`, `_SEVERITY`, `_EFFORT`, alongside the usual `TICKET_KEY`, `TICKET_REPO`, `TICKET_PR`, `TICKET_WORKTREE` — and the whole finding as JSON on stdin.
The one in `examples/` asks a Claude GitHub bot with an `/edit` comment, which is one company's protocol; point `run:` at your own and nothing in the engine changes.
Leave `fix.easy` out and an easy finding is refused rather than guessed at.
`hard` is a local session, so `args:` is where agent mode goes; without it the session can read but not write, and every hard fix ends in "changed nothing".

**One run, one finding.** `ticket fix` acts on a single finding and then waits for its commit, printing where it has got to — a remote fixer runs one action per PR and silently drops a second dispatch, so handing out a second finding while the first is in flight loses both.
A finding already with a fixer is refused until its commit lands, or until `--force` sends it again.
`--no-wait` returns as soon as the handoff is made; a `hard` fix never waits, because it commits locally and the remote head does not move.

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

**`collect` does not wait, and a source can be read again.**
It reads what is on the PR at the moment it runs and returns; a dispatched review whose result has not been recorded is named in the output as still outstanding.
That covers two cases, and only one of them clears itself: a review that has not posted yet lands when you run `collect` again, while one that posted in a form the script parser could not read was recorded as none of ours and no number of `collect` runs will attach it to its dispatch.
`ticket attribute KEY <source-id> <review>` is the second case's remedy — see below — and `ticket skip <review>` is the way to drop the dispatch instead.
A collected source is normally skipped on the next run, with two exceptions.
A source that produced no findings is re-parsed every time, for free — the re-parse stops at the script parser and never falls back to Haiku — so a parser that has since learned to read that body recovers findings that were previously stranded behind their own source id.
`ticket collect KEY --recollect <source-id>` (repeatable) forces a full re-read of a named source, Haiku included, which is the case a source that already produced findings needs.
A re-read recovers findings; it does not re-decide attribution.
Which dispatch a source answered is settled on the first read and kept from then on, `null` included — a record that claimed nothing stays null through a forced re-read, because attribution is positional and the record did not keep the position, so asking again would hand back whatever is outstanding *now* and consume a dispatch that body cannot have answered.
Neither can duplicate anything: a re-read is deduplicated against the findings already open on the PR, and it updates the source's existing collection record rather than adding a second one.
That record carries a `reread_at` alongside its original `at`, so the first read and the last re-read of a source are both still readable.
A re-read that recovers nothing is not recorded as work at all: it prints nothing and rewrites nothing, because a record left empty is exactly the record that gets re-read again next run.
`--recollect` naming a source that is not on the PR is an error and exits non-zero — the sources that were there are still collected first.

**Attribution the machine cannot recover, you can supply.**
`ticket attribute KEY <source-id> <review>` writes the attribution a first read could not make, which is what unsticks a `docs-tests` dispatch answered by a body that fell back to Haiku: without it that dispatch is uncollected forever and `next` keeps asking for a `collect` that can never clear.
The source id it takes is GitHub's, and `ticket reviews KEY --json` dumps the PR document that carries it.
`none` in place of a review id is the other direction, for a record that took a slot it should not have.
The change reaches the findings that source minted as well as the collection record, so `ticket findings` and `ticket reviews` do not end up disagreeing about where a finding came from.
A review is a slot per dispatch: attributing to one whose dispatches are all collected is an error, as is naming a review that was never dispatched on the PR.

**GitHub is a contract, the tracker is not.** PRs, reviews and comments go through the `gh` CLI and a dispatched `bot` review rides a Claude GitHub bot's `/review` comment protocol; there is no forge abstraction and none is planned.
How an `easy` finding gets fixed is not part of that contract — it is the script `fix.easy.run` names.
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

Resolution is a `git log` scan for `Finding: <ref>` trailers — one commit per finding, zero tokens, and it works for a remote fixer's commits too.
The ref is a hash of the PR reference and the finding id, not the id: ids are minted per PR and restart at `f01`, so scanning a branch for `Finding: f01` used to match an unrelated PR's commit and close a finding nobody had fixed.
It also keeps a store-local handle out of a public comment.

**Trust boundary.** Findings are minted from PR reviews and PR comments, so their text is written by whoever can comment on the PR.
That text is passed to whatever `fix.easy.run` names — the example script splices it into an `/edit` instruction for the gh bot — and into the prompt of the local Claude session that a `hard` fix runs, after which this commits whatever the session changed.
A handoff's stdout is a control channel too: `ticket-pr:` registers a PR and `ticket-worktree:` sets the directory later commands run in.
Run this against repos whose commenters you trust.

Design notes, the decision log, and rejected alternatives: [v1: release notes](https://github.com/sfrankle/tickets-dag/wiki/v1:-release-notes) in the wiki.

## Tests

```bash
uv run pytest
```

The suite runs offline.
Everything that shells out is tested against fake `gh`/`git`/`claude` binaries on `PATH`.
