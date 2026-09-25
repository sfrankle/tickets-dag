"""Execute a step by kind: script, gate, handoff.

The engine knows how to execute exactly three things. Everything else is a
shell script or a prompt the config points at, so adding a step of any kind
requires no engine change.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import gh
from .config import Config, Step
from .resolve import active_pr
from .store import Store, now

PR_LINE = re.compile(r"^ticket-pr:\s*(\S+/\S+#\d+)\s*$", re.MULTILINE)
WORKTREE_LINE = re.compile(r"^ticket-worktree:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class StepResult:
    status: str
    exit_code: int = 0
    log: str | None = None
    pr: str | None = None


def workdir(cfg: Config, ticket: dict) -> Path:
    """Where a step runs.

    The ticket's worktree once one is registered, else the clone named by
    `repos.<repo>.path`, else the config directory. `implement` edits code and
    `worktree.sh` runs `git worktree add`; neither works from `~/.ticket`.
    """
    if ticket.get("worktree"):
        return Path(ticket["worktree"])
    return cfg.repo_path(ticket.get("repo", "")) or cfg.root


def step_env(cfg: Config, ticket: dict) -> dict[str, str]:
    repo = ticket.get("repo", "")
    env = dict(os.environ)
    env["TICKET_KEY"] = ticket["key"]
    env["TICKET_REPO"] = repo
    env["TICKET_STORE"] = str(cfg.store)
    env["TICKET_BRANCH"] = cfg.worktrees.branch_for(ticket["key"], repo)
    env["TICKET_USE_WORKTREES"] = "1" if cfg.worktrees.enabled else "0"
    env["TICKET_WORKTREE_ROOT"] = str(cfg.worktrees.root)
    env["TICKET_WORKTREE"] = str(workdir(cfg, ticket))
    repo_path = cfg.repo_path(repo)
    if repo_path:
        env["TICKET_REPO_PATH"] = str(repo_path)
    # The selected PR, not the newest: a script asked to comment on "the" PR
    # must mean the one every verb is working on.
    pr_ref = active_pr(ticket)
    if pr_ref:
        env["TICKET_PR"] = pr_ref
    return env


def _argv(cfg: Config, step: Step) -> tuple[list[str], str | None]:
    """(argv, stdin). Prompts go on stdin, never argv — decision #21."""
    if step.kind == "script":
        return [str(cfg.path_to(step.run))], None
    argv = ["claude", "-p", "--model", cfg.model_id(step.model), *step.args]
    return argv, cfg.path_to(step.prompt).read_text()


# How long a stopped step gets to exit on SIGTERM before its group is killed outright.
GRACE_SECONDS = 5.0


class Interrupted(BaseException):
    """`ticket` was sent a signal whose default would end it without unwinding (#43).

    A `BaseException`, like `KeyboardInterrupt`, so no `except Exception` on the way out swallows it and the lock's `finally` still runs.
    """

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# Signals whose default action ends the process without unwinding, which would skip the lock's `finally` and leave a running step's child orphaned (#43).
# SIGINT is not here: Python already raises `KeyboardInterrupt` for it.
UNWINDING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)

# Every request to stop: `KeyboardInterrupt` for SIGINT, and `Interrupted` for the others while `raise_interrupted` handles them.
STOPPING_SIGNALS = (signal.SIGINT, *UNWINDING_SIGNALS)


def raise_interrupted(signum, _frame):
    raise Interrupted(signum)


@contextmanager
def handling(signums, handler) -> Iterator[None]:
    """Handle `signums` with `handler` for the duration, then put back what was there.

    `signal.signal` is main-thread only, and elsewhere the handling is left as it was.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {signum: signal.signal(signum, handler) for signum in signums}
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


def _signal_group(pgid: int, signum: int) -> None:
    """Signal a step's group, which may already be gone.

    macOS answers EPERM rather than ESRCH while the leader is an unreaped zombie.
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


