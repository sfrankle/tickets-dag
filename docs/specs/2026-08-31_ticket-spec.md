# ticket — spec

Date: 2026-08-31
Status: design agreed, not implemented

## 1. What this is

A lightweight, config-driven DAG runner for driving a Jira ticket from evaluation
to a merged PR. Steps and reviews are declared in YAML so they can be reordered,
added, or skipped without touching code.

Two goals shape every decision below:

1. **Save Claude tokens.** A deterministic script is preferred over a model call
   everywhere a script can decide. Models are used only where judgement is
   genuinely required, and the cheapest adequate model is used when they are.
2. **Structure as configuration, not code.** Adding a step or a review is a YAML
   edit. The CLI verb set does not grow when the config does.

## 2. Model

### Units

- A **ticket** is the unit of work, keyed by Jira key (`^[A-Z][A-Z0-9]*-[0-9]+$`).
- A ticket **owns one or more PRs**. Steps belong to the ticket; reviews and
  findings belong to a PR, because a finding is about a diff.
- A **finding** is one actionable item, sourced from any review, any PR comment,
  or written directly by a local review session.

### The three step kinds

The engine knows how to execute exactly three things. Everything else is a shell
script the config points at.

| Kind | Declared by | Behaviour |
| --- | --- | --- |
| script | `run: <path>` | Run the script. Non-zero exit fails the step. |
| gate | `gate: true` | Park. Advances only on `ticket release <step> <KEY>`. |
| handoff | `model:` + `prompt:` | Run a Claude session with that prompt and model. |

Adding a step of any of these kinds requires no engine change.

### `next` is derived, never stored

State records what has *happened* (step outcomes, dispatched reviews, collected
reviews, finding statuses). What to do next is recomputed on every call. There is
no cursor to keep in sync, and skipping is a flag the resolver honours rather
than a state transition.

Resolution order, first match wins (**reordered by decision #20** — steps now
come before reviews; the original order is kept below, struck through, because
the reasoning still reads):

1. A dispatched review whose result has not been collected → `collect`.
2. An open finding on the active PR → `fix`.
3. ~~An undispatched, unskipped review whose predecessors are done, in `order` → `review`.~~
   The first step whose `needs` are satisfied and which is neither done nor skipped.
4. ~~The first step whose `needs` are satisfied and which is neither done nor skipped.~~
   An undispatched, unskipped review whose predecessors are done, in `order` → `review`.
5. Nothing → the ticket is at rest; print why.

A runnable step outranks dispatching a new review, so a step declared after
`draft-pr` — `describe` is the obvious one — runs where the config puts it
rather than after the whole review cycle drains. Collect and fix stay ahead of
it: both concern the diff already on the PR.

Rules 1, 2 and 4 only apply once the ticket has a PR, which means the moment a
PR is registered on the ticket — not the moment a PR document is written, since
that only happens on the first dispatch. Before `draft-pr`, the resolver falls
straight through to the step rule, so the pre-PR walk is a plain step sequence.

## 3. Configuration

### Location

- Config file: `$TICKET_CONFIG`, else `~/.ticket/config.yml`.
- Store: the `store:` key in config, else `~/.ticket`. `$TICKET_STORE` overrides
  both, for throwaway or test runs.

Config is central and serves every repo. Per-repo differences are expressed as
overrides under `repos:`, not as separate files.

### Shape

