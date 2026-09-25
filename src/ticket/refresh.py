"""`ticket refresh`: the built-in catch-up, then the scripts `refresh:` declares (#8).

Nothing here is a step.
An entry records no status, has no `needs:` and re-runs on every refresh; what it can change is what a step's two announce lines can, under stricter rules, because a refresh runs unattended over every ticket.
Every line the run prints goes through one `Transcript`, prefixed with whose it is, to the terminal and to one log per run.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import gh
from . import reviews as reviews_module
from .config import Config, config_path
from .errors import GhError, StepError, StoreError
from .resolve import active_pr
from .steps import PR_LINE, WORKTREE_LINE, step_env, tee
from .store import LockHeld, Store
from .view import REFRESH

QUEUE = "queue"


class Transcript:
    """Every line a refresh prints, prefixed with whose it is, to the terminal and the run's log."""

    def __init__(self, handle: TextIO | None):
        self.handle = handle

    def write(self, prefix: str, text: str) -> None:
        for line in text.splitlines():
            out = f"{prefix} | {line}\n"
            sys.stdout.write(out)
            if self.handle is not None:
                self.handle.write(out)

    def sink(self, prefix: str) -> Callable[[str], None]:
        return lambda text: self.write(prefix, text)


@dataclass
class Outcome:
    """What the closing list says: one line per failure, one per ticket skipped as busy."""

    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@contextmanager
def session(store: Store, *, dry_run: bool) -> Iterator[tuple[Transcript, Outcome]]:
    """One run's transcript and outcome, with the closing list written however the run ends.

    In a `finally`, so a Ctrl-C mid-run still leaves the log saying what had already failed.
    A dry run writes nothing, the log included.
    """
    path = None if dry_run else store.refresh_log_path()
    with open(path, "w", buffering=1) if path else nullcontext() as handle:
        transcript, outcome = Transcript(handle), Outcome()
        try:
            yield transcript, outcome
        finally:
            for line in outcome.failures:
                transcript.write("failed", line)
            for line in outcome.skipped:
                transcript.write("skipped", line)
            if path:
                transcript.write("log", str(path))


def fetch_summary(
    cfg: Config, key: str, *, dry_run: bool = False, say: Callable[[str], None] = print
) -> str | None:
    """Ask the configured tracker for this ticket's title, or `None` if it cannot.

    No tracker configured, or one whose CLI is not installed on this machine, is the ordinary case rather than an error: the summary is a convenience, and both callers have a job to finish without it.
    Persisting is the caller's, so neither of them writes the row twice.
    """
    argv = cfg.tracker.summary_argv(key)
    if not argv or not shutil.which(argv[0]):
        return None
    if dry_run:
        say(f"[dry-run] would refresh {key} summary from {argv[0]}")
        return None
    summary = gh.run(argv, retries=1)
    return summary.strip().splitlines()[0] if summary.strip() else ""


def builtin(
    cfg: Config,
    store: Store,
    ticket: dict,
    *,
    dry_run: bool,
    say: Callable[[str], None],
) -> None:
    """What `refresh` did before `refresh:` existed: sync, the PR head, the summary.

    Sync, fetch and the PR lookup run even under `--dry-run` (decision 22); only store writes and the tracker shell-out are skipped.
    A dead recorded worktree is reported by `gh.sync` rather than raised, so it cannot stop the entries that would repair it.
    The summary is a convenience: a tracker outage (`GhError`) is a warning, not a failure, so worktree/PR discovery and the ticket's entries still run without it (#8).
    """
    if cfg.sync and ticket.get("worktree"):
        reason = gh.sync(Path(ticket["worktree"]))
        say(f"sync: {reason}" if reason else "synced")
    pr_ref = active_pr(ticket)
    if pr_ref:
        pr = reviews_module.ensure_pr(store, ticket, pr_ref)
        pr["head"] = gh.pr_head(pr_ref)
        if dry_run:
            say(f"[dry-run] would write pr {pr_ref} (head {pr['head']})")
        else:
            store.write_pr(pr)
    try:
        summary = fetch_summary(cfg, ticket["key"], dry_run=dry_run, say=say)
    except GhError as exc:
        say(f"warning: tracker summary: {exc}")
        summary = None
    if summary is not None:
        ticket["summary"] = summary


