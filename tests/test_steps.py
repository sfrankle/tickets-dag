import os
import pty
import select
import signal
import sys
import textwrap
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from ticket import steps
from ticket.config import load_config
from ticket.steps import Interrupted, release_gate, run_step, tee

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
      - id: review-spec
        gate: true
        needs: [evaluate]
      - id: draft-pr
        run: scripts/draft-pr.sh
        needs: [review-spec]
      - id: describe
        model: haiku
        prompt: prompts/describe.md
        needs: [draft-pr]
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate the ticket.\n")
    (tmp_path / "prompts" / "describe.md").write_text("Describe the PR.\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    return load_config(path)


def ticket_doc():
    return {
        "key": "ABC-123",
        "repo": "acme/api",
        "prs": [],
        "steps": {},
        "tracked": True,
    }


def poll_until(check: str) -> str:
    """A script that prints `first`, waits for `check` to succeed, prints `second`.

    `check` is a shell test against the log, so the script can only finish if
    `tee` wrote that first line while the process was still running.
    """
    return (
        "echo first\n"
        "i=0\n"
        "while [ $i -lt 200 ]; do\n"
        f"  if {check}; then echo second; exit 0; fi\n"
        "  sleep 0.01\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 1\n"
    )


def write_script(cfg, name, body):
    path = cfg.root / "scripts" / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_script_step_success_is_recorded_done(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo working\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "done"
    assert ticket["steps"]["draft-pr"]["status"] == "done"
    assert store.read_ticket("ABC-123")["steps"]["draft-pr"]["status"] == "done"


def test_script_step_failure_records_exit_code_and_log(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo nope >&2\nexit 3\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "failed"
    assert ticket["steps"]["draft-pr"]["exit_code"] == 3
    assert "nope" in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_script_step_receives_ticket_env(cfg, store):
    write_script(cfg, "draft-pr.sh", 'echo "$TICKET_KEY $TICKET_REPO"\n')
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert "ABC-123 acme/api" in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_a_printed_pr_ref_is_registered_on_the_ticket(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.pr == "acme/api#115"
    assert ticket["prs"] == ["acme/api#115"]


def test_a_repeated_pr_ref_is_not_added_twice(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["prs"] == ["acme/api#115"]


def test_handoff_step_invokes_claude_with_the_resolved_model(cfg, store, fake_bin):
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("describe"))
    assert "claude-haiku-4-5-20251001" in fake_bin.calls_to("claude")[0]


def test_the_prompt_goes_on_stdin_not_in_argv(cfg, store, fake_bin):
    """Decision #21: a long review body would blow the argv size limit."""
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("describe"))
    assert "Describe the PR." in fake_bin.stdin_to("claude")[0]
    assert "Describe the PR." not in " ".join(fake_bin.calls_to("claude")[0])


def test_handoff_args_are_passed_through(cfg, store, fake_bin, tmp_path):
    """`args:` is where agent mode lives, so `implement` can actually write."""
    config = tmp_path / "config.yml"
    config.write_text(
        CONFIG.replace(
            "  - id: describe\n    model: haiku\n",
            "  - id: describe\n    model: haiku\n    args: [--permission-mode, acceptEdits]\n",
        )
    )
    from ticket.config import load_config as reload

    scoped = reload(config)
    run_step(scoped, store, ticket_doc(), scoped.step("describe"))
    assert "--permission-mode" in fake_bin.calls_to("claude")[0]


def test_a_step_runs_in_the_worktree_once_one_is_registered(cfg, store):
    write_script(cfg, "draft-pr.sh", "pwd\n")
    checkout = cfg.root / "checkout"
    checkout.mkdir()
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert str(checkout) in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_a_step_gets_the_worktree_and_branch_in_its_env(cfg, store):
    write_script(
        cfg,
        "draft-pr.sh",
        'echo "$TICKET_WORKTREE|$TICKET_BRANCH|$TICKET_USE_WORKTREES"\n',
    )
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    logged = store.read_log(ticket["steps"]["draft-pr"]["log"])
    assert "ABC-123|1" in logged


def test_an_announced_worktree_is_recorded_on_the_ticket(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-worktree: /tmp/checkout-abc'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["worktree"] == "/tmp/checkout-abc"
    assert ticket["steps"]["draft-pr"]["registered_worktree"] == "/tmp/checkout-abc"


def test_a_registered_pr_is_recorded_against_the_step_that_made_it(cfg, store):
    """`ticket reset` undoes registrations without knowing any step id."""
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["steps"]["draft-pr"]["registered_pr"] == "acme/api#115"


def test_a_step_fetches_before_it_runs(cfg, store, fake_bin):
    write_script(cfg, "draft-pr.sh", "echo hi\n")
    checkout = cfg.root / "checkout"
    (checkout / ".git").mkdir(parents=True)
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_handoff_step_defaults_to_the_default_model(cfg, store, fake_bin):
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("evaluate"))
    assert "claude-opus-5" in fake_bin.calls_to("claude")[0]


def test_handoff_failure_is_recorded(cfg, store, fake_bin):
    fake_bin.respond("claude", exit_code=1, stderr="model unavailable")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("evaluate"))
    assert result.status == "failed"


def test_gate_step_parks_without_running_anything(cfg, store, fake_bin):
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("review-spec"))
    assert result.status == "parked"
    assert "review-spec" not in ticket["steps"]
    assert fake_bin.calls == []


def test_release_gate_records_released(cfg, store):
    ticket = ticket_doc()
    release_gate(store, ticket, "review-spec")
    assert ticket["steps"]["review-spec"]["status"] == "released"
    assert store.read_ticket("ABC-123")["steps"]["review-spec"]["status"] == "released"


def test_dry_run_executes_nothing_and_records_nothing(cfg, store, fake_bin):
    write_script(cfg, "draft-pr.sh", "echo working\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"), dry_run=True)
    assert result.status == "dry-run"
    assert ticket["steps"] == {}
    assert store.read_ticket("ABC-123") is None