```yaml
store: ~/.ticket

models:
  opus: claude-opus-5                     # alias, so a new release is a one-line edit
  sonnet: claude-sonnet-5
  haiku: claude-haiku-4-5-20251001

defaults:
  model: opus

sync: true                                # fetch + fast-forward before anything
                                          # that reads a checkout (decision #22)

worktrees:                                # decision #23
  enabled: true                           # false: work in the clone on a branch
  root: ~/worktrees
  branch: "{key}"                         # {key} {repo} {owner} {name}

steps:
  - id: evaluate
    model: opus
    prompt: prompts/evaluate.md

  - id: spec
    model: opus
    prompt: prompts/spec.md
    needs: [evaluate]

  - id: review-spec
    gate: true
    needs: [spec]

  - id: plan
    model: opus
    prompt: prompts/plan.md
    needs: [review-spec]

  - id: review-plan
    gate: true
    needs: [plan]

  - id: worktree
    run: scripts/worktree.sh
    needs: [review-plan]

  - id: implement
    model: opus
    prompt: prompts/implement.md
    needs: [worktree]
    args: [--permission-mode, acceptEdits]   # extra claude argv: agent mode

  - id: draft-pr
    run: scripts/draft-pr.sh
    needs: [implement]

  - id: describe
    model: haiku
    prompt: prompts/describe.md
    needs: [draft-pr]

reviews:                                  # only the AI reviews *we* kick off
  - id: docs-tests
    order: 1
    dispatch: bot
    prompt: prompts/reviews/docs-tests.md

  - id: architecture
    order: 2
    dispatch: local
    model: opus
    prompt: prompts/reviews/architecture.md

  - id: security
    order: 3
    dispatch: local
    model: opus
    prompt: prompts/reviews/security.md

repos:
  acme/api:
    path: ~/code/api                      # the clone worktrees are added from
    reviews: [docs-tests]                 # this repo runs only one of ours
  acme/infra:
    path: ~/code/infra
    steps:
      skip: [describe]
```

### Notes on the schema

- **Every review carries a `prompt`, both transports.** `dispatch: bot` posts a
  PR comment beginning `/review <id>` with the prompt file's contents inside a
  `<details>` block. `dispatch: local` runs a Claude agent session with the same
  prompt. Moving a review between transports is a one-word edit — which matters,
  since migrating local reviews to the gh bot is in progress and the bot's
  results have so far been less thorough.