def entry_cwd(cfg: Config, ticket: dict) -> Path:
    """Where a ticket entry runs: the recorded worktree if it is still there, else the clone, else the config directory.

    Not `steps.workdir`, which trusts the recorded path: a step must not silently move to the clone (#46 refuses instead), but the entry that repairs a dead worktree has to be able to start.
    """
    recorded = ticket.get("worktree")
    if recorded and Path(recorded).is_dir():
        return Path(recorded)
    clone = cfg.repo_path(ticket.get("repo", ""))
    if clone and clone.is_dir():
        return clone
    return cfg.root


def entry_env(cfg: Config, ticket: dict, cwd: Path) -> dict[str, str]:
    """A step's environment, except `TICKET_WORKTREE` is where the entry actually runs.

    `TICKET_RECORDED_WORKTREE` is what the row records, dead or not, so a repair script can see what was stale.
    """
    env = step_env(cfg, ticket)
    env["TICKET_CONFIG"] = str(config_path())
    env["TICKET_WORKTREE"] = str(cwd)
    env.pop("TICKET_RECORDED_WORKTREE", None)
    if ticket.get("worktree"):
        env["TICKET_RECORDED_WORKTREE"] = ticket["worktree"]
    return env


def queue_env(cfg: Config) -> dict[str, str]:
    """No ticket, so none of a step's `TICKET_*`: only where the store and the config are.

    Inherited `TICKET_*` values are dropped, so a refresh run from inside a step does not hand a queue script that step's ticket.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("TICKET_")}
    env["TICKET_STORE"] = str(cfg.store)
    env["TICKET_CONFIG"] = str(config_path())
    return env


def run_entry(
    cfg: Config,
    run: str,
    *,
    cwd: Path,
    env: dict[str, str],
    say: Callable[[str], None],
) -> tuple[str, int]:
    """Run one `run:` script, its output going to `say`. A script that cannot start is exit 127, as in `run_step`."""
    path = cfg.path_to(run)
    try:
        return tee([str(path)], cwd=cwd, env=env, stdin_text=None, sink=say)
    except OSError as exc:
        say(f"could not execute {path}: {exc}")
        return "", 127


def label(run: str) -> str:
    """How the closing list names an entry: the script's name without its directory or suffix."""
    return Path(run).stem


def apply_announcements(
    ticket: dict, output: str, cwd: Path, say: Callable[[str], None]
) -> None:
    """The two announce lines a step can print, under refresh's rules.

    A PR the ticket already has does not move `active`: every refresh re-announces it, and moving the pointer each time would undo any `--pr` selection.
    A worktree that is not a directory is not recorded: a discovery script may name one the worktree step has yet to make, and recording it would strand the ticket the way #8 describes.
    A relative announced path is checked and recorded against `cwd`, the entry's own working directory, not this process's: the entry ran there, so that is what "does not exist" and "does exist" have to mean.
    """
    pr_match = PR_LINE.search(output)
    if pr_match:
        ref = pr_match.group(1)
        prs = ticket.setdefault("prs", [])
        if ref not in prs:
            prs.append(ref)
            ticket["active"] = ref
            say(f"registered {ref}")
    worktree_match = WORKTREE_LINE.search(output)
    if worktree_match:
        announced = worktree_match.group(1)
        resolved = Path(announced)
        if not resolved.is_absolute():
            resolved = cwd / resolved
        if not resolved.is_dir():
            say(f"{announced} does not exist, not recorded")
        else:
            path = str(resolved.resolve())
            if path != ticket.get("worktree"):
                ticket["worktree"] = path
                say(f"worktree {path}")


