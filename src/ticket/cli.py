"""Argument parsing and output formatting only. No logic.

Fixed verbs. The verb set does not grow when the config does: adding a step to
YAML changes what `next` does and what `run` accepts, and adds nothing here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass

from dataclasses import replace as replace_fields

from . import steps as steps_module
from .config import Config, load_config
from .errors import TicketError
from .resolve import Action, active_pr, next_action
from .store import Store, now

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")

# Verbs that change something. `main` takes the per-ticket advisory lock around
# these, and only these, so two runs on one ticket cannot interleave writes.
WRITE_VERBS = {
    "track", "next", "run", "skip", "release", "review",
    "collect", "fix", "decide", "effort", "reset", "refresh",
}


@dataclass
class Context:
    cfg: Config
    store: Store

    @classmethod
    def load(cls, repo: str | None = None) -> "Context":
        cfg = load_config()
        if repo:
            cfg = cfg.for_repo(repo)
        if os.environ.get("TICKET_NO_SYNC"):
            # `--no-sync` for the rare case of working offline; syncing is on by
            # default because the bot commits on the remote.
            cfg = replace_fields(cfg, sync=False)
        return cls(cfg=cfg, store=Store(cfg.store))


def load_ticket(ctx: Context, key: str) -> dict:
    ticket = ctx.store.read_ticket(key)
    if not ticket:
        raise TicketError(f"{key} is not tracked. Run: ticket track {key} --repo <owner/repo>")
    return ticket


def scoped(ctx: Context, ticket: dict) -> Context:
    """Re-resolve the config against the ticket's repo."""
    return Context(cfg=ctx.cfg.for_repo(ticket.get("repo", "")), store=ctx.store)


def resolve_for(ctx: Context, ticket: dict) -> Action:
    pr_ref = active_pr(ticket)
    # A missing PR document reads as an empty one: it is only written on the
    # first dispatch, and the first review is due before that.
    pr = (ctx.store.read_pr(pr_ref) or {}) if pr_ref else None
    findings = ctx.store.read_findings(pr_ref) if pr_ref else None
    return next_action(ctx.cfg, ticket, pr, findings)


# --- output ---------------------------------------------------------------


def _row(ctx: Context, ticket: dict) -> dict:
    inner = scoped(ctx, ticket)
    action = resolve_for(inner, ticket)
    pr_ref = active_pr(ticket)
    findings = inner.store.read_findings(pr_ref) if pr_ref else {"findings": []}
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
        print("No tracked tickets. Start one with: ticket track <KEY> --repo <owner/repo>")
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
        print(f"PR: {row['pr']}")
    for step in scoped(ctx, ticket).cfg.steps:
        status = (ticket.get("steps") or {}).get(step.id, {}).get("status", "-")
        print(f"  {step.id:<16} {status}")
    print(f"next: {row['next']['kind']} {row['next']['target'] or ''} — {row['next']['reason']}")
    return 0


# --- commands -------------------------------------------------------------


def cmd_track(args) -> int:
    ctx = Context.load()
    key = args.key
    ticket = ctx.store.read_ticket(key) or {"key": key, "prs": [], "steps": {}}
    ticket["repo"] = args.repo
    ticket["tracked"] = True
    ticket.setdefault("summary", "")
    ctx.store.write_ticket(ticket)
    print(f"tracking {key} in {args.repo}")
    return 0


def cmd_show(args) -> int:
    return print_row(Context.load(), args.key, args.json)


def cmd_queue(args) -> int:
    return print_queue(Context.load(), args.json)


def _execute(ctx: Context, ticket: dict, action: Action, dry_run: bool) -> int:
    if action.kind == "gate":
        print(f"parked at {action.target}. Release with: ticket release {action.target} {ticket['key']}")
        return 0
    if action.kind == "rest":
        print(f"at rest: {action.reason}")
        return 0
    if action.kind == "step":
        step = ctx.cfg.step(action.target)
        result = steps_module.run_step(ctx.cfg, ctx.store, ticket, step, dry_run=dry_run)
        if result.pr:
            print(f"registered PR {result.pr}")
        if result.status == "failed":
            print(f"{step.id} failed (exit {result.exit_code}). Log: {result.log}", file=sys.stderr)
            return 1
        print(f"{step.id} {result.status}")
        return 0
    # review / collect / fix arrive in Tasks 7-11; Task 12 replaces this block.
    print(f"next is `{action.kind} {action.target or ''}`. Not wired up yet.")
    return 0


def cmd_next(args) -> int:
    ctx = Context.load()
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    action = resolve_for(inner, ticket)
    return _execute(inner, ticket, action, args.dry_run)


def cmd_run(args) -> int:
    ctx = Context.load()
    ticket = load_ticket(ctx, args.key)
    inner = scoped(ctx, ticket)
    step = inner.cfg.step(args.step)
    if step.kind == "gate":
        print(f"{step.id} is a gate. Release it with: ticket release {step.id} {args.key}")
        return 0
    result = steps_module.run_step(inner.cfg, inner.store, ticket, step, dry_run=args.dry_run)
    if result.pr:
        print(f"registered PR {result.pr}")
    if result.status == "failed":
        print(f"{step.id} failed (exit {result.exit_code}). Log: {result.log}", file=sys.stderr)
        return 1
    print(f"{step.id} {result.status}")
    return 0


def cmd_skip(args) -> int:
    ctx = Context.load()
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
        raise TicketError(f"{args.key} has no PR yet, so review {args.step} cannot be skipped")
    pr = inner.store.read_pr(pr_ref) or {
        "pr": pr_ref, "key": args.key, "dispatched": [], "collected": [], "skipped": [],
    }
    if args.step not in pr.setdefault("skipped", []):
        pr["skipped"].append(args.step)
    inner.store.write_pr(pr)
    print(f"skipped review {args.step} on {pr_ref}")
    return 0


def cmd_release(args) -> int:
    ctx = Context.load()
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


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticket")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--no-sync", action="store_true",
        help="skip the git fetch/fast-forward this normally does before every command",
    )
    parser.set_defaults(func=cmd_queue, dry_run=False, verb=None, no_sync=False)
    sub = parser.add_subparsers(dest="verb")

    def add(name, func, **kwargs):
        p = sub.add_parser(name, **kwargs)
        p.set_defaults(func=func, json=False, dry_run=False)
        p.add_argument("--no-sync", action="store_true", help=argparse.SUPPRESS)
        return p

    p = add("show", cmd_show, help="show one ticket")
    p.add_argument("key")
    p.add_argument("--json", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and KEY_RE.match(argv[0]):
        argv = ["show", *argv]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits rather than returning. main() is the process's return
        # code and is called directly by the tests, so turn it back into one.
        return int(exc.code or 0)
    if getattr(args, "no_sync", False):
        os.environ["TICKET_NO_SYNC"] = "1"
    try:
        key = getattr(args, "key", None)
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