def test_a_missing_script_is_a_failure_not_a_crash(cfg, store):
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "failed"
    # The OSError path never reaches `tee`, so `run_step` writes this log itself.
    log = store.root / ticket["steps"]["draft-pr"]["log"]
    assert "could not execute" in log.read_text()


def test_tee_writes_each_line_as_it_arrives(cfg, tmp_path):
    """A step that is killed mid-run must still leave what it printed on disk."""
    log = tmp_path / "run.log"
    script = write_script(
        cfg, "streaming.sh", poll_until(f'grep -q first "{log}" 2>/dev/null')
    )
    output, exit_code = tee(
        [str(script)], cwd=tmp_path, env=dict(os.environ), stdin_text=None, log=log
    )
    assert exit_code == 0, "tee held the first line until the process exited"
    assert output == "first\nsecond\n"
    assert log.read_text() == "first\nsecond\n"


def test_a_steps_log_exists_before_the_step_finishes(cfg, store, tmp_path):
    """Same guarantee through `run_step`: the file it names is being written
    while the step runs, not once it is over."""
    logs = store.ticket_dir("ABC-123") / "logs"
    write_script(cfg, "draft-pr.sh", poll_until(f'grep -rq first "{logs}" 2>/dev/null'))
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "done"
    assert (store.root / ticket["steps"]["draft-pr"]["log"]).read_text() == (
        "first\nsecond\n"
    )


# --- stopping a step (#43) --------------------------------------------------


@pytest.fixture
def interrupt_after():
    """Raise `Interrupted` in the test's own thread after a delay, the way `cli.main`'s handler does on SIGTERM."""

    def arm(seconds: float) -> None:
        def handler(signum, _frame):
            raise Interrupted(signum)

        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)

    previous = signal.getsignal(signal.SIGALRM)
    yield arm
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous)