def refresh_ticket(
    cfg: Config,
    store: Store,
    key: str,
    transcript: Transcript,
    outcome: Outcome,
    *,
    dry_run: bool,
) -> None:
    """The built-in part, then each `refresh.ticket` entry, for one ticket. The caller holds its lock.

    The ticket is read here, under the lock, not handed in: a step that finished while the run was busy elsewhere must not have its record overwritten with an older copy.
    A failure stops this ticket's remaining entries, since a later one may read what an earlier one announced, and the caller moves on to the next ticket.
    """
    say = transcript.sink(key)
    ticket = store.read_ticket(key)
    if ticket is None:
        outcome.failures.append(f"{key} built-in: not tracked")
        return
    # Written back only if something changed: every write stamps `updated`, and the queue sorts on it, so an unconditional write would reshuffle the queue into refresh order every morning.
    before = copy.deepcopy(ticket)
    try:
        builtin(cfg, store, ticket, dry_run=dry_run, say=say)
    except (GhError, StoreError, OSError) as exc:
        say(f"failed: {exc}")
        outcome.failures.append(f"{key} built-in: {exc}")
        return
    if dry_run:
        for run in cfg.refresh.ticket:
            say(f"[dry-run] would run {run}")
        return
    for run in cfg.refresh.ticket:
        cwd = entry_cwd(cfg, ticket)
        try:
            env = entry_env(cfg, ticket, cwd)
        except StepError as exc:
            # A row naming a repo the config does not know (#42) fails this ticket, not the run.
            say(f"failed: {exc}")
            outcome.failures.append(f"{key} {label(run)}: {exc}")
            break
        output, code = run_entry(cfg, run, cwd=cwd, env=env, say=say)
        if code != 0:
            outcome.failures.append(f"{key} {label(run)}: exit {code}")
            break
        apply_announcements(ticket, output, cwd, say)
    if ticket != before:
        store.write_ticket(ticket)


def run_queue(
    cfg: Config, transcript: Transcript, outcome: Outcome, *, dry_run: bool
) -> None:
    """Each `refresh.queue` entry, once, in the config directory. Announce lines mean nothing without a ticket, so none are read.

    A failure is listed and the next entry still runs: a bulk sync that failed does not make the per-ticket catch-up wrong, only staler.
    """
    say = transcript.sink(QUEUE)
    env = queue_env(cfg)
    for run in cfg.refresh.queue:
        if dry_run:
            say(f"[dry-run] would run {run}")
            continue
        _, code = run_entry(cfg, run, cwd=cfg.root, env=env, say=say)
        if code != 0:
            outcome.failures.append(f"{QUEUE} {label(run)}: exit {code}")


def refresh_all(cfg: Config, store: Store, *, dry_run: bool) -> int:
    """`ticket refresh` with no key: the queue entries, then every tracked ticket under its own lock.

    A ticket a live run is holding is skipped rather than failed — busy is not broken — so an overnight `implement` does not turn the morning refresh red.
    A stale lock is a failure: nothing will clear it on its own, and the message names `ticket unlock`.
    `--dry-run` takes no lock, as `main` does not for a keyed dry run.
    """
    with session(store, dry_run=dry_run) as (transcript, outcome):
        run_queue(cfg, transcript, outcome, dry_run=dry_run)
        for listed in store.list_tickets():
            if not listed.get("tracked"):
                continue
            key = listed["key"]
            try:
                with nullcontext() if dry_run else store.lock(key, verb=REFRESH):
                    refresh_ticket(
                        cfg, store, key, transcript, outcome, dry_run=dry_run
                    )
            except LockHeld as exc:
                status = exc.status
                if status is None or not status.alive:
                    transcript.write(key, f"failed: {exc}")
                    outcome.failures.append(f"{key}: {exc}")
                    continue
                held_by = f" ({status.verb})" if status.verb else ""
                outcome.skipped.append(f"{key}: locked by pid {status.pid}{held_by}")
            except StoreError as exc:
                transcript.write(key, f"failed: {exc}")
                outcome.failures.append(f"{key}: {exc}")
    return 1 if outcome.failures else 0


def refresh_one(cfg: Config, store: Store, key: str, *, dry_run: bool) -> int:
    """`ticket refresh KEY`. `main` already holds the lock, so this does not take it again, and `queue` entries do not run."""
    with session(store, dry_run=dry_run) as (transcript, outcome):
        refresh_ticket(cfg, store, key, transcript, outcome, dry_run=dry_run)
    return 1 if outcome.failures else 0
