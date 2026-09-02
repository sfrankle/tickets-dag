"""Fetch reviews and comments from gh, dedupe, hand bodies to the parser.

Dedupe is on the GitHub review/comment id, so collection is idempotent and safe
to run repeatedly. Sources we did not dispatch — humans, review-bot, other
required agents — are recorded with `review: null` and an author. They are
tracked fully; they are simply not in the config.

What decides that is the script parser's two empty answers, which do not mean the same thing (issue #23).
`[]` is a review of ours that found nothing: it ran, it answered its dispatch, and it takes that dispatch's collected slot — an all-clear review claiming no slot would leave its dispatch outstanding forever and wedge `next` on a `collect` that can never clear.
`None` is a body we could not read as a review at all: it answered nothing, so it claims nothing.
Attribution is positional — `next_uncollected` in dispatch order, because a dispatch record carries no author to match a body against — and it happens once, on the first read.

A source id in `collected` is normally skipped, with two exceptions (issue #10):

  * a record that produced no findings is re-parsed on every run, so a parser that has since learned to read that body recovers its findings instead of the source being unreachable forever.
    This costs nothing: the re-parse stops at the script parser and never falls back to Haiku.
  * `recollect=[source_id]` forces a full re-read of a named source, Haiku included — the case a record that already has findings needs.

Neither can duplicate a finding: everything parsed goes through `_drop_duplicates` against the findings already open on the PR, and a re-read updates the existing collection record rather than adding a second one.

Collection never waits.
It reads what is on the PR at the moment it runs and returns; a review that has not posted yet is simply still outstanding, which `outstanding()` reports so the caller can say so.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gh
from .config import Config
from .effort import assign_effort
from .errors import TicketError
from .parse import parse_haiku, parse_script
from .resolve import _uncollected as next_uncollected
from .reviews import ensure_pr
from .store import Store, now

DISPATCH_RE = re.compile(r"^/review\s+(\S+)", re.MULTILINE)


def _is_our_dispatch_comment(body: str) -> bool:
    """Our own `/review <id>` request is a request, not a result."""
    return bool(DISPATCH_RE.match(body.lstrip()))


def _fingerprint(finding: dict) -> tuple[str, str]:
    return (finding.get("file") or "", (finding.get("summary") or "").strip())


def _drop_duplicates(
    store: Store, pr_ref: str, findings: list[dict], key: str | None = None
) -> list[dict]:
    """review-bot posts on every push and a re-run review repeats its unfixed
    items, so the same finding arrives again under a new source id.

    Only findings still `open` count toward the fingerprint set: a finding
    that was resolved or marked wontfix and then re-raised (because the fix
    was actually wrong) must get a fresh entry rather than being silently
    dropped as a duplicate.

    Both fingerprint fields are derived by the parser, so a store written by an older grammar re-adds its still-open script-parsed findings once on the first collect after the upgrade.
    It settles from there."""
    known = {
        _fingerprint(f)
        for f in store.read_findings(pr_ref, key)["findings"]
        if f.get("status") == "open"
    }
    fresh = []
    for finding in findings:
        mark = _fingerprint(finding)
        if mark in known:
            continue
        known.add(mark)
        fresh.append(finding)
    return fresh


def outstanding(pr: dict) -> str | None:
    """The dispatched review whose result has not been collected yet, if any.

    `collect` does not wait for it — this is what lets a caller say so.
    """
    return next_uncollected(pr)


def collect(
    cfg: Config,
    store: Store,
    ticket: dict,
    pr_ref: str,
    *,
    dry_run: bool = False,
    recollect: list[str] | None = None,
) -> list[dict]:
    pr = ensure_pr(store, ticket, pr_ref)
    seen = {c["source_id"]: c for c in pr.get("collected") or []}
    forced = set(recollect or ())

    # Fetch before reading the PR: the whole point of collecting is to act on
    # commits and comments made elsewhere.
    if cfg.sync and ticket.get("worktree"):
        reason = gh.sync(Path(ticket["worktree"]))
        if reason:
            print(f"sync: {reason}")

    sources = [{"kind": "review", **item} for item in gh.pr_reviews(pr_ref)] + [
        {"kind": "comment", **item} for item in gh.pr_comments(pr_ref)
    ]

    added: list[dict] = []
    for source in sources:
        prior = seen.get(source["id"])
        forced_here = source["id"] in forced
        # A record that already produced findings is done with, unless the caller named it.
        # One that produced none is looked at again below.
        if prior is not None and prior.get("findings") and not forced_here:
            continue
        if not source["body"].strip():
            continue
        if _is_our_dispatch_comment(source["body"]):
            continue

        # The reviews we dispatch emit our known format. A body the script
        # parser recognises is therefore one of ours; anything that falls back
        # to Haiku is not, and is recorded with review: null.
        script_findings = parse_script(cfg, source["body"], author=source["author"])

        if prior is not None and not forced_here and not script_findings:
            # An automatic re-read is a free re-run of the script parser.
            # The body is still unreadable (or genuinely empty, an all-clear review), so there is nothing to recover and no reason to buy a Haiku call for it on this run and every run after it.
            continue

        # Attribution happens once, on the first read, and never again.
        # A re-read keeps whatever its record claimed — `null` included, which is a decision already made ("nothing we dispatched") rather than a value still missing.
        # Asking `next_uncollected` on a re-read returns whatever is outstanding *now*, which for a `null` record is a dispatch that body cannot have answered: it would consume that newer dispatch's slot and leave the genuine result to arrive as "not one of ours".
        # Which dispatch an unrecognised body did answer is not recoverable — attribution is positional, and the record did not keep it — so it stays null rather than being guessed.
        if prior is not None:
            review_id = prior.get("review")
        elif script_findings is not None:
            # `[]` is a review of ours that found nothing: it answered its dispatch and occupies that dispatch's slot.
            # Only `None` — a body the script parser could not read as a review at all — claims nothing.
            review_id = next_uncollected(pr)
        else:
            review_id = None

        if dry_run:
            # Stop before the parser: a dry run must not spend a Haiku call on
            # parsing or on estimating effort.
            verb = "re-read" if prior is not None else "collect"
            print(f"[dry-run] would {verb} {source['id']} from {source['author']}")
            added.append(
                {
                    "source_id": source["id"],
                    "review": review_id,
                    "author": source["author"],
                    "at": now(),
                    "findings": [],
                    # The caller prints from this record, so it has to carry the same verb the line above used.
                    "reread": prior is not None,
                }
            )
            continue

        findings = script_findings
        if findings is None:
            findings = parse_haiku(cfg, source["body"])
        findings = _drop_duplicates(store, pr_ref, findings, ticket["key"])

        if prior is not None and not forced_here and not findings:
            # An automatic re-read that recovered nothing is a no-op, not work.
            # review-bot repeats its unfixed items on every push, so a body's findings are often all open already under a later source id — and recording that would leave this record's `findings` empty, which is the very condition that makes it eligible for re-reading, so it would be re-read, rewritten and printed on every run after this one.
            # A caller who named the source is told what happened either way.
            continue

        assign_effort(cfg, findings)
        for finding in findings:
            finding["source"] = {
                "kind": source["kind"],
                "review": review_id,
                "source_id": source["id"],
            }

        # Named, not looked up: this runs before the `write_pr` calls
        # below, so on a first collection there is no PR document on disk
        # to find the key in.
        minted = store.add_findings(pr_ref, findings, ticket["key"])

        if prior is not None:
            # One record per source, always: a re-read updates the record it already has, so the source id keeps a single history.
            prior["review"] = review_id
            prior["reread_at"] = now()
            prior["findings"] = list(prior.get("findings") or []) + minted
            store.write_pr(pr)
            # What the caller is told about is this run's work, so `findings` here is what was newly minted, not the record's whole list.
            added.append(dict(prior, findings=minted, reread=True))
            continue

        record = {
            "source_id": source["id"],
            "review": review_id,
            "author": source["author"],
            "at": now(),
            "findings": minted,
        }

        pr.setdefault("collected", []).append(record)
        # Written per source, not once at the end: a failure on a later source
        # must not leave earlier findings minted with no collection record,
        # which would re-ingest and duplicate them on the next run.
        store.write_pr(pr)
        seen[source["id"]] = record
        added.append(record)

    missing = sorted(forced - {s["id"] for s in sources})
    if missing:
        # Raised after the work above is written, not before: the sources that do exist were collected, and a typo in one id must not throw that away.
        # It is still an error — a mistyped id that exits 0 reads as success to whatever ran us.
        raise TicketError(
            f"--recollect: no such source on {pr_ref}: {', '.join(missing)}"
        )

    return added
