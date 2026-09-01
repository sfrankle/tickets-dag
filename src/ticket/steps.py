"""Execute a step by kind: script, gate, handoff.

The engine knows how to execute exactly three things. Everything else is a
shell script or a prompt the config points at, so adding a step of any kind
requires no engine change.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import gh
from .config import Config, Step
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
    prs = ticket.get("prs") or []
    if prs:
        env["TICKET_PR"] = prs[-1]
    return env


def _argv(cfg: Config, step: Step) -> tuple[list[str], str | None]:
    """(argv, stdin). Prompts go on stdin, never argv — decision #21."""
    if step.kind == "script":
        return [str(cfg.path_to(step.run))], None
    argv = ["claude", "-p", "--model", cfg.model_id(step.model), *step.args]
    return argv, cfg.path_to(step.prompt).read_text()


def _tee(
    argv: list[str], *, cwd: Path, env: dict, stdin_text: str | None
) -> tuple[str, int]:
    """Run, streaming output to the terminal as it arrives and collecting it.

    A handoff can run for twenty minutes; capturing silently and printing at the
    end is the wrong experience for the one step a human actually watches.
    """
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if stdin_text is not None:
            process.stdin.write(stdin_text)
        process.stdin.close()
    except BrokenPipeError:
        # The child exited without reading its prompt. Its output and exit code
        # below are the real story; the failed write is not.
        pass
    lines: list[str] = []
    for line in process.stdout:
        lines.append(line)
        sys.stdout.write(line)
    return "".join(lines), process.wait()


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
    try:
        output, exit_code = _tee(
            argv,
            cwd=workdir(cfg, ticket),
            env=step_env(cfg, ticket),
            stdin_text=stdin_text,
        )
    except OSError as exc:
        output = f"could not execute {argv[0]}: {exc}\n"
        exit_code = 127

    log_file.write_text(output)

    # Recorded relative to the store root, so the pointer survives the store
    # being moved; `StepResult.log` stays absolute because it is printed for a
    # human to open.
    record: dict = {"status": "done", "at": now(), "log": store.relative(log_file)}

    pr_ref = None
    match = PR_LINE.search(output)
    if match:
        pr_ref = match.group(1)
        prs = ticket.setdefault("prs", [])
        if pr_ref not in prs:
            prs.append(pr_ref)
        # Recorded against the step so `ticket reset` can undo it without any
        # step id being hardcoded in the engine.
        record["registered_pr"] = pr_ref

    worktree_match = WORKTREE_LINE.search(output)
    if worktree_match:
        ticket["worktree"] = worktree_match.group(1)
        record["registered_worktree"] = worktree_match.group(1)

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
