"""Argument parsing and output formatting only. No logic.

Fixed verbs. The verb set does not grow when the config does: adding a step to
YAML changes what `next` does and what `run` accepts, and adds nothing here.

Two families of verb, and `--help` says which is which (issue #12).
Management verbs — `show`, `track`, `refresh`, `next`, `reset`, `log`, `stages`, `config` — are the engine's own and mean the same thing under every config.
Stage verbs — `run`, `skip`, `release`, `review`, `collect`, `fix`, ... — are also the engine's, but every name they take as an argument comes from config, and `ticket stages --list` is where those names are read.

A stage exists because config declares it, not because the store has a row for it: the store only records what has happened to a stage.
There is no per-key registration, and every verb here resolves a stage name against `cfg.steps` and `cfg.reviews` alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from dataclasses import replace as replace_fields
from pathlib import Path

from . import collect as collect_module
from . import fix as fix_module
from . import gh
from . import reviews as reviews_module
from . import steps as steps_module
from .config import Config, RepoGuess, config_path, load_config
from .effort import EFFORTS
from .errors import GhError, TicketError
from .resolve import Action, active_pr, next_action, open_findings, orphan_steps
from .store import Store, now

# A key is not just a label: it is a store filename, a lock filename, a log
# directory and a worktree directory. The engine therefore checks only that it
# is a safe single path segment and leaves shape to `key_pattern:` in config —
# tickets come from Jira, Linear, GitHub issues or nothing at all.
UNSAFE_KEY_RE = re.compile(r"[\s/\\]")


def is_safe_key(key: str) -> bool:
    return bool(
        key
        and not UNSAFE_KEY_RE.search(key)
        and not key.startswith("-")
        and key not in (".", "..")
    )


# Verbs that change something. `main` takes the per-ticket advisory lock around
# these, and only these, so two runs on one ticket cannot interleave writes.
WRITE_VERBS = {
    "track",
    "next",
    "run",
    "skip",
    "release",
    "review",
    "collect",
    "fix",
    "decide",
    "effort",
    "attribute",
    "reset",
    "refresh",
}


@dataclass
class Context:
    cfg: Config
    store: Store

    @classmethod
    def load(cls, repo: str | None = None, no_sync: bool = False) -> Context:
        cfg = load_config()
        if repo:
            cfg = cfg.for_repo(repo)
        if no_sync:
            # `--no-sync` for the rare case of working offline; syncing is on by
            # default because the bot commits on the remote.
            cfg = replace_fields(cfg, sync=False)
        return cls(cfg=cfg, store=Store(cfg.store))


def load_ticket(ctx: Context, key: str) -> dict:
    ticket = ctx.store.read_ticket(key)
    if not ticket:
        raise TicketError(
            f"{key} is not tracked. Run: ticket track {key} --repo <owner/repo>"
        )
    return ticket


def scoped(ctx: Context, ticket: dict) -> Context:
    """Re-resolve the config against the ticket's repo."""
    return Context(cfg=ctx.cfg.for_repo(ticket.get("repo", "")), store=ctx.store)


def resolve_for(ctx: Context, ticket: dict) -> Action:
    pr_ref = active_pr(ticket)
    # A missing PR document reads as an empty one: it is only written on the
    # first dispatch, and the first review is due before that.
    pr = (ctx.store.read_pr(pr_ref, ticket["key"]) or {}) if pr_ref else None
    findings = ctx.store.read_findings(pr_ref, ticket["key"]) if pr_ref else None
    return next_action(ctx.cfg, ticket, pr, findings)


# --- output ---------------------------------------------------------------


def _row(ctx: Context, ticket: dict) -> dict:
    inner = scoped(ctx, ticket)
    action = resolve_for(inner, ticket)
    pr_ref = active_pr(ticket)
    findings = (
        inner.store.read_findings(pr_ref, ticket["key"]) if pr_ref else {"findings": []}
    )
    open_findings = [f for f in findings["findings"] if f.get("status") == "open"]
    return {
        "key": ticket["key"],
        "repo": ticket.get("repo", ""),
        "summary": ticket.get("summary", ""),
        "pr": pr_ref,
        "next": {"kind": action.kind, "target": action.target, "reason": action.reason},
        "open_findings": len(open_findings),
    }


def print_queue(ctx: Context, as_json: bool) -> int:
    rows = [_row(ctx, t) for t in ctx.store.list_tickets() if t.get("tracked")]
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(
            "No tracked tickets. Start one with: ticket track <KEY> --repo <owner/repo>"
        )
        return 0
    width = max(len(r["key"]) for r in rows)
    for row in rows:
        target = f" {row['next']['target']}" if row["next"]["target"] else ""
        findings = f"  {row['open_findings']} open" if row["open_findings"] else ""
        print(f"{row['key']:<{width}}  {row['next']['kind']}{target}{findings}")
    return 0


