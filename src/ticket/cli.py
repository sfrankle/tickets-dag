"""Argument parsing and output formatting only. No logic.

Fixed verbs. The verb set does not grow when the config does: adding a step to
YAML changes what `next` does and what `run` accepts, and adds nothing here.
"""

from __future__ import annotations

import argparse
import json
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
from .config import Config, load_config
from .effort import EFFORTS
from .errors import TicketError
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
    for step in scoped(ctx, ticket).cfg.steps:
        record = (ticket.get("steps") or {}).get(step.id) or {}
        # A recorded path can outlive its file; say so here rather than let whatever goes to read it fall over.
        missing = "  (log missing)" if ctx.store.log_missing(record.get("log")) else ""
        print(f"  {step.id:<16} {record.get('status', '-')}{missing}")
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


def cmd_track(args) -> int:
    key = args.key  # `main` has already refused a key that is not path-safe
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    pattern = ctx.cfg.key_pattern
    if pattern and not re.match(pattern, key):
        raise TicketError(f"{key!r} does not match key_pattern {pattern!r}")
    ticket = ctx.store.read_ticket(key) or {"key": key, "prs": [], "steps": {}}
    ticket["repo"] = args.repo
    ticket["tracked"] = True
    ticket.setdefault("summary", "")
    ctx.store.write_ticket(ticket)
    print(f"tracking {key} in {args.repo}")
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
            f"parked at {action.target}. Release with: ticket release {action.target} {ticket['key']}"
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
            f"{step.id} is a gate. Release it with: ticket release {step.id} {args.key}"
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
        print(f"not waiting for {pending}: run collect again once it posts")
    return 0


def cmd_fix(args) -> int:
    ctx = Context.load(no_sync=getattr(args, "no_sync", False))
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    pr_ref = pick_pr(ticket, args)

    if args.finding and args.all:
        raise TicketError("pass a finding id or --all, not both")

    queue = open_findings(inner.store.read_findings(pr_ref))
    efforts = {f["id"]: f.get("effort") for f in queue}

    if args.finding:
        targets = [args.finding]
    elif args.all:
        # Findings with no effort are refused by `fix_one`, so working the whole
        # queue skips them rather than stopping on the first one.
        targets = [f["id"] for f in queue if f.get("effort") in EFFORTS]
        stuck = [f["id"] for f in queue if f.get("effort") not in EFFORTS]
        if stuck:
            print(
                f"no effort set, skipped: {', '.join(stuck)}. "
                f"Set one with: ticket effort {args.key} <id> easy|hard"
            )
        if not targets:
            print("nothing to fix")
            return 0
    else:
        action = resolve_for(inner, ticket)
        if action.kind != "fix":
            raise TicketError(f"next is `{action.kind}`, not a fix: {action.reason}")
        targets = [action.target]

    for finding_id in targets:
        # The bot runs one action per PR and silently drops a second dispatch,
        # so an easy fix waits for its commit before the next one goes out.
        # `--all` implies that wait; without it the second /edit vanishes with
        # no error. A hard fix commits locally and never waits: the remote head
        # does not move, so waiting on it would poll until it gave up.
        wait_here = (args.wait or args.all) and efforts.get(finding_id) == "easy"
        before = None
        if wait_here and not args.dry_run:
            # The live head, not the stored one: the stored value is None for a
            # PR we have only ever collected from, and stale once an earlier fix
            # moved the head. Either makes the wait below return immediately.
            before = gh.pr_head(pr_ref)
        status = fix_module.fix_one(
            inner.cfg, inner.store, ticket, pr_ref, finding_id, dry_run=args.dry_run
        )
        print(f"{finding_id}: {status}")
        if status == "open" and not args.dry_run and wait_here:
            head = fix_module.wait_for_head(pr_ref, before)
            # ensure_pr, not read_pr: the document does not exist yet when the
            # finding came from a source we only ever collected.
            pr = reviews_module.ensure_pr(inner.store, ticket, pr_ref)
            pr["head"] = head
            inner.store.write_pr(pr)
            fix_module.resolve_from_git(inner.cfg, inner.store, ticket, pr_ref)
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
    prs = ticket.get("prs") or []
    if not prs:
        print(f"{args.key} has no PR yet")
        return 0
    repo, number = gh.split_ref(prs[-1])
    gh.run(["gh", "pr", "view", number, "--repo", repo, "--web"], retries=1)
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
    argv = ctx.cfg.tracker.summary_argv(ticket["key"])
    # No tracker configured, or one whose CLI is not installed on this machine,
    # is the ordinary case rather than an error: the summary is a convenience
    # and the rest of refresh is the point.
    if argv and shutil.which(argv[0]):
        if dry_run:
            print(f"[dry-run] would refresh {ticket['key']} summary from {argv[0]}")
        else:
            summary = gh.run(argv, retries=1)
            ticket["summary"] = (
                summary.strip().splitlines()[0] if summary.strip() else ""
            )
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


# --- parser ---------------------------------------------------------------


VERBS: set[str] = set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticket")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="skip the git fetch/fast-forward this normally does before every command",
    )
    parser.set_defaults(func=cmd_queue, dry_run=False, verb=None, no_sync=False)
    sub = parser.add_subparsers(dest="verb")

    def add(name, func, **kwargs):
        VERBS.add(name)
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
    p.add_argument("--repo", required=True, help="owner/repo")

    p = add("next", cmd_next, help="run whatever the resolver says is next")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true")

    p = add("run", cmd_run, help="run or re-run a named step")
    p.add_argument("step")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true")

    p = add("skip", cmd_skip, help="mark a step or review skipped")
    p.add_argument("step")
    p.add_argument("key")
    p.add_argument("--reason", default="")
    p.add_argument("--dry-run", action="store_true")

    p = add("release", cmd_release, help="release a gate")
    p.add_argument("step")
    p.add_argument("key")
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

    p = add("fix", cmd_fix, help="work findings, one commit each, routed by effort")
    p.add_argument("key")
    p.add_argument("finding", nargs="?")
    p.add_argument("--pr")
    p.add_argument("--wait", action="store_true", help="wait for the bot's commit")
    p.add_argument(
        "--all",
        action="store_true",
        help="work every open finding in order, waiting between easy ones",
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

    p = add("refresh", cmd_refresh, help="refresh gh and tracker state")
    p.add_argument("key", nargs="?")
    p.add_argument("--dry-run", action="store_true")

    p = add("reset", cmd_reset, help="re-run a step, resetting every step below it")
    p.add_argument("key")
    p.add_argument("step", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    return parser


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