- **`reviews:` lists only reviews we dispatch.** Required repo bots (e.g.
  `oplane-bot`'s security review), other required agent reviews, and human
  reviews are never listed. They are not `unknown` and not an error — they are
  simply other sources of findings, handled in §4.
- **Models are named by alias.** `opus`, not `opus-5`. The alias table is the only
  place a model ID appears, so a new release is one edit.
- **Repo overrides are shallow.** `reviews:` replaces the list; `steps.skip:`
  subtracts from it. No deep merge, no inheritance chains. A `steps.skip:` that
  strands a dependent — skipping a step another step `needs` — is rejected at
  load, because the step is *removed* rather than marked satisfied and the row
  would otherwise go silently to rest. (`ticket skip` is the other thing: it
  marks a step satisfied for one ticket.)
- **Steps run in the ticket's checkout**, which is whatever `worktree.sh`
  announced with `ticket-worktree:`, else `repos.<repo>.path`, else the config
  directory. `worktrees.enabled` decides whether that is a worktree of its own
  or the clone on a branch; the engine only ever learns the path, so the choice
  lives in the script and the config (decision #23).
- **`sync: true` fetches early and often** (decision #22). `git fetch --prune`
  and a fast-forward where one is possible, before every command that reads a
  checkout. The gh bot commits on the remote, so without this a trailer scan
  never closes a finding. It never rebases, never merges non-fast-forward, and
  never touches uncommitted work. `--no-sync` opts out.
- **`args:` on a step or review** is appended to the `claude` argv. Agent mode
  lives here; a handoff that has to write files needs it. Prompts themselves go
  on stdin, never argv (decision #21).

## 4. Reviews, findings, fixes

This is the part that already works today and must keep working: post a review
from a saved prompt, get the review off the PR, post a comment asking the bot to
fix the easy findings.

### Dispatch

`ticket review <KEY> [id]` fires one review. With no id, the next undispatched,
unskipped review in `order`. Dispatch is recorded against the PR with the head
sha at the time of firing.

Bot reviews post:

```
/review docs-tests
<details>
{contents of prompts/reviews/docs-tests.md}
</details>
```

### Collection

`ticket collect <KEY>` fetches PR reviews and PR comments via `gh api`, and
ingests anything not already seen. Dedupe is on the GitHub review/comment id, so
collection is idempotent and safe to run repeatedly.

Collection is deliberately its own verb rather than a `--wait` flag on `review`,
because reviews that arrive without us dispatching them — humans, `oplane-bot`,
other required agents — must also be collectable.

### Parsing a review into findings

The AI reviews we trigger emit a known format (see `example-review.md`):

- One `<details>` block per severity, keyed by the emoji in `<summary>`:
  🔴 blocking, 🟡 maintenance, 🔵 architecture.
- Findings are the top-level `*` bullets inside a block.
- `None.` or `No findings.` means the block is empty.
- The body ends with a `**Verdict:**` line.

A script parses this. It is treated as a successful parse only if the body
contains at least one recognised `<details>`/`<summary>` pair **and** a
`**Verdict:**` line. Anything else — a human comment, `oplane-bot`, an unexpected
format drift — falls back to Haiku, which is asked to split the body into
findings with severity and file. Cost is therefore zero on the common path and
small on the uncommon one.

Parse failures are never silent: the fallback records `parsed_by: haiku` on each
finding so format drift is visible in the store.

### Finding identity

Findings get a short sequential id per PR: `f01`, `f02`, … Ids are stable once
assigned, human-typable, and never reused. Each finding records the source it
came from (review id, GitHub comment id, or `local`).

### Effort and routing

`fix` routes on the finding's effort rather than accepting one as an argument. An
`easy` finding goes back to the gh bot as an `/edit` comment and the bot commits;
a `hard` one gets a local Claude session and the script commits. Letting a caller
send a hard finding to `/edit` fails quietly — it produces a minimal diff that
misses the point — so the effort is a property of the finding, not of the call.

Effort is `easy` or `hard`: `easy` means the gh bot can make the change from an
`/edit` comment, `hard` means it needs a local Claude session. It is a question
about how contained the fix is, and it is **never derived from severity** —
severity says how important a finding is, and a 🔴 can be a one-line fix while a
🔵 can be a rewrite. Haiku sets it at ingestion (decision #16); a finding it
cannot judge is stored with `effort: null` and refused by `fix` until
`ticket effort <KEY> <id> hard` sets one by hand (decision #18).

### Ordering constraint on fixes

Easy findings go out one at a time, each waiting on the PR head sha to move,
because the bot runs one action per PR and silently drops a second dispatch.
Remote work runs before local work so a local commit never lands on a head the
bot then moves past.

### Resolution

**One commit per finding.** Every fix commit carries a trailer:

```
Finding: f07
```

Resolution is then a `git log` scan for trailers — deterministic, zero tokens,
and it works for the bot's commits too, provided the `/edit` comment instructs
the bot to include the trailer.

A finding can also be closed without a commit via `ticket decide <KEY> <id>`,
which records `wontfix` plus a reason. Nothing else infers resolution.

## 5. Store

Plain JSON, one file per concern, under the configured store path.

```
~/.ticket/
  config.yml
  tickets/ABC-123.json
  prs/acme-api-115.json
  findings/acme-api-115.json
  logs/ABC-123/<step>-<timestamp>.log
```

### `tickets/<KEY>.json`

```json
{
  "key": "ABC-123",
  "repo": "acme/api",
  "summary": "Retry environment assembly on API failure",
  "prs": ["acme/api#115"],
  "steps": {
    "evaluate": { "status": "done", "at": "2026-08-31T09:02:11Z" },
    "spec":     { "status": "done", "at": "2026-08-31T09:40:03Z" },
    "review-spec": { "status": "released", "at": "2026-08-31T10:15:00Z" },
    "describe": { "status": "skipped", "reason": "docs-only change" }
  },
  "tracked": true
}
```

Step status is one of `done`, `failed`, `skipped`, `released` (gates only).
Absent means not yet run.

### `prs/<owner>-<repo>-<n>.json`

```json
{
  "pr": "acme/api#115",
  "key": "ABC-123",
  "head": "9c1f0ab",
  "dispatched": [
    { "review": "docs-tests", "at": "...", "head": "3a7bd21", "transport": "bot" }
  ],
  "collected": [
    { "source_id": "PRR_kwabc", "review": "docs-tests", "at": "...", "findings": ["f01", "f02"] },
    { "source_id": "IC_kwxyz", "review": null, "author": "oplane-bot", "at": "...", "findings": ["f03"] }
  ]
}
```

`review: null` with an `author` is how a review we did not dispatch is recorded.
It is tracked fully; it is simply not in the config.

### `findings/<owner>-<repo>-<n>.json`

```json
{
  "pr": "acme/api#115",
  "next_id": 4,
  "findings": [
    {
      "id": "f01",
      "severity": "maintenance",
      "effort": "easy",
      "file": "workflows/manifest-status-update.md",
      "summary": "Degradation claim contradicts collect.team-prs",
      "body": "...",
      "source": { "kind": "review", "review": "docs-tests", "source_id": "PRR_kwabc" },
      "parsed_by": "script",
      "status": "open"
    },
    {
      "id": "f02",
      "status": "resolved",
      "commit": "4e91c02"
    },
    {
      "id": "f03",
      "status": "wontfix",
      "reason": "covered by ABC-140"
    }
  ]
}
```

Finding status is one of `open`, `resolved` (with `commit`), `wontfix` (with
`reason`).

Concurrent writes are avoided by writing to a temp file and renaming, and by an
advisory lock file per ticket. The CLI is single-user and single-machine; this is
enough.

## 6. Command surface

Fixed verbs. The verb set does not grow when the config does — adding a step to
YAML changes what `next` does and what `run` accepts, and adds nothing to the CLI.

Three dispatch rules carry over from v1 and still hold:

- **Verb first.** `ticket <verb> [KEY] [args]`, matching `gh pr close 115` and
  `kubectl delete pod foo`. Buys tab completion of verbs before a key is typed,
  and per-verb help without inventing a key.
- **Dispatch on shape, not a reserved list.** Jira keys match
  `^[A-Z][A-Z0-9]*-[0-9]+$` and verbs are lowercase, so a key and a verb can
  never collide and no word needs reserving. Jira enforces the shape upstream, so
  this cannot drift into a local convention. A bare key therefore means "show
  this row" with no ambiguity.
- **Reads default to the whole queue; writes demand a target.** As with `git
  status` versus `git add`. Anything that posts a comment, moves a branch, or
  resets state takes a key.

### Queue level

| Command | Does |
| --- | --- |
| `ticket` | print the queue |
| `ticket refresh` | refresh gh and Jira state for every tracked row |
| `ticket --json` | the assembled rows, machine-readable |

### Row level, inspect

| Command | Does |
| --- | --- |
| `ticket <KEY>` | the row: current step, next action, review and finding counts |
| `ticket refresh <KEY>` | refresh one row |
| `ticket reviews <KEY>` | every review on the PR, ours and theirs, in order, with status |
| `ticket findings <KEY>` | findings grouped by status, then severity |
| `ticket open <KEY>` | the PR in a browser |
| `ticket track <KEY>` | start driving an untracked row |

### Row level, the walk

| Command | Does |
| --- | --- |
| `ticket next <KEY>` | run whatever the resolver says is next |
| `ticket run <step> <KEY>` | run or re-run a named step |
| `ticket skip <step> <KEY>` | mark a step skipped; the resolver walks past it |
| `ticket release <step> <KEY>` | release a gate |
| `ticket reset <KEY> [step]` | re-run a step, resetting every step below it |

### Row level, reviews and findings

| Command | Does |
| --- | --- |
| `ticket review <KEY> [id]` | dispatch one review; default is next in `order` |
| `ticket collect <KEY>` | ingest reviews and comments the store has not seen |
| `ticket fix <KEY> [id]` | work findings, one commit each, routed by effort |
| `ticket decide <KEY> <id>` | close a finding as `wontfix` with a reason |
| `ticket effort <KEY> <id> <easy\|hard>` | override a finding's effort |

### Flags

| Flag | On | Does |
| --- | --- | --- |
| `--pr <n>` | review, collect, findings, fix | pick the PR when a key has several |
| `--dry-run` | every write | print what would happen, do nothing |
| `--json` | ticket, findings, reviews | machine-readable output |
| `--force` | reset | skip the confirmation |

### Rejected alternatives

- **Generated verbs from step ids** (`ticket spec ABC-1`). Reads nicely, but the
  verb set shifts with the config, help text has to be generated, and step ids
  eventually collide with real verbs. v1's own note that "the verb column is the
  real gap" is this problem stated from the other side; `run`/`skip` deletes the
  column entirely by making step ids data rather than API.
- **Two nouns** (`ticket ...` and `finding ...`). Cleaner separation, two
  entrypoints to keep in sync. Not worth it at this size.

If the terseness of the generated form is later missed, it can be added as pure
sugar: after fixed-verb lookup fails, try resolving the word as a step id, so
`ticket spec ABC-1` works and `ticket run spec ABC-1` stays canonical. Verbs win
ties, so it stays unambiguous.

## 7. Implementation

**Python 3, stdlib plus PyYAML, run via a `uv` script with PEP 723 inline
dependencies.** No system Python pollution, no venv to manage.

Rationale over Go: every step shells out to `gh`, `git`, or `claude`, so neither
language has an execution edge — the choice comes down to edit speed. The
engine's real work is JSON/YAML munging and prose parsing, where Python is
markedly less code, and the findings schema will churn. Go's static-binary
advantage does not apply to a solo tool on one machine. Go becomes the right
answer if this ever grows a daemon or a scheduler, and porting stays cheap
because the step scripts are bash and the store is plain JSON — only the engine
would move.

### Modules

Each has one job, a stated interface, and can be tested without the others.

| Module | Job |
| --- | --- |
| `config.py` | Load and validate config, resolve repo overrides and model aliases |
| `store.py` | Read/write ticket, PR, and findings JSON; atomic writes; locking |
| `resolve.py` | Given a ticket's state, return the next action. Pure function. |
| `gh.py` | `gh`/`git` subprocess wrappers: retry, `pr_ref` splitting, `sync` |
| `effort.py` | Haiku's easy/hard estimate at ingestion (decision #16) |
| `steps.py` | Execute a step by kind: script, gate, handoff |
| `reviews.py` | Dispatch a review over either transport |
| `collect.py` | Fetch from `gh`, dedupe, hand bodies to the parser |
| `parse.py` | Review body → findings. Script path plus Haiku fallback. |
| `fix.py` | Route a finding by effort, enforce ordering, verify the trailer landed |
| `cli.py` | Argument parsing and output formatting only. No logic. |

`resolve.py` being a pure function of stored state is the load-bearing choice: it
makes the trickiest behaviour in the system testable with fixtures and no
network, no git, and no model.

### Error handling

- A failed step records `status: failed` with the exit code and a log path. It
  does not advance. `ticket <KEY>` shows the failure and `ticket run` retries.
- `gh` and network failures are retried three times with backoff, then reported.
  They never mark a step done.
- A parse falling back to Haiku is recorded, not warned about; a Haiku fallback
  that also fails leaves the source uncollected so it can be retried.
- `--dry-run` is honoured by every write path, including PR comments.

### Testing

- `resolve.py` gets the heaviest coverage: fixtures for each state the resolver
  can be in, asserting the next action. No I/O.
- `parse.py` gets fixture reviews, including `example-review.md`, a human
  comment, an `oplane-bot` review, and a deliberately malformed body, asserting
  both the script path and the fallback trigger.
- `store.py` gets round-trip and atomicity tests against a temp store.
- Everything that shells out is tested with a fake `gh`/`git` on `PATH`, so the
  suite runs offline.

## 8. Open questions

1. ~~**Finding effort defaults.**~~ **Closed** — see decisions #16, #18, #19.
   Deriving `easy`/`hard` from severity is a guess.
   A 🟡 finding can need real work and a 🔴 can be a one-line fix. Options: keep
   the severity rule and correct by hand with `ticket effort`; let the review
   prompt emit an explicit effort per finding, which costs nothing since we
   control the prompt; or have Haiku judge it at ingestion. **Resolved: Haiku at
   ingestion**, because severity and effort are orthogonal and the sources we do
   not author (`oplane-bot`, humans) never emit an effort of their own. The field
   was called `class` in earlier drafts and is now `effort` (decision #19).
2. **`ticket auto`.** v1 had an unattended walk of every row. Deferred until the
   attended path is proven, since an unattended run that mis-routes a hard
   finding to the bot wastes a round trip and muddies the PR.
3. **Multi-PR tickets.** The model supports them; `--pr` disambiguates. Whether
   the resolver should ever consider more than the newest PR is unanswered and
   deferred until a real case appears.
4. **Bot review depth.** Bot reviews have so far come back less thorough than
   local ones. This spec makes the transport a one-word edit but does not solve
   the quality gap; that is prompt work, tracked separately.

## 9. Build order

1. `config.py`, `store.py`, and the store layout. Nothing works without them.
2. `resolve.py` plus its fixture suite. The behaviour everything else hangs off.
3. `cli.py` with `ticket`, `ticket <KEY>`, `next`, `run`, `skip`, `release`.
4. `reviews.py` and `collect.py` — dispatch and ingest, the parts that work today.
5. `parse.py`, script path first, Haiku fallback second.
6. `fix.py`, now that the effort question in §8 is settled.
7. `refresh`, `reset`, `track`, `open`, and the flags.