def print_row(ctx: Context, key: str, as_json: bool) -> int:
    ticket = load_ticket(ctx, key)
    row = _row(ctx, ticket)
    if as_json:
        print(json.dumps(row, indent=2))
        return 0
    print(f"{row['key']}  {row['repo']}  {row['summary']}".rstrip())
    if row["pr"]:
        findings = f"  {row['open_findings']} open" if row["open_findings"] else ""
        print(f"PR: {row['pr']}{findings}")
    cfg = scoped(ctx, ticket).cfg
    for step in cfg.steps:
        record = (ticket.get("steps") or {}).get(step.id) or {}
        # A recorded path can outlive its file; say so here rather than let whatever goes to read it fall over.
        missing = "  (log missing)" if ctx.store.log_missing(record.get("log")) else ""
        print(f"  {step.id:<16} {record.get('status', '-')}{missing}")
    if any((ticket.get("steps") or {}).get(s.id, {}).get("log") for s in cfg.steps):
        print(f"logs: ticket log {row['key']} <step>")
    print(
        f"next: {row['next']['kind']} {row['next']['target'] or ''} — {row['next']['reason']}"
    )
    return 0


def pick_pr(ticket: dict, args) -> str:
    prs = ticket.get("prs") or []
    if not prs:
        raise TicketError(f"{ticket['key']} has no PR yet")
    wanted = getattr(args, "pr", None)
    if not wanted:
        return prs[-1]
    matches = [p for p in prs if p.endswith(f"#{wanted}")]
    if not matches:
        raise TicketError(
            f"{ticket['key']} has no PR #{wanted}. Known: {', '.join(prs)}"
        )
    return matches[0]


def downstream(cfg: Config, step_id: str) -> list[str]:
    """The step, plus every step that transitively needs it."""
    affected = {step_id}
    changed = True
    while changed:
        changed = False
        for step in cfg.steps:
            if step.id not in affected and any(n in affected for n in step.needs):
                affected.add(step.id)
                changed = True
    return [s.id for s in cfg.steps if s.id in affected]


# --- commands -------------------------------------------------------------


def fetch_summary(ctx: Context, key: str, *, dry_run: bool = False) -> str | None:
    """Ask the configured tracker for this ticket's title, or `None` if it cannot.

    No tracker configured, or one whose CLI is not installed on this machine, is the ordinary case rather than an error: the summary is a convenience, and both callers have a job to finish without it.
    Persisting is the caller's, so neither of them writes the row twice.
    """
    argv = ctx.cfg.tracker.summary_argv(key)
    if not argv or not shutil.which(argv[0]):
        return None
    if dry_run:
        print(f"[dry-run] would refresh {key} summary from {argv[0]}")
        return None
    summary = gh.run(argv, retries=1)
    return summary.strip().splitlines()[0] if summary.strip() else ""


def _guess_repo(ctx: Context, ticket: dict) -> RepoGuess:
    """Which repo a ticket is about, read out of its summary.

    The summary is what inference reads, so it is fetched here rather than left to `refresh` (issue #8: the fact that repairs the ticket must not be gated on the ticket being repaired).
    A config with no patterns has already lost the guess, so it does not pay for the round trip; and a tracker that cannot answer — offline, no VPN, a key it has never heard of — costs the guess, not the row.
    The fetched summary is recorded on `ticket` for the caller to persist along with whatever it makes of the guess.
    """
    if not ctx.cfg.inference.patterns:
        return ctx.cfg.infer_repo(ticket["summary"])
    try:
        summary = fetch_summary(ctx, ticket["key"])
    except GhError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return RepoGuess(None, "the tracker did not answer")
    if summary is not None:
        ticket["summary"] = summary
    return ctx.cfg.infer_repo(ticket["summary"])


def cmd_track(args) -> int:
    key = args.key  # `main` has already refused a key that is not path-safe
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    pattern = ctx.cfg.key_pattern
    if pattern and not re.match(pattern, key):
        raise TicketError(f"{key!r} does not match key_pattern {pattern!r}")
    ticket = ctx.store.read_ticket(key) or {"key": key, "prs": [], "steps": {}}
    ticket["tracked"] = True
    ticket.setdefault("summary", "")
    ticket.setdefault("repo", "")

    guess = RepoGuess(None)
    if args.repo:
        ticket["repo"] = ctx.cfg.resolve_repo(args.repo)
    elif not ticket["repo"]:
        guess = _guess_repo(ctx, ticket)
        ticket["repo"] = guess.repo or ""

    ctx.store.write_ticket(ticket)
    if ticket["repo"]:
        print(f"tracking {key} in {ticket['repo']}")
        return 0
    # Not an error: a row with no repo is a ticket you can still `refresh`, retitle or point by hand, and failing here would strand it unwritten.
    print(f"tracking {key}")
    print(
        f"warning: no repo for {key} — {guess.why}. "
        f"Set one with: ticket track {key} --repo <owner/repo>",
        file=sys.stderr,
    )
    return 0


def cmd_show(args) -> int:
    return print_row(
        Context.load(no_sync=getattr(args, "no_sync", False)), args.key, args.json
    )


def cmd_queue(args) -> int:
    return print_queue(Context.load(no_sync=getattr(args, "no_sync", False)), args.json)


