"""Dispatch a review over either transport.

Every review carries a prompt, both transports. `bot` posts a PR comment the
gh Claude bot picks up; `local` runs a Claude session and posts the result.
Moving a review between the two is a one-word config edit.
"""

from __future__ import annotations

import subprocess

from pathlib import Path

from . import gh
from .config import Config, Review
from .errors import TicketError
from .steps import workdir
from .store import Store, now


def bot_body(review: Review, prompt_text: str) -> str:
    return f"/review {review.id}\n<details>\n{prompt_text.rstrip()}\n</details>\n"


def already_dispatched_at(pr: dict, review_id: str, head: str) -> bool:
    return any(
        d.get("review") == review_id and d.get("head") == head
        for d in pr.get("dispatched") or []
    )


def ensure_pr(store: Store, ticket: dict, pr_ref: str) -> dict:
    return store.read_pr(pr_ref) or {
        "pr": pr_ref,
        "key": ticket["key"],
        "head": None,
        "dispatched": [],
        "collected": [],
        "skipped": [],
    }


def _run_local(cfg: Config, ticket: dict, review: Review, prompt_text: str) -> str:
    model = cfg.model_id(review.model or cfg.default_model)
    # cwd matters: a review reads the diff, so it has to run in the checkout.
    # The prompt goes on stdin (decision #21).
    completed = subprocess.run(
        ["claude", "-p", "--model", model, *review.args],
        cwd=str(workdir(cfg, ticket)),
        input=prompt_text,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise TicketError(
            f"local review {review.id} failed: {completed.stderr.strip() or completed.returncode}"
        )
    return completed.stdout


def dispatch(
    cfg: Config,
    store: Store,
    ticket: dict,
    pr_ref: str,
    review: Review,
    *,
    dry_run: bool = False,
) -> dict:
    pr = ensure_pr(store, ticket, pr_ref)
    prompt_text = cfg.path_to(review.prompt).read_text()

    # Sync first so a local review reads the same diff the PR shows. Skipped
    # on a dry run: a dry run must not touch the checkout, only report.
    if not dry_run and cfg.sync and ticket.get("worktree"):
        reason = gh.sync(Path(ticket["worktree"]))
        if reason:
            print(f"sync: {reason}")

    head = gh.pr_head(pr_ref)
    if already_dispatched_at(pr, review.id, head):
        raise TicketError(
            f"{review.id} was already dispatched on {pr_ref} at {head}. "
            f"Re-run it once the head moves."
        )

    if review.dispatch == "bot":
        gh.pr_comment(pr_ref, bot_body(review, prompt_text), dry_run=dry_run)
    else:
        if dry_run:
            print(f"[dry-run] would run local review {review.id}")
        else:
            body = _run_local(cfg, ticket, review, prompt_text)
            gh.pr_comment(pr_ref, body, dry_run=False)

    record = {"review": review.id, "at": now(), "head": head, "transport": review.dispatch}
    if dry_run:
        return record

    pr["head"] = head
    pr["dispatched"].append(record)
    store.write_pr(pr)
    return record
