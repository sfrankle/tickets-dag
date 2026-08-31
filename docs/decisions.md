# Decision log

Running log. One row per decision that closed off an alternative. Append, don't
rewrite — if a decision is reversed, add a new row and mark the old one
`superseded by #N`.

Status: **firm** (settled, would need a real reason to reopen) ·
**provisional** (works, expected to be revisited) · **open** (not yet decided).

| # | Date | Decision | Chosen | Instead of | Why | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2026-08-31 | Build vs adopt | Build our own engine | Dagu (dagu.sh) | Unit mismatch — see below | firm |
| 2 | 2026-08-31 | Unit of work | Ticket owns PRs | PR-only, or ticket-only | Steps belong to the ticket, findings to a diff | firm |
| 3 | 2026-08-31 | Config scope | Steps and reviews as two YAML collections | Steps only, reviews hardcoded | Reviews are ordered and skippable in their own right | firm |
| 4 | 2026-08-31 | Next action | Derived from state each call | Stored cursor | Skipping becomes a flag, not a transition; resolver stays a pure function | firm |
| 5 | 2026-08-31 | Step kinds | Three: script, gate, handoff | Every node a shell command | Handoffs would otherwise re-script model + prompt + agent mode every time | firm |
| 6 | 2026-08-31 | Review registry | List only reviews we dispatch | Also declare matchers for bots we never fire | `oplane-bot` is neither ours nor `unknown`; matching must not be strict to the YAML | firm |
| 7 | 2026-08-31 | Findings source | Separate store, fed from any review, comment, or local session | Findings attached to a review type | Decouples findings from whether we triggered the source | firm |
| 8 | 2026-08-31 | Review parsing | Script first, Haiku on shape mismatch | Always Haiku; or script-only | Zero tokens on the common path, still handles humans and format drift | firm |
| 9 | 2026-08-31 | Resolution tracking | One commit per finding, `Finding: fNN` trailer | Model verifies each finding against the diff | `git log` scan, zero tokens, works for bot commits too | firm |
| 10 | 2026-08-31 | CLI shape | Fixed verbs, config drives content | Verbs generated from step ids; two nouns | Verb set can't churn with config; kills the "verb column" gap in v1 | firm |
| 11 | 2026-08-31 | Store and config | Central store, central config, both overridable | State inside each repo | Queue view spans repos without scanning them | firm |
| 12 | 2026-08-31 | Language | Python 3 + PyYAML via `uv` | Go | Edit speed on a churning JSON schema; no distribution requirement | firm |
| 13 | 2026-08-31 | Model naming | Alias in config (`opus`) | Full model ID | New release is a one-line edit | firm |
| 14 | 2026-08-31 | Finding class | Derived from severity, overridable | Explicit class from the review prompt; Haiku at ingestion | Placeholder only — blocks `fix.py` | superseded by #16 |
| 15 | 2026-08-31 | `ticket auto` | Deferred | Build now | Unattended mis-routing wastes a bot round trip and muddies the PR | provisional |
| 16 | 2026-08-31 | Finding effort | Haiku judges it at ingestion | Severity rule; explicit value from review prompt | Severity (🔴/🟡/🔵) is importance, not effort, so it cannot imply `easy`/`hard` at all. Haiku is uniform across sources we don't author (`oplane-bot`, humans). Supersedes #14 | firm |
| 18 | 2026-08-31 | Findings Haiku can't judge | Store `effort: null`; `fix` refuses to route until `ticket effort` sets one | Fall back to a severity rule | Same reason as #16 — a severity fallback would silently send an important-but-large finding to the bot | firm |
| 19 | 2026-08-31 | Name of the routing field | `effort` (`ticket effort`, `effort.py`) | `class` | `class` reads as an OOP class and shadows the Python keyword; `effort` says what the field measures | firm |
| 17 | 2026-08-31 | Bot transport on this machine | `dispatch: bot` builds and posts the comment, nothing more; tested against a fake `gh` | Integrate against the real bot | The custom gh Claude bot is work-only and unavailable here; config and example prompts ship as templates configured on the work laptop | firm |
| 20 | 2026-08-31 | Resolver order | collect, fix, **step**, review, rest | Spec §2's collect, fix, review, step | A step declared after `draft-pr` (`describe`) could not run until every review was collected and every finding closed, which is not what the config says. Collect and fix stay first: both concern the diff already on the PR. Reorders spec §2 | firm |
| 21 | 2026-08-31 | Passing prompts to `claude` | On stdin | In argv | A review body pasted into a fix prompt can pass the 256 KB single-argument limit on macOS; `E2BIG` is an ugly way to find that out | firm |
| 22 | 2026-08-31 | Keeping local and remote in sync | `git fetch --prune` plus a fast-forward before every command that reads a checkout, `--no-sync` to opt out | A separate `sync` verb, or fetching only in `refresh` | Fetching is cheap and the gh bot commits on the remote, so a stale checkout means trailer scanning never closes a finding. Never rebases, never merges non-fast-forward, never touches uncommitted work — it reports and carries on | firm |
| 23 | 2026-08-31 | Worktrees | `worktrees.enabled` / `.root` / `.branch` in config; `worktree.sh` reads them and announces the path with `ticket-worktree:` | Always a worktree, or always the clone | Not every repo can take a worktree, and the engine should not care either way — it only ever learns the path the step announces | firm |

## #1 — Why Dagu was rejected

Dagu (dagu.sh, `dagucloud/dagu`) is a local-first workflow engine: a single Go
binary, DAGs declared in YAML, file-based state with no database or broker, cron
scheduling with timezones, retry policies, shell/Docker/SSH/HTTP/k8s executors, a
web UI with dependency visualisation and run history, Prometheus metrics, an MCP
server, and human approval gates. GPL-3.0, free to self-host, paid tiers for
SSO/RBAC/audit. It is a genuinely good tool and was taken seriously.

It was rejected for one structural reason and three supporting ones.

**The structural reason: its unit is a run, ours is a ticket.** A Dagu run is one
execution of a DAG, start to finish. Our unit is a long-lived row keyed on a Jira
key that sits at a stage for days, resumes where it left off, and can be reset to
an earlier stage with everything below it cleared. Dagu has no model for "this
ticket is at stage 7, resume it." Expressing that on top of Dagu means keeping
our own state store anyway — at which point Dagu is providing scheduling and a
UI we did not ask for.

**Stage 9 is not a DAG.** The review cycle loops an unbounded number of times,
driven by external events that arrive on their own schedule (a bot posts a
review; a human comments), with a findings store and per-finding routing. Dagu's
edges are static and known before the run starts. Ours are not.

**The command surface is the product.** The value here is `ticket <verb> <KEY>`
with shape-based dispatch and read/write defaults. Dagu offers `dagu start
<dag>`. Wrapping it means owning the CLI regardless, so the wrapper becomes the
real tool and Dagu becomes a dependency inside it.

**Licence and weight.** GPL-3.0 matters if this is ever shared. The distributed
workers, gRPC coordinator, scheduler, and web UI are the bulk of what Dagu is,
and none of it applies to a single-user CLI on one machine.

**What was taken from it.** Its central idea — workflow structure as
configuration rather than code — is adopted wholesale, and is the reason the
config carries steps, reviews, models, and repo overrides while the engine stays
small.

**What would reverse this.** If the tool ever needs to run unattended on a
schedule across many tickets, or to fan out across machines, the scheduling and
worker machinery stop being dead weight. Revisit then.
