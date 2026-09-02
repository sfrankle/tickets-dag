"""Select one finding, hand it to the fixer the config declares, verify it landed.

The engine's whole job here is selection and handoff.
What actually fixes an `easy` finding is a script the config names under `fix.easy.run:` — a comment to a review bot, a queue, a patch mailer, whatever the site does — and the finding reaches it through the environment and stdin.
No fixer's protocol is written into this module, so pointing `fix` at another company's script changes nothing in the engine.

Effort is a property of the finding, not of the call: `easy` goes to that script, `hard` gets a local Claude session and this module commits.
A finding with no effort is refused rather than guessed at, because severity says nothing about effort.

Resolution is a `git log` trailer scan — deterministic, zero tokens, and it works for a remote fixer's commits too because the script asks for the trailer.
Those commits land on the *remote*, so every scan fetches first; without that the easy path would never close a single finding.

The trailer carries `finding_ref`, a hash of the PR reference and the finding id, not the id itself.
Ids are minted per PR and restart at `f01`, so a scan of HEAD and `@{upstream}` for `Finding: f01` used to match a commit from an entirely different PR and close a finding nothing had touched — after which the run stopped waiting for the fixer and posted the next one on top of it.
A ref is unique per PR, and it keeps a store-local handle out of a public comment.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import gh, steps
from .config import Config
from .effort import EFFORTS
from .errors import GhError, TicketError
from .store import Store, now

TRAILER_KEY = "Finding"

FIX_PROMPT = """Fix exactly one review finding in this working tree.

Finding{where}:
{summary}

{body}