def _execute(ctx: Context, ticket: dict, action: Action, dry_run: bool) -> int:
    if action.kind == "gate":
        print(
            f"parked at {action.target}. Release with: ticket release {ticket['key']} {action.target}"
        )
        return 0
    if action.kind == "rest":
        print(f"at rest: {action.reason}")
        return 0
    if action.kind == "step":
        step = ctx.cfg.step(action.target)
        result = steps_module.run_step(
            ctx.cfg, ctx.store, ticket, step, dry_run=dry_run
        )
        if result.pr:
            print(f"registered PR {result.pr}")
        if result.status == "failed":
            print(
                f"{step.id} failed (exit {result.exit_code}). Log: {result.log}",
                file=sys.stderr,
            )
            return 1
        if result.status != "dry-run":
            print(f"{step.id} {result.status}")
        return 0
    if action.kind == "review":
        review = ctx.cfg.review(action.target)
        pr_ref = ticket["prs"][-1]
        reviews_module.dispatch(
            ctx.cfg, ctx.store, ticket, pr_ref, review, dry_run=dry_run
        )
        print(f"dispatched {review.id} ({review.dispatch}) on {pr_ref}")
        return 0
    if action.kind == "collect":
        pr_ref = ticket["prs"][-1]
        added = collect_module.collect(
            ctx.cfg, ctx.store, ticket, pr_ref, dry_run=dry_run
        )
        fresh = [record for record in added if not record.get("reread")]
        rereads = len(added) - len(fresh)
        # Counted apart: a re-read is an old source read again, and folding it in here reports sources that were never new.
        counts = [f"collected {len(fresh)} new sources"] if fresh else []
        if rereads:
            counts.append(f"re-read {rereads}")
        print(", ".join(counts) if counts else "nothing new yet")
        return 0
    if action.kind == "fix":
        pr_ref = ticket["prs"][-1]
        status = fix_module.fix_one(
            ctx.cfg, ctx.store, ticket, pr_ref, action.target, dry_run=dry_run
        )
        print(f"{action.target}: {status}")
        return 0
    raise TicketError(f"unhandled action {action.kind}")


def warn_about_orphans(ctx: Context, ticket: dict) -> None:
    """Say so when state records steps this config has never heard of.

    The unscoped config on purpose: a `repos.<repo>.steps.skip` removes a step from the resolved config legitimately, and that is not a dead id.
    """
    dead = orphan_steps(ctx.cfg, ticket)
    if dead:
        print(
            f"warning: {ticket['key']} records steps config.yml no longer defines: "
            f"{', '.join(dead)}. They are kept, and ignored when resolving.",
            file=sys.stderr,
        )


