"""Given a ticket's stored state, return the next action. Pure function.

State records what has happened. What to do next is recomputed on every call,
so there is no cursor to keep in sync and skipping is a flag this module
honours rather than a state transition.

Resolution order, first match wins (decision #20 reorders spec §2):
  1. A dispatched review whose result has not been collected -> collect.
  2. An open finding on the active PR -> fix.
  3. The first step whose needs are satisfied and which is neither done nor skipped.
  4. An undispatched, unskipped review whose predecessors are done -> review.
  5. Nothing -> the ticket is at rest.

Rules 1, 2 and 4 only apply once the ticket has a PR. Steps outrank dispatching a
new review so that a step declared after draft-pr runs when the config says it
does; collect and fix still come first, because both are about the diff that is
already on the PR.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .config import Config

SATISFIED = {"done", "released", "skipped"}


@dataclass(frozen=True)
class Action:
    kind: str
    target: str | None
    reason: str


def active_pr(ticket: dict) -> str | None:
    """The PR every PR-shaped rule and verb acts on.

    A key can carry several PRs, and which one is being worked is a selection
    the person makes rather than something the store can derive: nothing here
    records whether a PR is open, merged or abandoned. `--pr` on any verb moves
    this pointer and it stays moved, so `next` never has to guess and never
    scans.

    The newest registration is the default, which is what every ticket written
    before the pointer existed means. A pointer at a ref the ticket no longer
    claims is stale rather than authoritative: `reset` and a hand-edited state
    file both produce one, and resolving against it would read findings no verb
    could reach.
    """
    prs = ticket.get("prs") or []
    if not prs:
        return None
    selected = ticket.get("active")
    return selected if selected in prs else prs[-1]


def orphan_steps(cfg: Config, ticket: dict) -> list[str]:
    """Recorded step ids the config no longer defines.

    A config edit that renames or collapses a step leaves its old id in the ticket's state.
    Resolution only ever walks `cfg.steps`, so such an id has no effect on what runs next — but a state file that is half this config's and half a dead one should say so out loud rather than look complete.
    The record itself is kept: it is the only trace that the work happened.
    """
    known = {step.id for step in cfg.steps}
    return sorted(sid for sid in (ticket.get("steps") or {}) if sid not in known)


def step_satisfied(ticket: dict, step_id: str) -> bool:
    status = (ticket.get("steps") or {}).get(step_id, {}).get("status")
    return status in SATISFIED


def _dispatched_ids(pr: dict) -> set[str]:
    return {d["review"] for d in pr.get("dispatched") or [] if d.get("review")}


def _collected_ids(pr: dict) -> set[str]:
    return {c["review"] for c in pr.get("collected") or [] if c.get("review")}


def review_status(pr: dict, review_id: str) -> str:
    """One review's state on one PR: `skipped`, `dispatched`, `collected` or `pending`.

    Skipping is read first, so a review skipped after it was dispatched reads
    as skipped here and as nothing at all to `_uncollected` — the two must
    agree, or the view would name work `next` has already moved past.

    `dispatched` is a count compare rather than "has it ever been collected":
    a review re-dispatched on a new head is dispatched again, however many
    results are already in.
    """
    if review_id in set(pr.get("skipped") or []):
        return "skipped"
    dispatched = sum(
        1 for d in pr.get("dispatched") or [] if d.get("review") == review_id
    )
    collected = sum(
        1 for c in pr.get("collected") or [] if c.get("review") == review_id
    )
    if dispatched > collected:
        return "dispatched"
    return "collected" if collected else "pending"


def _uncollected(pr: dict) -> str | None:
    """A review can be re-dispatched once the head moves, so this counts rather
    than sets: the third dispatch of `docs-tests` is uncollected until a third
    result has been recorded for it.

    Skipping is honoured here as well as in `_next_review`; otherwise skipping a
    review after it was dispatched would wedge `next` on `collect` forever."""
    collected = Counter(
        c["review"] for c in pr.get("collected") or [] if c.get("review")
    )
    skipped = set(pr.get("skipped") or [])
    seen: Counter = Counter()
    for dispatch in pr.get("dispatched") or []:
        review = dispatch.get("review")
        if not review or review in skipped:
            continue
        seen[review] += 1
        if seen[review] > collected[review]:
            return review
    return None


def open_findings(findings: dict | None) -> list[dict]:
    """Every open finding, in the order they should be worked.

    Remote (easy) work goes out before local (hard) work, so a local commit
    never lands on a head the bot then moves past. Findings with no effort go
    last: effort is never inferred from severity, so they need `ticket effort`.
    """
    if not findings:
        return []
    is_open = [f for f in findings.get("findings") or [] if f.get("status") == "open"]
    order = {"easy": 0, "hard": 1}
    is_open.sort(key=lambda f: (order.get(f.get("effort"), 2), f["id"]))
    return is_open


def _open_finding(findings: dict | None) -> dict | None:
    queue = open_findings(findings)
    return queue[0] if queue else None


def _next_review(cfg: Config, pr: dict) -> str | None:
    dispatched = _dispatched_ids(pr)
    collected = _collected_ids(pr)
    skipped = set(pr.get("skipped") or [])
    for review in cfg.reviews:
        if review.id in skipped or review.id in dispatched:
            continue
        earlier = [r for r in cfg.reviews if r.order < review.order]
        if all(r.id in skipped or r.id in collected for r in earlier):
            return review.id
        return None
    return None


def _next_step(cfg: Config, ticket: dict) -> tuple[str, str] | None:
    """Return (step_id, reason) for the first runnable step, or None."""
    statuses = ticket.get("steps") or {}
    for step in cfg.steps:
        status = statuses.get(step.id, {}).get("status")
        if status in SATISFIED:
            continue
        if not all(step_satisfied(ticket, need) for need in step.needs):
            continue
        if status == "failed":
            return step.id, f"{step.id} failed and has not been re-run"
        return step.id, f"{step.id} is the first step whose needs are met"
    return None


def next_action(
    cfg: Config,
    ticket: dict,
    pr: dict | None,
    findings: dict | None,
) -> Action:
    # A ticket has a PR as soon as it is registered on the ticket. The PR
    # document is only written on the first dispatch or collection, so keying
    # off the document would leave these rules dead exactly when the first
    # review is due.
    has_pr = bool(active_pr(ticket))
    pr = pr or {}

    if has_pr:
        uncollected = _uncollected(pr)
        if uncollected:
            return Action(
                "collect",
                uncollected,
                f"{uncollected} was dispatched but not collected",
            )

        finding = _open_finding(findings)
        if finding:
            return Action(
                "fix",
                finding["id"],
                f"{finding['id']} is open ({finding.get('effort') or 'no effort set'})",
            )

    # Decision #20: a runnable step outranks dispatching a new review, so a
    # step declared after draft-pr runs when the config says it does.
    step = _next_step(cfg, ticket)
    if step:
        step_id, reason = step
        if cfg.step(step_id).kind == "gate":
            return Action(
                "gate", step_id, f"parked at {step_id}; ticket release <KEY> {step_id}"
            )
        return Action("step", step_id, reason)

    if has_pr:
        review = _next_review(cfg, pr)
        if review:
            return Action("review", review, f"{review} is the next review in order")

    if not has_pr:
        return Action(
            "rest", None, "every step is done, skipped, or blocked, and there is no PR"
        )
    return Action(
        "rest", None, "every step is done, every review collected, no findings open"
    )