Make the smallest change that genuinely addresses it. Do not fix anything else.
Do not commit; the caller commits.
"""


def finding_ref(pr_ref: str, finding_id: str) -> str:
    """A short, stable, PR-scoped handle for one finding.

    Public: it is what a fixer is asked to put in its commit message.
    It says nothing about the store, and two PRs' `f01` never collide.
    """
    digest = hashlib.sha1(f"{pr_ref}\x00{finding_id}".encode()).hexdigest()
    return digest[:12]


def trailer(ref: str) -> str:
    return f"{TRAILER_KEY}: {ref}"


def worktree_of(ticket: dict) -> Path:
    path = ticket.get("worktree")
    if not path:
        raise TicketError(
            f"{ticket['key']} has no worktree recorded. A script step announces one by "
            f"printing `ticket-worktree: /path` on stdout."
        )
    return Path(path)


def scan_trailers(worktree: Path) -> dict[str, str]:
    """finding ref -> commit sha, newest wins.

    Scans the upstream ref as well as HEAD, because a remote fixer's commits are on the remote and may not have been merged into the local branch yet.
    """
    revisions = ["HEAD"]
    try:
        gh.run(
            ["git", "rev-parse", "--verify", "--quiet", "@{upstream}"],
            cwd=worktree,
            retries=1,
        )
        revisions.append("@{upstream}")
    except GhError:
        pass
    argv = ["git", "log", "--format=%H%x00%B%x1e", *revisions]
    text = gh.run(argv, cwd=worktree, retries=1)
    found: dict[str, str] = {}
    for entry in text.split("\x1e"):
        if "\x00" not in entry:
            continue
        sha, message = entry.split("\x00", 1)
        sha = sha.strip()
        for raw in message.splitlines():
            line = raw.strip()
            if line.startswith(f"{TRAILER_KEY}:"):
                ref = line.split(":", 1)[1].strip()
                found.setdefault(ref, sha)
    return found


def _find(doc: dict, finding_id: str) -> dict:
    for finding in doc["findings"]:
        if finding["id"] == finding_id:
            return finding
    raise TicketError(f"no finding {finding_id} on {doc['pr']}")


def resolve_from_git(cfg: Config, store: Store, ticket: dict, pr_ref: str) -> list[str]:
    doc = store.read_findings(pr_ref)
    worktree = worktree_of(ticket)
    # A remote fixer's commits are on the remote.
    # Without this the easy path never closes anything.
    if cfg.sync:
        reason = gh.sync(worktree)
        if reason:
            print(f"sync: {reason}")
    found = scan_trailers(worktree)
    closed: list[str] = []
    for finding in doc["findings"]:
        ref = finding_ref(pr_ref, finding["id"])
        if finding.get("status") == "open" and ref in found:
            finding["status"] = "resolved"
            finding["commit"] = found[ref]
            closed.append(finding["id"])
    if closed:
        store.write_findings(doc)
    return closed


def wait_for_head(
    pr_ref: str,
    before: str,
    *,
    attempts: int = 30,
    interval: int = 10,
    poll=time.sleep,
    report=print,
) -> str:
    """Wait for the fixer's commit, saying so the whole time.

    A remote fixer runs one action per PR and silently drops a second dispatch, so the run that handed it a finding is the run that has to wait.
    That wait is minutes long: it announces itself and counts, because a silent five minutes is indistinguishable from a hang.
    """
    report(
        f"waiting for a new commit on {pr_ref} — up to "
        f"{attempts * interval // 60} min, checking every {interval}s"
    )
    for attempt in range(1, attempts + 1):
        head = gh.pr_head(pr_ref)
        if head != before:
            report(
                f"{pr_ref} moved to {head[:7]} after about {(attempt - 1) * interval}s"
            )
            return head
        report(f"  still {before[:7]} ({attempt}/{attempts}, {attempt * interval}s)")
        poll(interval)
    raise TicketError(f"head did not move on {pr_ref} after {attempts} checks")


def finding_env(
    cfg: Config, ticket: dict, pr_ref: str, finding: dict
) -> dict[str, str]:
    """What a fix script is told.

    The same base a step gets, plus the finding.
    The id is here and nowhere else: a script needs to know which finding it is working on, and that is a private channel.
    What the script chooses to make public is the script's business.
    """
    env = steps.step_env(cfg, ticket)
    env["TICKET_PR"] = pr_ref
    ref = finding_ref(pr_ref, finding["id"])
    env["TICKET_FINDING_ID"] = finding["id"]
    env["TICKET_FINDING_REF"] = ref
    env["TICKET_FINDING_TRAILER"] = trailer(ref)
    for key in ("file", "summary", "body", "severity", "effort"):
        env[f"TICKET_FINDING_{key.upper()}"] = str(finding.get(key) or "")
    return env


def _fix_easy(cfg, store, ticket, pr_ref, finding, dry_run) -> None:
    if not cfg.fix.easy_run:
        raise TicketError(
            f"{finding['id']} is easy and no fixer is configured. Declare the "
            f"script that hands an easy finding to whatever fixes it here:\n\n"
            f"    fix:\n      easy:\n        run: input/scripts/fix-easy.sh\n\n"
            f"See examples/input/scripts/fix-easy.sh for one that asks a Claude "
            f"GitHub bot."
        )
    script = cfg.path_to(cfg.fix.easy_run)
    if dry_run:
        print(f"[dry-run] would run {script} for {finding['id']}")
        return
    print(f"{finding['id']}: handing to {script.name}")
    try:
        output, exit_code = steps.tee(
            [str(script)],
            cwd=steps.workdir(cfg, ticket),
            env=finding_env(cfg, ticket, pr_ref, finding),
            # The whole finding, for a script that wants more than the environment carries.
            stdin_text=json.dumps(finding, indent=2),
        )
    except OSError as exc:
        raise TicketError(f"could not run {script}: {exc}") from None
    if exit_code != 0:
        raise TicketError(
            f"{script} exited {exit_code} for {finding['id']}: "
            f"{output.strip().splitlines()[-1] if output.strip() else 'no output'}"
        )


def _hard_prompt(cfg: Config, finding: dict, ref: str) -> str:
    template = FIX_PROMPT
    if cfg.fix.hard_prompt:
        template = cfg.path_to(cfg.fix.hard_prompt).read_text()
    fields = {
        "id": finding["id"],
        "ref": ref,
        "where": f" in {finding['file']}" if finding.get("file") else "",
        "file": finding.get("file") or "",
        "summary": finding.get("summary") or "",
        "body": finding.get("body") or "",
        "severity": finding.get("severity") or "",
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError) as exc:
        raise TicketError(
            f"fix.hard.prompt uses a placeholder this engine does not fill: {exc}. "
            f"Known: {', '.join('{' + k + '}' for k in sorted(fields))}"
        ) from None


def _fix_hard(cfg, store, ticket, pr_ref, finding, dry_run) -> None:
    worktree = worktree_of(ticket)
    ref = finding_ref(pr_ref, finding["id"])
    prompt = _hard_prompt(cfg, finding, ref)
    # Sync first so the local session reads and commits against the same head
    # the PR shows (decision #22). This runs even on a dry run: a dry run
    # must not post/write, but it should still fetch to keep the checkout
    # fresh — --no-sync is sync's opt-out, deliberately separate from
    # --dry-run.
    if cfg.sync:
        reason = gh.sync(worktree)
        if reason:
            print(f"sync: {reason}")
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
    # `cfg.fix.args` is where agent mode goes. Without it the session can read
    # but not write, and every hard fix ends in "changed nothing" below.
    print(f"{finding['id']}: running a local session; this can take a while")
    completed = subprocess.run(
        ["claude", "-p", "--model", cfg.model_id(cfg.fix.model), *cfg.fix.args],
        cwd=str(worktree),
        input=prompt,  # stdin, not argv — decision #21
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TicketError(
            f"local fix for {finding['id']} failed: {completed.stderr.strip()}"
        )
    gh.run(["git", "add", "-A"], cwd=worktree, retries=1)
    try:
        gh.run(
            [
                "git",
                "commit",
                "-m",
                f"fix: {finding.get('summary', finding['id'])}",
                "-m",
                trailer(ref),
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
    force: bool = False,
) -> str:
    doc = store.read_findings(pr_ref)
    finding = _find(doc, finding_id)

    if finding.get("status") != "open":
        raise TicketError(f"{finding_id} is {finding.get('status')}, not open")

    # A finding already out with a fixer must not go out again: a second dispatch while the first is in flight is how a fixer that runs one action per PR ends up dropping both.
    sent = finding.get("sent")
    if sent and not force:
        raise TicketError(
            f"{finding_id} was handed to the {sent['route']} fixer at {sent['at']} "
            f"and nothing has landed for it yet. Wait for that commit, or send it "
            f"again with --force."
        )

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

    if effort_value == "easy":
        # Written before the scan below, which re-reads the document.
        finding["sent"] = {"route": "easy", "at": now()}
        store.write_findings(doc)

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