def stop(process: subprocess.Popen) -> None:
    """End `process` and everything it started, politely first.

    `process` leads its own group (`tee` starts it with `process_group=0`), so the group id is its pid and a grandchild it forked goes too.
    """
    # Deaf to further requests to stop while this one is under way: an impatient second Ctrl-C, or a supervisor repeating its SIGTERM, would otherwise raise out of the grace wait before SIGKILL is sent, and a step that ignores SIGTERM would run on.
    with handling(STOPPING_SIGNALS, signal.SIG_IGN):
        _signal_group(process.pid, signal.SIGTERM)
        # A member stopped by the terminal holds SIGTERM pending until it is continued.
        _signal_group(process.pid, signal.SIGCONT)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=GRACE_SECONDS)
        # Even when the leader has exited, something it forked can still hold the group.
        _signal_group(process.pid, signal.SIGKILL)
        process.wait()


def _controlling_terminal() -> int | None:
    """A descriptor on `ticket`'s terminal when `ticket` is its foreground job, else None."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return None
    try:
        foreground = os.tcgetpgrp(fd)
    except OSError:
        foreground = None
    if foreground != os.getpgrp():
        os.close(fd)
        return None
    return fd


@contextmanager
def _foreground(process: subprocess.Popen) -> Iterator[bool]:
    """Hand the terminal to the step's group while it runs, the way a shell does for its foreground job.

    A background group that touches the terminal is stopped by it, so an ssh passphrase or a git credential prompt would leave the step stopped and `ticket` waiting on it forever.
    Ctrl-C then goes to the step's group rather than to `ticket`, and `tee` turns a step that died of it back into `KeyboardInterrupt`.
    Without a terminal, or run as a background job, there is nothing to hand over.
    Yields whether the terminal was handed over.
    """
    fd = _controlling_terminal()
    if fd is None:
        yield False
        return
    # While the step has the terminal, `ticket` is the background job: taking the terminal back, or echoing the step's output under `stty tostop`, would send it SIGTTOU and stop it.
    # Blocked, both just happen.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    handed = False
    try:
        # The group is gone if the step has already exited, and there is nothing to hand over.
        with suppress(OSError):
            os.tcsetpgrp(fd, process.pid)
            handed = True
        # It may have reached for the terminal before it was given it, and been stopped for that.
        _signal_group(process.pid, signal.SIGCONT)
        yield handed
    finally:
        # The terminal may have hung up, and then there is nothing to take back.
        with suppress(OSError):
            os.tcsetpgrp(fd, os.getpgrp())
        os.close(fd)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


# Exit statuses that mean the step ended because Ctrl-C reached it: killed by SIGINT, or exiting 128 + SIGINT as a shell reports that.
INTERRUPTED_EXITS = (-signal.SIGINT, 128 + signal.SIGINT)


def tee(
    argv: list[str],
    *,
    cwd: Path,
    env: dict,
    stdin_text: str | None,
    log: Path | None = None,
    sink: Callable[[str], None] | None = None,
) -> tuple[str, int]:
    """Run, streaming output to the terminal and to `log` as it arrives, and collecting it.

    A handoff can run for twenty minutes; capturing silently and printing at the
    end is the wrong experience for the one step a human actually watches, and a
    log written only after the process exits leaves nothing behind for a run
    that is killed or dies with its terminal (issue #27).

    The child leads its own process group, so a signal meant for `ticket` does not reach it on its own, and `ticket` stops it on the way out instead (#43).
    While it runs it holds the terminal, so it can still ask for a passphrase and Ctrl-C reaches it first.
    Anything that ends the read early — Ctrl-C, `Interrupted`, a failed write — stops the child and its group before the exception goes on.
    With `sink`, each line goes to it instead of to the terminal and `log`: `ticket refresh` prefixes and logs its own (#8).
    The returned text is the raw output either way, so announce lines still match at the start of a line.
    """
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        process_group=0,
    )
    lines: list[str] = []
    try:
        # buffering=1 is line buffering, so an interrupted run keeps what it printed
        # and a reader tailing the file sees each line as the step prints it.
        with (
            _foreground(process) as handed,
            open(log, "w", buffering=1) if log is not None else nullcontext() as handle,
        ):
            # Inside the `try`: a prompt larger than the pipe blocks here until the child reads it, and a signal that lands meanwhile must still stop the child.
            try:
                if stdin_text is not None:
                    process.stdin.write(stdin_text)
                process.stdin.close()
            except BrokenPipeError:
                # The child exited without reading its prompt. Its output and exit code
                # below are the real story; the failed write is not.
                pass
            emit = sink or _terminal_and(handle)
            for line in process.stdout:
                lines.append(line)
                emit(line)
            exit_code = process.wait()
        # Without the terminal, Ctrl-C reached `ticket` itself, and a step exiting 130 is only a step exiting 130.
        if handed and exit_code in INTERRUPTED_EXITS:
            raise KeyboardInterrupt
    except BaseException:
        stop(process)
        raise
    return "".join(lines), exit_code


def _terminal_and(handle: TextIO | None) -> Callable[[str], None]:
    """`tee`'s sink when the caller brings none: the terminal, and the log when there is one."""

    def emit(line: str) -> None:
        sys.stdout.write(line)
        if handle is not None:
            handle.write(line)

    return emit


def release_gate(store: Store, ticket: dict, step_id: str) -> None:
    ticket.setdefault("steps", {})[step_id] = {"status": "released", "at": now()}
    store.write_ticket(ticket)


def run_step(
    cfg: Config,
    store: Store,
    ticket: dict,
    step: Step,
    *,
    dry_run: bool = False,
) -> StepResult:
    if step.kind == "gate":
        return StepResult("parked")

    argv, stdin_text = _argv(cfg, step)
    if dry_run:
        print(f"[dry-run] would run {step.id}: {' '.join(argv[:2])}")
        # Not "done": nothing ran and nothing was written, and a caller that
        # printed the status would otherwise report the step as complete.
        return StepResult("dry-run")

    # Fetch first. The bot commits on the remote, so the checkout drifts
    # constantly and a stale one makes trailer scanning lie.
    if cfg.sync and ticket.get("worktree"):
        reason = gh.sync(Path(ticket["worktree"]))
        if reason:
            print(f"sync: {reason}")

    log_file = store.log_path(ticket["key"], step.id)
    stopped: BaseException | None = None
    try:
        output, exit_code = tee(
            argv,
            cwd=workdir(cfg, ticket),
            env=step_env(cfg, ticket),
            stdin_text=stdin_text,
            log=log_file,
        )
    except (KeyboardInterrupt, Interrupted) as exc:
        # `tee` has already stopped the child, and the log holds what it printed.
        # A PR the step announced before it was stopped is open all the same, and a rerun that did not know it would open another.
        stopped, output, exit_code = exc, "", None
        with suppress(OSError):
            output = log_file.read_text()
    except OSError as exc:
        # Nothing ever started, so `tee` wrote no file. This branch owns the log.
        output = f"could not execute {argv[0]}: {exc}\n"
        exit_code = 127
        log_file.write_text(output)

    # Recorded relative to the store root, so the pointer survives the store being moved; `StepResult.log` stays absolute because it is printed for a human to open.
    record: dict = {"status": "done", "at": now(), "log": store.relative(log_file)}

    pr_ref = None
    match = PR_LINE.search(output)
    if match:
        pr_ref = match.group(1)
        prs = ticket.setdefault("prs", [])
        if pr_ref not in prs:
            prs.append(pr_ref)
        # A registration is the one thing the store knows for certain about
        # which PR is being worked: the step just opened it. Leaving the
        # pointer where it was would dispatch every later review, fix and
        # collect at the PR the run just walked away from, while announcing
        # the new one — and nothing but a hand-typed `--pr` could reach it.
        ticket["active"] = pr_ref
        # Recorded against the step so `ticket reset` can undo it without any
        # step id being hardcoded in the engine.
        record["registered_pr"] = pr_ref

    worktree_match = WORKTREE_LINE.search(output)
    if worktree_match:
        ticket["worktree"] = worktree_match.group(1)
        record["registered_worktree"] = worktree_match.group(1)

    if stopped is not None:
        # Recorded as a failure so `next` runs it again, and marked so `show` can tell a stopped step from a broken one.
        ticket.setdefault("steps", {})[step.id] = {
            **record,
            "status": "failed",
            "interrupted": True,
        }
        store.write_ticket(ticket)
        raise stopped

    if exit_code == 0:
        ticket.setdefault("steps", {})[step.id] = record
        result = StepResult("done", 0, str(log_file), pr_ref)
    else:
        ticket.setdefault("steps", {})[step.id] = {
            **record,
            "status": "failed",
            "exit_code": exit_code,
        }
        result = StepResult("failed", exit_code, str(log_file), pr_ref)

    store.write_ticket(ticket)
    return result
