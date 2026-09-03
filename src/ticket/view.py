"""Reads. One query layer, shared by `show`, `show --json` and the TUI (#28).

Nothing here writes to the store or talks to the network, and `row` is the
whole contract: `--json` prints the dict as it stands, and `print_row` formats
that same dict rather than walking the config a second time. Two readers of one
config cannot agree by inspection, and `show` and `show --json` had already
drifted once.

`Context` and its helpers live here rather than in `cli.py` because everything
that resolves a ticket against a config is a read: `cli.py` says at the top
that it is argument parsing and output formatting only.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as replace_fields

from .config import Config, load_config
from .errors import TicketError
from .resolve import Action, active_pr, next_action, review_status
from .store import Store


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


def documents(
    ctx: Context, ticket: dict, pr_ref: str | None
) -> tuple[dict | None, dict | None]:
    """The PR document and its findings, or `(None, None)` before the first PR.

    One read apiece, so a caller that needs both the resolver's answer and the
    documents behind it can hand them to `resolve_for` rather than reading the
    same two files twice.
    """
    if not pr_ref:
        return None, None
    # A missing PR document reads as an empty one: it is only written on the
    # first dispatch, and the first review is due before that.
    return (
        ctx.store.read_pr(pr_ref, ticket["key"]) or {},
        ctx.store.read_findings(pr_ref, ticket["key"]),
    )


def resolve_for(
    ctx: Context,
    ticket: dict,
    pr_ref: str | None = None,
    documents_read: tuple[dict | None, dict | None] | None = None,
) -> Action:
    """What to do next, against one PR.

    The caller may name the PR, because a `--pr` under `--dry-run` selects
    nothing: resolving against the stored pointer there would answer a question
    about a different PR than the one the command is about to act on. It may
    also pass documents it has already read, for the same PR.
    """
    pr_ref = pr_ref or active_pr(ticket)
    pr, findings = documents_read or documents(ctx, ticket, pr_ref)
    return next_action(ctx.cfg, ticket, pr, findings)


def _steps(ctx: Context, ticket: dict) -> list[dict]:
    """One entry per declared step, in config order.

    `status` is the word the store holds or `None` when the step has not run —
    never a re-interpretation, because a display vocabulary invented here would
    drift from the resolver's. Orphan records are not entries: a step exists
    because config declares it (`resolve.orphan_steps` is where the leftovers
    are reported).
    """
    records = ticket.get("steps") or {}
    entries = []
    for step in ctx.cfg.steps:
        record = records.get(step.id) or {}
        log = record.get("log")
        entries.append(
            {
                "id": step.id,
                "kind": step.kind,
                "status": record.get("status"),
                "log": log,
                # A recorded path can outlive its file; say so rather than let
                # whatever goes to read it fall over.
                "log_missing": ctx.store.log_missing(log),
            }
        )
    return entries


def _reviews(ctx: Context, pr: dict | None) -> list[dict]:
    """One entry per declared review, in `order`, empty until there is a PR.

    A review's state lives on the PR document, so before the first PR there is
    nothing to say — `pending` for every review would read as a claim about a
    diff that does not exist yet.
    """
    if pr is None:
        return []
    return [
        {"id": review.id, "status": review_status(pr, review.id)}
        for review in ctx.cfg.reviews
    ]


def row(ctx: Context, ticket: dict) -> dict:
    inner = scoped(ctx, ticket)
    pr_ref = active_pr(ticket)
    # Read once and hand the same two documents to the resolver: `row` is the
    # whole contract, so it should not cost twice the reads to build.
    pr, findings = documents(inner, ticket, pr_ref)
    action = resolve_for(inner, ticket, pr_ref, documents_read=(pr, findings))
    open_findings = [
        f
        for f in (findings or {"findings": []})["findings"]
        if f.get("status") == "open"
    ]
    return {
        "key": ticket["key"],
        "repo": ticket.get("repo", ""),
        "summary": ticket.get("summary", ""),
        "pr": pr_ref,
        "next": {"kind": action.kind, "target": action.target, "reason": action.reason},
        "open_findings": len(open_findings),
        "steps": _steps(inner, ticket),
        "reviews": _reviews(inner, pr),
    }


def rows(ctx: Context) -> list[dict]:
    return [row(ctx, t) for t in ctx.store.list_tickets() if t.get("tracked")]
