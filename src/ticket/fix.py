"""Route a finding by effort, enforce ordering, verify the trailer landed.

Effort is a property of the finding, not of the call: `easy` goes back to the gh
bot as an /edit comment and the bot commits; `hard` gets a local Claude session
and this module commits. A finding with no effort is refused rather than guessed
at, because severity says nothing about effort.

Resolution is a `git log` trailer scan — deterministic, zero tokens, and it
works for the bot's commits too because the /edit comment asks for the trailer.
The bot's commits land on the *remote*, so every scan fetches first; without
that the easy path would never close a single finding.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import gh
from .effort import EFFORTS
from .config import Config
from .errors import GhError, TicketError
from .store import Store

TRAILER_KEY = "Finding"

FIX_PROMPT = """Fix exactly one review finding in this working tree.

Finding {id}{where}:
{summary}

{body}

Make the smallest change that genuinely addresses it. Do not fix anything else.
Do not commit; the caller commits.
"""


def trailer(finding_id: str) -> str:
    return f"{TRAILER_KEY}: {finding_id}"


def edit_body(finding: dict) -> str:
    where = f" in `{finding['file']}`" if finding.get("file") else ""
    return (
        f"/edit Fix finding {finding['id']}{where}.\n\n"
        f"{finding.get('body') or finding.get('summary', '')}\n\n"
        f"Make one commit for this change only, and include this trailer in the "
        f"commit message, on its own line at the end:\n\n"
        f"    {trailer(finding['id'])}\n"
    )


def worktree_of(ticket: dict) -> Path:
    path = ticket.get("worktree")
    if not path:
        raise TicketError(
            f"{ticket['key']} has no worktree recorded. A script step announces one by "
            f"printing `ticket-worktree: /path` on stdout."
        )
    return Path(path)


def scan_trailers(worktree: Path, since: str | None = None) -> dict[str, str]:
    """finding id -> commit sha, newest wins.

    Scans the upstream ref as well as HEAD, because the bot's commits are on the
    remote and may not have been merged into the local branch yet.
    """
    revisions = ["HEAD"]
    try:
        gh.run(["git", "rev-parse", "--verify", "--quiet", "@{upstream}"],
               cwd=worktree, retries=1)
        revisions.append("@{upstream}")
    except GhError:
        pass
    argv = ["git", "log", "--format=%H%x00%B%x1e", *revisions]
    if since:
        argv = ["git", "log", "--format=%H%x00%B%x1e", f"{since}..HEAD"]
    text = gh.run(argv, cwd=worktree, retries=1)
    found: dict[str, str] = {}
    for entry in text.split("\x1e"):
        if "\x00" not in entry:
            continue
        sha, message = entry.split("\x00", 1)
        sha = sha.strip()
        for line in message.splitlines():
            line = line.strip()
            if line.startswith(f"{TRAILER_KEY}:"):
                finding_id = line.split(":", 1)[1].strip()
                found.setdefault(finding_id, sha)
    return found


def _find(doc: dict, finding_id: str) -> dict:
    for finding in doc["findings"]:
        if finding["id"] == finding_id:
            return finding
    raise TicketError(f"no finding {finding_id} on {doc['pr']}")


def resolve_from_git(cfg: Config, store: Store, ticket: dict, pr_ref: str) -> list[str]:
    doc = store.read_findings(pr_ref)
    worktree = worktree_of(ticket)
    # The bot's commits are on the remote. Without this the easy path never
    # closes anything.
    if cfg.sync:
        reason = gh.sync(worktree)
        if reason:
            print(f"sync: {reason}")
    found = scan_trailers(worktree)
    closed: list[str] = []
    for finding in doc["findings"]:
        if finding.get("status") == "open" and finding["id"] in found:
            finding["status"] = "resolved"
            finding["commit"] = found[finding["id"]]
            closed.append(finding["id"])
    if closed:
        store.write_findings(doc)
    return closed


def wait_for_head(pr_ref: str, before: str, *, attempts: int = 30, poll=time.sleep) -> str:
    """The bot runs one action per PR and silently drops a second dispatch."""
    for _ in range(attempts):
        head = gh.pr_head(pr_ref)
        if head != before:
            return head
        poll(10)
    raise TicketError(f"head did not move on {pr_ref} after {attempts} checks")


def _fix_easy(cfg, store, ticket, pr_ref, finding, dry_run) -> None:
    gh.pr_comment(pr_ref, edit_body(finding), dry_run=dry_run)


def _fix_hard(cfg, store, ticket, pr_ref, finding, dry_run) -> None:
    worktree = worktree_of(ticket)
    where = f" in {finding['file']}" if finding.get("file") else ""
    prompt = FIX_PROMPT.format(
        id=finding["id"],
        where=where,
        summary=finding.get("summary", ""),
        body=finding.get("body", ""),
    )
    if dry_run:
        print(f"[dry-run] would run a local session for {finding['id']}")
        return
    # One commit per finding is the whole resolution mechanism, and `git add -A`
    # after the session would sweep whatever was already dirty into it.
    if gh.is_dirty(worktree):
        raise TicketError(
            f"{worktree} has uncommitted changes. One commit per finding means "
            f"starting from a clean tree — commit or stash first."
        )
    completed = subprocess.run(
        ["claude", "-p", "--model", cfg.model_id(cfg.default_model)],
        cwd=str(worktree),
        input=prompt,                  # stdin, not argv — decision #21
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise TicketError(
            f"local fix for {finding['id']} failed: {completed.stderr.strip()}"
        )
    gh.run(["git", "add", "-A"], cwd=worktree, retries=1)
    try:
        gh.run(
            [
                "git", "commit",
                "-m", f"fix: {finding.get('summary', finding['id'])}",
                "-m", trailer(finding["id"]),
            ],
            cwd=worktree,
            retries=1,
        )
    except GhError as exc:
        if "nothing to commit" not in str(exc):
            raise
        # The session decided nothing needed changing. That is an answer, not a
        # crash: leave the finding open for `ticket decide`.
        print(f"{finding['id']}: the session changed nothing; left open")


def fix_one(
    cfg: Config,
    store: Store,
    ticket: dict,
    pr_ref: str,
    finding_id: str,
    *,
    dry_run: bool = False,
) -> str:
    doc = store.read_findings(pr_ref)
    finding = _find(doc, finding_id)

    if finding.get("status") != "open":
        raise TicketError(f"{finding_id} is {finding.get('status')}, not open")

    effort_value = finding.get("effort")
    if effort_value not in EFFORTS:
        raise TicketError(
            f"{finding_id} has no effort set. Set one with: "
            f"ticket effort {ticket['key']} {finding_id} easy|hard"
        )

    if effort_value == "easy":
        _fix_easy(cfg, store, ticket, pr_ref, finding, dry_run)
    else:
        _fix_hard(cfg, store, ticket, pr_ref, finding, dry_run)

    if dry_run:
        return "open"

    closed = resolve_from_git(cfg, store, ticket, pr_ref)
    return "resolved" if finding_id in closed else "open"


def decide(store: Store, pr_ref: str, finding_id: str, reason: str) -> None:
    doc = store.read_findings(pr_ref)
    finding = _find(doc, finding_id)
    finding["status"] = "wontfix"
    finding["reason"] = reason
    store.write_findings(doc)


def set_effort(store: Store, pr_ref: str, finding_id: str, value: str) -> None:
    if value not in EFFORTS:
        raise TicketError(f"effort must be one of {', '.join(EFFORTS)}")
    doc = store.read_findings(pr_ref)
    _find(doc, finding_id)["effort"] = value
    store.write_findings(doc)