def gone(pid: int, within: float = 3.0) -> bool:
    """Whether `pid` has exited. An orphan is reaped by init, so give it a moment."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def pids(log) -> list[int]:
    return [int(line) for line in log.read_text().split()]


def test_stopping_tee_stops_the_child_and_what_it_started(
    cfg, tmp_path, interrupt_after
):
    """The reported case: `ticket` ended and the `claude` child ran on, reparented to init."""
    log = tmp_path / "run.log"
    # The step's own pid, then a grandchild's, then it waits on the grandchild.
    script = write_script(cfg, "forks.sh", "echo $$\nsleep 60 &\necho $!\nwait\n")
    interrupt_after(0.5)
    with pytest.raises(Interrupted):
        tee([str(script)], cwd=tmp_path, env=dict(os.environ), stdin_text=None, log=log)
    child, grandchild = pids(log)
    assert gone(child)
    assert gone(grandchild), "the grandchild outlived the step"


def test_a_child_that_ignores_sigterm_is_killed(
    cfg, tmp_path, interrupt_after, monkeypatch
):
    monkeypatch.setattr(steps, "GRACE_SECONDS", 0.2)
    log = tmp_path / "run.log"
    script = write_script(
        cfg, "stubborn.sh", "trap '' TERM\necho $$\nwhile :; do sleep 0.05; done\n"
    )
    interrupt_after(0.5)
    started = time.monotonic()
    with pytest.raises(Interrupted):
        tee([str(script)], cwd=tmp_path, env=dict(os.environ), stdin_text=None, log=log)
    assert time.monotonic() - started < 5
    assert gone(pids(log)[0])


def test_an_interrupted_step_is_recorded_with_what_it_printed(
    cfg, store, interrupt_after
):
    """Not left looking like it never ran, and re-runnable by `next` like any failure."""
    write_script(cfg, "draft-pr.sh", "echo first\nsleep 60\n")
    ticket = ticket_doc()
    interrupt_after(0.5)
    with pytest.raises(Interrupted):
        run_step(cfg, store, ticket, cfg.step("draft-pr"))
    record = store.read_ticket("ABC-123")["steps"]["draft-pr"]
    assert record["status"] == "failed"
    assert record["interrupted"] is True
    assert (store.root / record["log"]).read_text() == "first\n"


@pytest.fixture
def sigterm_at():
    """Send this process SIGTERM at each delay, with the handler `cli.main` installs."""
    timers: list[threading.Timer] = []

    def handler(signum, _frame):
        raise Interrupted(signum)

    def arm(*seconds: float) -> None:
        for delay in seconds:
            timer = threading.Timer(delay, os.kill, (os.getpid(), signal.SIGTERM))
            timers.append(timer)
            timer.start()

    previous = signal.signal(signal.SIGTERM, handler)
    yield arm
    for timer in timers:
        timer.cancel()
    signal.signal(signal.SIGTERM, previous)


def test_a_second_signal_during_the_grace_wait_does_not_skip_the_kill(
    cfg, tmp_path, sigterm_at, monkeypatch
):
    """An impatient second request to stop raised out of the wait, and a step that ignores SIGTERM ran on."""
    monkeypatch.setattr(steps, "GRACE_SECONDS", 1.0)
    log = tmp_path / "run.log"
    script = write_script(
        cfg, "stubborn.sh", "trap '' TERM\necho $$\nwhile :; do sleep 0.05; done\n"
    )
    sigterm_at(0.5, 1.0)
    with pytest.raises(Interrupted):
        tee([str(script)], cwd=tmp_path, env=dict(os.environ), stdin_text=None, log=log)
    assert gone(pids(log)[0]), "the step outlived a second SIGTERM"


def test_a_signal_while_the_prompt_is_written_stops_the_child(
    cfg, tmp_path, interrupt_after
):
    """A prompt larger than the pipe blocks the write until the child reads it, and a signal then used to skip the stop."""
    pidfile = tmp_path / "pid"
    script = write_script(cfg, "deaf.sh", f"echo $$ > {pidfile}\nsleep 60\n")
    interrupt_after(0.5)
    with pytest.raises(Interrupted):
        tee(
            [str(script)],
            cwd=tmp_path,
            env=dict(os.environ),
            stdin_text="x" * (1 << 20),
        )
    assert gone(pids(pidfile)[0]), "the step outlived the interrupted write"


def test_a_pr_announced_before_the_step_was_stopped_is_registered(
    cfg, store, interrupt_after
):
    """The PR is open whether or not the step finished, and a rerun that did not know would open a second."""
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#7'\nsleep 60\n")
    ticket = ticket_doc()
    interrupt_after(0.5)
    with pytest.raises(Interrupted):
        run_step(cfg, store, ticket, cfg.step("draft-pr"))
    stored = store.read_ticket("ABC-123")
    assert stored["prs"] == ["acme/api#7"]
    assert stored["active"] == "acme/api#7"
    record = stored["steps"]["draft-pr"]
    assert record["interrupted"] is True
    assert record["registered_pr"] == "acme/api#7"


# --- a step that holds the terminal -----------------------------------------

SRC = Path(steps.__file__).resolve().parents[1]


def in_a_terminal(code: str, conversation: list[tuple[bytes, bytes]]) -> str:
    """Run `code` in a fresh Python on its own pseudo-terminal, as the foreground job.

    For each (expected, reply) pair, wait until `expected` has been printed, then type `reply`. Returns everything printed.
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c", code])
    printed = b""
    deadline = time.monotonic() + 10

    def read_until(wanted: bytes | None) -> None:
        nonlocal printed
        while wanted is None or wanted not in printed:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(
                    f"timed out waiting for {wanted!r}; saw {printed!r}"
                )
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                chunk = b""
            if not chunk:
                if wanted is None:
                    return
                raise AssertionError(f"ended before {wanted!r}; saw {printed!r}")
            printed += chunk

    try:
        for expected, reply in conversation:
            read_until(expected)
            os.write(fd, reply)
        read_until(None)
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(fd)
    return printed.decode(errors="replace")


def terminal_program(script: str) -> str:
    return textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(SRC)!r})
        from ticket.steps import tee
        try:
            output, code = tee(["sh", "-c", {script!r}], cwd=".", env=dict(os.environ), stdin_text=None)
            print("exit", code)
        except KeyboardInterrupt:
            print("interrupted")
        print("ticket has the terminal:", os.tcgetpgrp(0) == os.getpgrp(), flush=True)
    """)


def test_a_step_can_ask_for_input_at_the_terminal():
    """An ssh passphrase or a git credential prompt: the step used to be stopped for touching the terminal, and `ticket` waited forever."""
    program = terminal_program(
        "printf 'passphrase? ' >/dev/tty; read answer </dev/tty; echo got $answer"
    )
    printed = in_a_terminal(program, [(b"passphrase? ", b"hunter2\n")])
    assert "got hunter2" in printed
    assert "exit 0" in printed
    assert "ticket has the terminal: True" in printed


def test_ctrl_c_at_a_step_that_holds_the_terminal_interrupts_ticket():
    """Ctrl-C now reaches the step's group, and `ticket` must still hear about it."""
    program = terminal_program("echo ready; sleep 60")
    printed = in_a_terminal(program, [(b"ready", b"\x03")])
    assert "interrupted" in printed
    assert "ticket has the terminal: True" in printed
