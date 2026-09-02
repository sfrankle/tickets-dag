"""Fetch reviews and comments from gh, dedupe, hand bodies to the parser.

Dedupe is on the GitHub review/comment id, so collection is idempotent and safe
to run repeatedly. Sources we did not dispatch — humans, review-bot, other
required agents — are recorded with `review: null` and an author. They are
tracked fully; they are simply not in the config.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gh
from .config import Config
from .effort import assign_effort
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


def collect(
    cfg: Config,
    store: Store,
    ticket: dict,
    pr_ref: str,
    *,
    dry_run: bool = False,
) -> list[dict]:
    pr = ensure_pr(store, ticket, pr_ref)
    seen = {c["source_id"] for c in pr.get("collected") or []}

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
        if source["id"] in seen:
            continue
        if not source["body"].strip():
            continue
        if _is_our_dispatch_comment(source["body"]):
            continue

        # The reviews we dispatch emit our known format. A body the script
        # parser recognises is therefore one of ours; anything that falls back
        # to Haiku is not, and is recorded with review: null.
        script_findings = parse_script(cfg, source["body"], author=source["author"])
        review_id = next_uncollected(pr) if script_findings is not None else None

        if dry_run:
            # Stop before the parser: a dry run must not spend a Haiku call on
            # parsing or on estimating effort.
            print(f"[dry-run] would collect {source['id']} from {source['author']}")
            added.append(
                {
                    "source_id": source["id"],
                    "review": review_id,
                    "author": source["author"],
                    "at": now(),
                    "findings": [],
                }
            )
            continue

        findings = script_findings
        if findings is None:
            findings = parse_haiku(cfg, source["body"])
        findings = _drop_duplicates(store, pr_ref, findings, ticket["key"])
        assign_effort(cfg, findings)
        for finding in findings:
            finding["source"] = {
                "kind": source["kind"],
                "review": review_id,
                "source_id": source["id"],
            }

        record = {
            "source_id": source["id"],
            "review": review_id,
            "author": source["author"],
            "at": now(),
            "findings": [],
        }

        # Named, not looked up: this runs before `write_pr` below, so on a first collection there is no PR document on disk to find the key in.
        record["findings"] = store.add_findings(pr_ref, findings, ticket["key"])
        pr.setdefault("collected", []).append(record)
        # Written per source, not once at the end: a failure on a later source
        # must not leave earlier findings minted with no collection record,
        # which would re-ingest and duplicate them on the next run.
        store.write_pr(pr)
        seen.add(source["id"])
        added.append(record)

    return added
