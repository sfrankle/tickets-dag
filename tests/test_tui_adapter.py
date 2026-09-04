"""The curses adapter (#37): the spawn, and what the two liveness sources say.

Nothing here drives curses.
The adapter is a terminal, a clock, a stat and a `Popen`, and the only parts worth pinning are the ones that would go wrong silently: a run started with the wrong interpreter or in the TUI's own session, and a lock left behind by a run that died.
"""

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import dead_pid, write_lock
from ticket import tui, tui_curses, view
from ticket.cli import main
from ticket.store import Store

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5}
    defaults: {model: opus}
    steps:
      - id: implement
        prompt: prompts/implement.md
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "implement.md").write_text("Implement.\n")
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


@pytest.fixture
def tracked(env):
    main(["track", "ABC-123", "--repo", "acme/api"])
    return Store(env / "store")


@pytest.fixture
def popen():
    """A `Popen` that records how it was called and never starts anything."""
    calls: list[tuple[list[str], dict]] = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4823, poll=lambda: None)

    fake.calls = calls
    return fake


# --- spawn ------------------------------------------------------------------


def test_spawn_runs_the_module_the_reducer_asked_for(tracked, popen):
    """`-m` and this interpreter, so a run is always the version that started it."""
    command = tui.contextual(tui.State(), view.rows(view.Context.load())).command
    tui_curses.spawn(tracked, command, popen=popen)
    argv, kwargs = popen.calls[0]

    assert command == tui.Command("ABC-123", ("run", "ABC-123", "implement"))
    assert argv == [sys.executable, "-m", "ticket", "run", "ABC-123", "implement"]
    assert kwargs["start_new_session"] is True


def test_spawn_sends_the_child_s_output_to_the_ticket_s_err_file(tracked, popen):
    """The only place an immediate crash can announce itself (#28, run model)."""
    tui_curses.spawn(
        tracked, tui.Command("ABC-123", ("run", "ABC-123", "implement")), popen=popen
    )
    _argv, kwargs = popen.calls[0]
    path = Path(kwargs["stdout"].name)

    assert path.parent == tracked.ticket_dir("ABC-123") / "logs"
    assert path.name.startswith("spawn-") and path.suffix == ".err"
    assert kwargs["stderr"] is subprocess.STDOUT


def test_a_keyless_spawn_writes_nowhere_in_the_store(tracked, popen):
    """`refresh` names no ticket, and a `logs/` at the store root would read as a pre-#27 layout to the migration."""
    tui_curses.spawn(tracked, tui.Command(None, ("refresh",)), popen=popen)
    _argv, kwargs = popen.calls[0]

    assert kwargs["stdout"] is subprocess.DEVNULL


# --- liveness ---------------------------------------------------------------


def test_a_dead_pid_renders_a_stale_lock_and_not_a_running_row(tracked):
    """The second liveness source, read the way `render` shows it.

    The lock file is what covers runs this TUI did not start, and the pid it already records is the whole of the difference between a run still working and one that died without releasing.
    """
    path = write_lock(tracked, f"{dead_pid()}\n")
    rows = view.rows(view.Context.load())
    lines = tui.render(tui.State(), rows, 160, 40)
    # A tmp path is longer than the pane, and nothing scrolls horizontally, so
    # what is on screen is a prefix of it.
    stale = next(line for line in lines if "stale lock" in line)
    shown = stale.split("stale lock: ", 1)[1].rstrip(" |")

    assert rows[0]["running"] is None
    assert shown and str(path).startswith(shown)
    assert "running" not in "\n".join(lines)


def test_a_run_this_tui_started_is_running_before_the_lock_appears(tracked):
    """The first source: between the spawn and the child taking the lock, the handle is the only thing that knows."""
    rows = view.rows(view.Context.load())
    tui_curses.mark_running(rows, {"ABC-123": 4823})

    assert rows[0]["running"]["pid"] == 4823
    assert tui.contextual(tui.State(), rows).command is None


# --- the tick ---------------------------------------------------------------


def test_the_pulse_does_not_move_when_only_a_log_grows(tracked):
    """A running step appends to its log every few moments (#29), and rows read none of it.

    Counting `logs/` would move the beat on every tick of every run, which is when the cache is worth the most.
    """
    log = tracked.log_path("ABC-123", "implement")
    log.write_text("one\n")
    before = tui_curses.pulse(tracked.root)
    log.write_text("one\ntwo\n")

    assert tui_curses.pulse(tracked.root) == before


def test_the_pulse_moves_when_a_ticket_does(tracked):
    """The other half: what rows do read has to be seen."""
    before = tui_curses.pulse(tracked.root)
    main(["track", "ABC-124", "--repo", "acme/api"])

    assert tui_curses.pulse(tracked.root) != before


def test_the_tail_reads_the_end_of_a_long_log_and_not_all_of_it(tracked):
    """The pane shows a screenful, and this runs once a second for as long as it is open."""
    log = tracked.log_path("ABC-123", "implement")
    log.write_text("".join(f"line {n}\n" for n in range(200_000)))
    lines = tui_curses.tail(tracked, tracked.relative(log))

    assert len(lines) == tui_curses.LOG_TAIL
    assert lines[-1] == "line 199999"
    # A block that starts mid-line drops its first, so nothing is reported as a
    # line that was never written as one.
    assert all(line.startswith("line ") for line in lines)