def cmd_next(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    warn_about_orphans(ctx, ticket)
    inner = scoped(ctx, ticket)
    action = resolve_for(inner, ticket)
    return _execute(inner, ticket, action, args.dry_run)


def cmd_run(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    step = inner.cfg.step(args.step)
    if step.kind == "gate":
        print(
            f"{step.id} is a gate. Release it with: ticket release {args.key} {step.id}"
        )
        return 0
    result = steps_module.run_step(
        inner.cfg, inner.store, ticket, step, dry_run=args.dry_run
    )
    if result.pr:
        print(f"registered PR {result.pr}")
    if result.status == "failed":
        print(
            f"{step.id} failed (exit {result.exit_code}). Log: {result.log}",
            file=sys.stderr,
        )
        return 1
    if result.status != "dry-run":
        print(f"{step.id} {result.status}")
    return 0


def cmd_skip(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    known_steps = {s.id for s in inner.cfg.steps}
    known_reviews = {r.id for r in inner.cfg.reviews}
    if args.step not in known_steps and args.step not in known_reviews:
        raise TicketError(f"unknown step or review: {args.step}")
    if args.dry_run:
        print(f"[dry-run] would skip {args.step} on {args.key}")
        return 0
    if args.step in known_steps:
        ticket.setdefault("steps", {})[args.step] = {
            "status": "skipped",
            "at": now(),
            "reason": args.reason or "",
        }
        inner.store.write_ticket(ticket)
        print(f"skipped step {args.step}")
        return 0
    pr_ref = active_pr(ticket)
    if not pr_ref:
        raise TicketError(
            f"{args.key} has no PR yet, so review {args.step} cannot be skipped"
        )
    pr = inner.store.read_pr(pr_ref, ticket["key"]) or {
        "pr": pr_ref,
        "key": args.key,
        "dispatched": [],
        "collected": [],
        "skipped": [],
    }
    if args.step not in pr.setdefault("skipped", []):
        pr["skipped"].append(args.step)
    inner.store.write_pr(pr)
    print(f"skipped review {args.step} on {pr_ref}")
    return 0


def cmd_attribute(args) -> int:
    """The manual half of issue #23.

    Attribution is positional and happens once, on the first read, so a body the script parser could not read keeps `review: null` and the dispatch it actually answered stays uncollected — which wedges `next` on a `collect` no run can clear.
    Nothing on disk can recover which dispatch that was; a person reading the review can.
    """
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)
    # `none` rather than a flag: the argument is "which dispatch did this answer", and "none of ours" is one of the answers.
    review = None if args.review == "none" else args.review
    if review is not None and review not in {r.id for r in inner.cfg.reviews}:
        raise TicketError(f"unknown review: {args.review}")
    pr = inner.store.read_pr(pr_ref, ticket["key"])
    if not pr:
        raise TicketError(f"nothing has been collected on {pr_ref} yet")
    if args.dry_run:
        print(f"[dry-run] would attribute {args.source} to {args.review}")
        return 0
    collect_module.set_review(inner.store, pr, args.source, review)
    print(f"{args.source}: {args.review}")
    return 0


def cmd_release(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    step = inner.cfg.step(args.step)
    if step.kind != "gate":
        raise TicketError(f"{step.id} is not a gate")
    if args.dry_run:
        print(f"[dry-run] would release {step.id} on {args.key}")
        return 0
    steps_module.release_gate(inner.store, ticket, step.id)
    print(f"released {step.id}")
    return 0


def cmd_review(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)
    if args.review:
        review = inner.cfg.review(args.review)
    else:
        action = resolve_for(inner, ticket)
        if action.kind != "review":
            raise TicketError(f"next is `{action.kind}`, not a review: {action.reason}")
        review = inner.cfg.review(action.target)
    reviews_module.dispatch(
        inner.cfg, inner.store, ticket, pr_ref, review, dry_run=args.dry_run
    )
    print(f"dispatched {review.id} ({review.dispatch}) on {pr_ref}")
    return 0


def cmd_collect(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)
    added = collect_module.collect(
        inner.cfg,
        inner.store,
        ticket,
        pr_ref,
        dry_run=args.dry_run,
        recollect=args.recollect,
    )
    pr = inner.store.read_pr(pr_ref, ticket["key"]) or {}
    # `(not one of ours)` only carries information when some of the sources could have been ours.
    # With nothing dispatched it was on every line.
    ours_are_out = bool(pr.get("dispatched"))
    if not added:
        print("nothing new")
    for record in added:
        if record["review"]:
            source = record["review"]
        elif ours_are_out:
            source = f"{record['author']} (not one of ours)"
        else:
            source = record["author"]
        if record.get("reread"):
            print(f"re-read {source}: {len(record['findings'])} new findings")
        else:
            print(f"collected {source}: {len(record['findings'])} findings")
    pending = collect_module.outstanding(pr)
    if pending:
        # `outstanding` counts dispatches against collection records carrying that review id, so what it reports is "not collected yet" — true both of a review that has yet to post and of one that posted and was recorded as none of ours.
        # Saying "once it posts" is only true of the first, and for the second running collect again never clears it.
        print(
            f"not waiting for {pending}: not collected yet."
            f" Collect again once it posts; if it has posted and was recorded as none of ours,"
            f" `ticket attribute {args.key} <source-id> {pending}` says so and `ticket skip {pending}` drops it."
        )
    return 0


def cmd_fix(args) -> int:
    """One run, one finding.

    A run used to be able to work the whole queue, which meant a second handoff going out while the first was still with the fixer — and a fixer that runs one action per PR drops the second.
    The queue is worked by running this again.
    """
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)

    if args.finding:
        finding_id = args.finding
    else:
        action = resolve_for(inner, ticket)
        if action.kind != "fix":
            raise TicketError(f"next is `{action.kind}`, not a fix: {action.reason}")
        finding_id = action.target

    queue = {f["id"]: f for f in inner.store.read_findings(pr_ref)["findings"]}
    effort = (queue.get(finding_id) or {}).get("effort")
    remaining = [f["id"] for f in open_findings(inner.store.read_findings(pr_ref))]

    # An easy fix is committed by someone else, on the remote, minutes later, so this run waits for that commit rather than leaving the next run to hand out another finding on top of a job still in flight.
    # A hard fix commits locally: the remote head never moves, so waiting on it would poll until it gave up.
    wait_here = effort == "easy" and not args.no_wait and not args.dry_run
    before = None
    if wait_here:
        # The live head, not the stored one: the stored value is None for a PR we have only ever collected from, and stale once an earlier fix moved the head.
        # Either makes the wait below return immediately.
        before = gh.pr_head(pr_ref)

    status = fix_module.fix_one(
        inner.cfg,
        inner.store,
        ticket,
        pr_ref,
        finding_id,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(f"{finding_id}: {status}")
    if args.dry_run or status == "resolved":
        return 0

    if not wait_here:
        if effort == "easy":
            print(
                f"{finding_id}: not waiting (--no-wait). It is with the fixer; run "
                f"`ticket fix {args.key}` again once its commit lands."
            )
        return 0

    head = fix_module.wait_for_head(pr_ref, before)
    # ensure_pr, not read_pr: the document does not exist yet when the finding came from a source we only ever collected.
    pr = reviews_module.ensure_pr(inner.store, ticket, pr_ref)
    pr["head"] = head
    inner.store.write_pr(pr)
    closed = fix_module.resolve_from_git(inner.cfg, inner.store, ticket, pr_ref)
    if finding_id in closed:
        print(f"{finding_id}: resolved")
        left = [f for f in remaining if f != finding_id]
        if left:
            print(f"{len(left)} finding(s) still open. Next: ticket fix {args.key}")
    else:
        print(
            f"{finding_id}: still open — {head[:7]} carries no trailer for it. "
            f"Check the PR, then re-send with: ticket fix {args.key} {finding_id} --force"
        )
    return 0


def cmd_decide(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    if args.dry_run:
        print(f"[dry-run] would close {args.finding} as wontfix")
        return 0
    fix_module.decide(ctx.store, pick_pr(ticket, args), args.finding, args.reason)
    print(f"{args.finding}: wontfix — {args.reason}")
    return 0


def cmd_effort(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    if args.dry_run:
        print(f"[dry-run] would set {args.finding} to {args.value}")
        return 0
    fix_module.set_effort(ctx.store, pick_pr(ticket, args), args.finding, args.value)
    print(f"{args.finding}: {args.value}")
    return 0


def cmd_findings(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    doc = ctx.store.read_findings(pick_pr(ticket, args))
    if args.json:
        print(json.dumps(doc["findings"], indent=2))
        return 0
    order = {"open": 0, "wontfix": 1, "resolved": 2}
    for finding in sorted(
        doc["findings"],
        key=lambda f: (
            order.get(f.get("status"), 9),
            ctx.cfg.severity_rank(f.get("severity")),
            f["id"],
        ),
    ):
        effort = finding.get("effort") or "-"
        print(
            f"{finding['id']}  {finding.get('status', '?'):<9} "
            f"{finding.get('severity', '?'):<13} {effort:<12} {finding.get('summary', '')}"
        )
    return 0


def cmd_reviews(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)
    pr = inner.store.read_pr(pr_ref, ticket["key"]) or reviews_module.ensure_pr(
        inner.store, ticket, pr_ref
    )
    if args.json:
        print(json.dumps(pr, indent=2))
        return 0
    collected = {c["review"]: c for c in pr.get("collected") or [] if c.get("review")}
    dispatched = {d["review"]: d for d in pr.get("dispatched") or []}
    skipped = set(pr.get("skipped") or [])
    print(f"{pr_ref}  head {pr.get('head') or '?'}")
    for review in inner.cfg.reviews:
        if review.id in skipped:
            status = "skipped"
        elif review.id in collected:
            status = f"collected ({len(collected[review.id]['findings'])} findings)"
        elif review.id in dispatched:
            status = "dispatched"
        else:
            status = "-"
        print(f"  {review.id:<16} {review.dispatch:<6} {status}")
    others = [c for c in pr.get("collected") or [] if not c.get("review")]
    for other in others:
        print(
            f"  {other['author']:<16} {'-':<6} collected ({len(other['findings'])} findings), not ours"
        )
    return 0


def cmd_open(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    if not ticket.get("prs"):
        # `open` answers rather than fails: asking to look at a PR that does not
        # exist yet is a question, not a mistake. Every other `pick_pr` caller
        # wants its stricter error.
        print(f"{args.key} has no PR yet")
        return 0
    repo, number = gh.split_ref(pick_pr(ticket, args))
    gh.run(["gh", "pr", "view", number, "--repo", repo, "--web"], retries=1)
    return 0


def cmd_unlock(args) -> int:
    """Clear a lock whose run is gone.

    Deliberately not a WRITE_VERB: taking the per-ticket lock first would fail
    on exactly the lock this exists to clear.
    """
    store = Context.load(no_sync=True).store
    status = store.lock_status(args.key)
    if status is None:
        print(f"{args.key} is not locked")
        return 0
    if status.alive:
        raise TicketError(
            f"{args.key} is locked by a running process (pid {status.pid}). "
            f"Nothing removed — stop that run first, or delete "
            f"{store.lock_path(args.key)} by hand."
        )
    store.clear_lock(args.key)
    owner = f"pid {status.pid} is not running" if status.pid else "it recorded no pid"
    print(f"cleared {args.key} lock ({owner})")
    return 0


def _refresh_one(ctx: Context, ticket: dict, dry_run: bool = False) -> None:
    # Refresh is the one verb whose whole job is being in sync, so it fetches
    # first and reports what it could not fast-forward. Sync/fetch runs even
    # under --dry-run (decision #22: dry-run gates writes and model calls,
    # not sync); only the store writes and the jira shell-out are skipped.
    if ctx.cfg.sync and ticket.get("worktree"):
        reason = gh.sync(Path(ticket["worktree"]))
        print(
            f"{ticket['key']} sync: {reason}" if reason else f"{ticket['key']} synced"
        )
    prs = ticket.get("prs") or []
    if prs:
        pr_ref = prs[-1]
        pr = reviews_module.ensure_pr(ctx.store, ticket, pr_ref)
        pr["head"] = gh.pr_head(pr_ref)
        if dry_run:
            print(f"[dry-run] would write pr {pr_ref} (head {pr['head']})")
        else:
            ctx.store.write_pr(pr)
    summary = fetch_summary(ctx, ticket["key"], dry_run=dry_run)
    if summary is not None:
        ticket["summary"] = summary
        ctx.store.write_ticket(ticket)


def cmd_refresh(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    if args.key:
        _refresh_one(ctx, load_ticket(ctx, args.key), dry_run=args.dry_run)
        print(f"refreshed {args.key}")
        return 0
    for ticket in ctx.store.list_tickets():
        if ticket.get("tracked"):
            _refresh_one(ctx, ticket, dry_run=args.dry_run)
    print("refreshed every tracked row")
    return 0


def cmd_reset(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    if args.step:
        inner.cfg.step(args.step)  # a name config does not declare is an error
    affected = (
        downstream(inner.cfg, args.step)
        if args.step
        else [s.id for s in inner.cfg.steps]
    )
    if args.dry_run:
        print(f"[dry-run] would reset {', '.join(affected)} on {args.key}")
        return 0
    if not args.force:
        try:
            answer = input(f"reset {', '.join(affected)} on {args.key}? [y/N] ")
        except EOFError:  # piped or cron: no one to answer, so decline
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing reset")
            return 0
    statuses = ticket.setdefault("steps", {})
    for step_id in affected:
        # A step records what it registered, so undoing it needs no step id in
        # the engine. Without this, resetting draft-pr leaves the PR on the
        # ticket and the resolver dives straight back into the review loop.
        record = statuses.pop(step_id, None) or {}
        registered_pr = record.get("registered_pr")
        if registered_pr and registered_pr in (ticket.get("prs") or []):
            ticket["prs"].remove(registered_pr)
        if record.get("registered_worktree") == ticket.get("worktree"):
            ticket.pop("worktree", None)
    inner.store.write_ticket(ticket)
    print(f"reset {', '.join(affected)}")
    return 0


def cmd_stages(args) -> int:
    """The names config declares.

    They are not verbs and never were.

    `ticket --help` is the engine's surface and stays fixed; this is the config's, and it changes when the YAML does.
    `--list` is accepted because the issue asks for it by name, and is also what happens with no flag.
    """
    ctx = Context.load(repo=getattr(args, "repo", None), no_sync=True)
    cfg = ctx.cfg
    doc = {
        "steps": [
            {
                "id": step.id,
                "kind": step.kind,
                "needs": list(step.needs),
                "source": step.run or step.prompt or "",
            }
            for step in cfg.steps
        ],
        "reviews": [
            {
                "id": review.id,
                "order": review.order,
                "dispatch": review.dispatch,
                "prompt": review.prompt,
            }
            for review in cfg.reviews
        ],
    }
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    print(f"stages declared by {config_path()}")
    print("steps")
    for step in doc["steps"]:
        parts = [step["source"]] if step["source"] else []
        if step["needs"]:
            parts.append(f"needs: {', '.join(step['needs'])}")
        print(f"  {step['id']:<16} {step['kind']:<8} {'  '.join(parts)}".rstrip())
    print("reviews")
    for review in doc["reviews"]:
        print(
            f"  {review['id']:<16} {review['dispatch']:<8} "
            f"order {review['order']}  {review['prompt']}"
        )
    if not doc["steps"] and not doc["reviews"]:
        print("  (none)")
    return 0


def config_problems(cfg: Config) -> list[str]:
    """Everything wrong with a config that still loaded.

    Load-time checks — cycles, unknown keys, an unknown model alias — raise before this is reached; `cmd_config` catches those and reports them the same way.
    What is left is the filesystem: a step or review that points at a prompt or script which is not there, is not readable, or (for a `run:`) is not executable, and a `repos.<repo>.path` that is not a checkout.
    """
    problems: list[str] = []

    def check(owner: str, label: str, relative: str, executable: bool = False) -> None:
        path = cfg.path_to(relative)
        if not path.is_file():
            problems.append(f"{owner}: {label} {relative} is not a file ({path})")
        elif not os.access(path, os.R_OK):
            problems.append(f"{owner}: {label} {relative} is not readable ({path})")
        elif executable and not os.access(path, os.X_OK):
            problems.append(f"{owner}: {label} {relative} is not executable ({path})")

    for step in cfg.steps:
        if step.run:
            check(f"step {step.id}", "run", step.run, executable=True)
        if step.prompt:
            check(f"step {step.id}", "prompt", step.prompt)
        if step.model and step.model not in cfg.models:
            problems.append(f"step {step.id}: model {step.model} is not in models:")
    for review in cfg.reviews:
        check(f"review {review.id}", "prompt", review.prompt)
        if review.model and review.model not in cfg.models:
            problems.append(
                f"review {review.id}: model {review.model} is not in models:"
            )
    for repo in cfg.repos:
        path = cfg.repo_path(repo)
        if path and not path.is_dir():
            problems.append(f"repos.{repo}: path {path} is not a directory")
    return problems


def cmd_config(args) -> int:
    """Show the resolved config and say whether it works.

    One command for both because they answer the same question: a config you cannot see is one you cannot check.
    Anything that stops it loading at all is reported here rather than raised, so `--validate` is the one place that always tells you what is wrong.
    """
    path = config_path()
    try:
        # load_config, not Context.load: this reports what the file says, and `--no-sync` is a flag about this run, not a setting to echo back.
        cfg = load_config()
        if getattr(args, "repo", None):
            cfg = cfg.for_repo(args.repo)
    except TicketError as exc:
        if args.json:
            print(
                json.dumps(
                    {"path": str(path), "valid": False, "problems": [str(exc)]},
                    indent=2,
                )
            )
        else:
            print(f"config: {path}")
            print(f"invalid: {exc}")
        return 1

    problems = config_problems(cfg)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "store": str(cfg.store),
                    "valid": not problems,
                    "problems": problems,
                    "models": cfg.models,
                    "default_model": cfg.default_model,
                    "sync": cfg.sync,
                    "worktrees": {
                        "enabled": cfg.worktrees.enabled,
                        "root": str(cfg.worktrees.root),
                        "branch": cfg.worktrees.branch,
                    },
                    "severities": [s.id for s in cfg.severities],
                    "steps": [{"id": s.id, "kind": s.kind} for s in cfg.steps],
                    "reviews": [
                        {"id": r.id, "dispatch": r.dispatch} for r in cfg.reviews
                    ],
                    "owner": cfg.owner,
                    "repos": {
                        repo: {"aliases": cfg.aliases_for(repo)}
                        for repo in sorted(cfg.repos)
                    },
                    "infer": {"repo": {"patterns": len(cfg.inference.patterns)}},
                },
                indent=2,
            )
        )
        return 1 if problems else 0

    if not args.validate:
        print(f"config: {path}")
        print(f"store:  {cfg.store}")
        print(f"models: {', '.join(f'{k}={v}' for k, v in cfg.models.items())}")
        print(f"default model: {cfg.default_model}")
        print(f"sync: {cfg.sync}")
        print(
            f"worktrees: enabled={cfg.worktrees.enabled} "
            f"root={cfg.worktrees.root} branch={cfg.worktrees.branch}"
        )
        print(f"severities: {', '.join(s.id for s in cfg.severities)}")
        print(f"steps: {', '.join(s.id for s in cfg.steps) or '(none)'}")
        print(f"reviews: {', '.join(r.id for r in cfg.reviews) or '(none)'}")
        if cfg.owner:
            print(f"owner: {cfg.owner}")
        if cfg.repos:
            # Aliases are shown beside the repo they resolve to, because the question this line answers is "will `--repo CSM` work".
            named = []
            for repo in sorted(cfg.repos):
                aliases = cfg.aliases_for(repo)
                named.append(f"{repo} ({', '.join(aliases)})" if aliases else repo)
            print(f"repos: {', '.join(named)}")
        if cfg.inference.patterns:
            count = len(cfg.inference.patterns)
            print(f"infer.repo: {count} pattern{'s' if count > 1 else ''}")
    if problems:
        print(f"invalid: {len(problems)} problem{'s' if len(problems) > 1 else ''}")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("ok")
    return 0


def cmd_log(args) -> int:
    """A step's recorded log, or a sentence saying why there is not one."""
    ctx = Context.load(no_sync=True)
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    inner.cfg.step(args.step)  # a name config does not declare is an error
    record = (ticket.get("steps") or {}).get(args.step) or {}
    print(ctx.store.read_log(record.get("log")))
    return 0


# --- parser ---------------------------------------------------------------


VERBS: set[str] = set()

# Which family a verb belongs to (issue #12).
# Management verbs mean the same thing under every config; stage verbs take a name that only config can supply.
# The split is what `--help` renders, and the only thing it changes.
STAGE_VERBS = (
    "run",
    "skip",
    "release",
    "review",
    "collect",
    "fix",
    "decide",
    "effort",
    "attribute",
    "findings",
    "reviews",
)

# Verbs whose two positionals are a key and a stage.
# The key comes first, as it does everywhere else; `_unswap` forgives the order this used to take.
STEP_AND_KEY_VERBS = {"run", "skip", "release", "reset", "log"}


def _stage_help(entries: list[tuple[str, str]]) -> str:
    """The block `--help` prints below argparse's own list.

    Held out of the subparser list on purpose: these are engine verbs, but every argument they take is a config name, and mixing the two families in one alphabetical list is what made the surface feel heavy.
    """
    width = max((len(name) for name, _ in entries), default=0)
    lines = [f"    {name:<{width}}  {text}" for name, text in entries]
    return "\n".join(
        [
            "stage commands (engine verbs; every name they take comes from config):",
            *lines,
            "",
            "The stages themselves are not verbs. List them with: ticket stages --list",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="skip the git fetch/fast-forward this normally does before every command",
    )
    parser.set_defaults(func=cmd_queue, dry_run=False, verb=None, no_sync=False)
    sub = parser.add_subparsers(
        dest="verb", title="management commands", metavar="<command>"
    )
    stage_help: list[tuple[str, str]] = []

    def add(name, func, **kwargs):
        VERBS.add(name)
        if name in STAGE_VERBS:
            # Popped, not set to SUPPRESS: argparse renders the sentinel as literal text rather than hiding the row.
            # No `help` at all is what keeps a verb out of the subparser list.
            stage_help.append((name, kwargs.pop("help", "")))
        p = sub.add_parser(name, **kwargs)
        p.set_defaults(func=func, dry_run=False)
        # default=SUPPRESS: a subparser's own copy of this flag must not clobber
        # a `--no-sync` (or `--json`) already parsed before the verb (argparse
        # applies each parser's defaults into the shared namespace, so a plain
        # default=False here would silently override `ticket --no-sync collect
        # ...` or `ticket --json show ABC-1`).
        p.add_argument(
            "--no-sync",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        return p

    p = add("show", cmd_show, help="show one ticket")
    p.add_argument("key")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = add("track", cmd_track, help="start driving an untracked row")
    p.add_argument("key")
    p.add_argument(
        "--repo",
        help="owner/repo, a bare repo name, or a repo alias. Omit to infer it "
        "from the ticket summary via infer.repo: in config",
    )

    p = add("next", cmd_next, help="run whatever the resolver says is next")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true")

    p = add("run", cmd_run, help="run or re-run a named step")
    p.add_argument("key")
    p.add_argument("step")
    p.add_argument("--dry-run", action="store_true")

    p = add("skip", cmd_skip, help="mark a step or review skipped")
    p.add_argument("key")
    p.add_argument("step")
    p.add_argument("--reason", default="")
    p.add_argument("--dry-run", action="store_true")

    p = add("release", cmd_release, help="release a gate")
    p.add_argument("key")
    p.add_argument("step")
    p.add_argument("--dry-run", action="store_true")

    p = add("review", cmd_review, help="dispatch one review")
    p.add_argument("key")
    p.add_argument("review", nargs="?")
    p.add_argument("--pr")
    p.add_argument("--dry-run", action="store_true")

    p = add("collect", cmd_collect, help="ingest reviews and comments not yet seen")
    p.add_argument("key")
    p.add_argument("--pr")
    p.add_argument(
        "--recollect",
        action="append",
        metavar="SOURCE_ID",
        help="re-read this already-collected source (repeatable)",
    )
    p.add_argument("--dry-run", action="store_true")

    p = add("fix", cmd_fix, help="work one finding, routed by effort")
    p.add_argument("key")
    p.add_argument("finding", nargs="?")
    p.add_argument("--pr")
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="hand the finding off and return, without waiting for its commit",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="send a finding again that is already with the fixer",
    )
    p.add_argument("--dry-run", action="store_true")

    p = add("decide", cmd_decide, help="close a finding as wontfix")
    p.add_argument("key")
    p.add_argument("finding")
    p.add_argument("reason")
    p.add_argument("--pr")
    p.add_argument("--dry-run", action="store_true")

    p = add("effort", cmd_effort, help="override a finding's effort")
    p.add_argument("key")
    p.add_argument("finding")
    p.add_argument("value", choices=list(EFFORTS))
    p.add_argument("--pr")
    p.add_argument("--dry-run", action="store_true")

    p = add(
        "attribute",
        cmd_attribute,
        help="say which dispatch a collected source answered",
    )
    p.add_argument("key")
    p.add_argument("source", help="the source id `ticket reviews` prints")
    p.add_argument("review", help="a review id, or `none` for not one of ours")
    p.add_argument("--pr")
    p.add_argument("--dry-run", action="store_true")

    p = add("findings", cmd_findings, help="findings grouped by status, then severity")
    p.add_argument("key")
    p.add_argument("--pr")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = add("reviews", cmd_reviews, help="every review on the PR, ours and theirs")
    p.add_argument("key")
    p.add_argument("--pr")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = add("open", cmd_open, help="the PR in a browser")
    p.add_argument("key")
    p.add_argument("--pr")

    p = add("unlock", cmd_unlock, help="clear a lock a dead run left behind")
    p.add_argument("key")

    p = add("refresh", cmd_refresh, help="refresh gh and tracker state")
    p.add_argument("key", nargs="?")
    p.add_argument("--dry-run", action="store_true")

    p = add("reset", cmd_reset, help="re-run a step, resetting every step below it")
    p.add_argument("key")
    p.add_argument("step", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = add("log", cmd_log, help="what a step's last run wrote")
    p.add_argument("key")
    p.add_argument("step")

    p = add("stages", cmd_stages, help="the steps and reviews config declares")
    p.add_argument(
        "--list",
        action="store_true",
        help="list them (the default, and the only thing this does)",
    )
    p.add_argument("--repo", help="resolve repo overrides before listing")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    p = add("config", cmd_config, help="show the resolved config and check it")
    p.add_argument(
        "--validate", action="store_true", help="only report problems, not the config"
    )
    p.add_argument("--repo", help="resolve repo overrides first")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    parser.epilog = _stage_help(stage_help)
    return parser


def _is_stage(cfg, name: str) -> bool:
    """Does config declare this name as a step or a review?"""
    return any(s.id == name for s in cfg.steps) or any(
        r.id == name for r in cfg.reviews
    )


def _unswap(args, cfg, store: Store) -> None:
    """Forgive `ticket skip <step> <KEY>`, the order these verbs used to take.

    That order is the whole of issue #12's headline bug: `run`, `skip` and `release` read `<step> <key>` while every other verb read `<key>` first, so `ticket skip KEY-1 evaluate` looked up a ticket named `evaluate` and answered "evaluate is not tracked" about a step `show` had just listed.

    The swap is taken only when the first word names a stage config declares and no tracked ticket, and the second word does name a tracked ticket. All three have to hold, so it can never quietly pick the wrong ticket, and a mistyped key is reported as the key the user typed rather than being "corrected" into the step slot.
    """
    key = getattr(args, "key", None)
    step = getattr(args, "step", None)
    # Both words are interpolated into a store path by read_ticket below, so
    # neither is trusted before it is checked; an unusable key falls through to
    # main()'s own error rather than being swapped for the step.
    if not key or not step or not is_safe_key(step) or not is_safe_key(key):
        return
    # The first word has to name a stage, or this is not the old order at all:
    # `skip ABC-999 ABC-123` is a typo in the key, and saying "the key comes
    # first now" about it asserts a correction nobody made.
    if not _is_stage(cfg, key):
        return
    if store.read_ticket(key) or not store.read_ticket(step):
        return
    args.key, args.step = step, key
    print(
        f"note: the key comes first now — ticket {args.verb} {args.key} {args.step}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # A bare key means `show`. Keys are loose enough to look like words now, so
    # a verb always wins the ambiguity — `ticket refresh` is the verb even for
    # someone with a ticket keyed `refresh`, who can still say `ticket show
    # refresh`.
    if argv and argv[0] not in VERBS and is_safe_key(argv[0]):
        argv = ["show", *argv]
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits rather than returning. main() is the process's return
        # code and is called directly by the tests, so turn it back into one.
        return int(exc.code or 0)
    try:
        if args.verb in STEP_AND_KEY_VERBS:
            cfg = load_config()
            _unswap(args, cfg, Store(cfg.store))
        key = getattr(args, "key", None)
        # Before anything interpolates the key into a path — the lock below
        # does it first, ahead of any command's own validation.
        if key and not is_safe_key(key):
            raise TicketError(
                f"{key!r} is not a usable ticket key: it becomes a file and a "
                f"directory name, so it cannot be empty, contain whitespace or "
                f"a path separator, or start with '-'."
            )
        if args.verb in WRITE_VERBS and key and not getattr(args, "dry_run", False):
            store = Store(load_config().store)
            with store.lock(key):
                return args.func(args)
        return args.func(args)
    except TicketError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
