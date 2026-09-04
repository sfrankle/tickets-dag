"""The TUI's curses adapter (#28, #37): read a key, run the reducer, spawn, paint.

Deliberately small.
Everything with a decision in it lives in `tui.py` as a pure function with a test, because this file is the one part of the TUI a test cannot drive.
What is left here is a terminal, a clock, a stat and a `Popen`.

The adapter is also the only writer, and it writes the way #28 requires: never to the store's documents, only by running `ticket` itself in a child process that takes `store.lock(key)` on its own.
Mutual exclusion therefore stays the engine's, and the reducer refusing ENTER on a row it sees as running is a convenience that keeps the common case away from that error.
"""

from __future__ import annotations

import contextlib
import curses
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import tui, view
from .store import Store

# One timer, one second (#28).
# A tick that finds the store unchanged does no work at all: rows are rebuilt only when the mtimes move.
POLL_MS = 1000

# How much of a log the pane holds in memory.
# The pane shows a screenful, and the rest is what `j` scrolls back through.
LOG_TAIL = 500


def _command_key(argv: list[str]) -> str | None:
    """The ticket key a command names, when it names one.

    Every verb the reducer emits takes the key first or takes none at all — `refresh` is the only keyless one — so this is a position, not a parse.
    """
    if len(argv) > 1 and not argv[1].startswith("-"):
        return argv[1]
    return None


def spawn_error_path(store: Store, key: str) -> Path:
    """Where a spawned run's stdout and stderr go.

    One file per spawn, beside that run's own logs.
    It is the only place an immediate crash can announce itself: the child dies before the engine has written anything, so without this the row simply never starts and says nothing about why.
    """
    directory = store.ticket_dir(key) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"spawn-{stamp}.err"


def spawn(store: Store, argv: list[str], *, popen=subprocess.Popen) -> subprocess.Popen:
    """Run one `ticket` command as a detached child.

    `-m` rather than the console script, so the run is always the same interpreter and the same installed version as the TUI it was started from.
    `start_new_session=True` puts the child in its own session, so a twenty-minute handoff outlives the TUI and survives the terminal closing — which is the whole reason the TUI spawns instead of running the verb in-process.

    A keyless command has nowhere in the store to write to, and a `logs/` directory at the store root would read as a pre-#27 layout to the migration, so its output is discarded rather than misfiled.
    """
    key = _command_key(argv)
    # The stack closes our end as soon as `Popen` returns, by which point the child has its own dup of the descriptor.
    with contextlib.ExitStack() as stack:
        stream = subprocess.DEVNULL
        if key:
            stream = stack.enter_context(open(spawn_error_path(store, key), "wb"))
        return popen(
            [sys.executable, "-m", "ticket", *argv],
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def mark_running(rows: list[dict], pids: dict[str, int]) -> None:
    """Report a run this TUI started that the lock file has not caught up with.

    Liveness has two sources (#28) and this is the second one: `locks/<KEY>.lock` covers every run, including ones started in another terminal, but only once the child has got as far as taking it.
    Between the spawn and that moment the `Popen` handle is the only thing that knows, and without it ENTER would stay live on a row that is already starting.
    """
    for row in rows:
        pid = pids.get(row["key"])
        if pid is not None and not row.get("running"):
            row["running"] = {"pid": pid, "since": None, "log": None}


def pulse(root: Path) -> tuple[int, int]:
    """A value that changes whenever anything in the store does.

    Count and summed mtimes together, so an edit in place, a new file and a deleted one all move it.
    Cheaper than rebuilding rows, which is the point: the timer fires every second and almost every tick finds nothing.
    """
    count = 0
    total = 0
    for directory, _subdirectories, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(directory, name)).st_mtime_ns
            except OSError:
                # A log rotated away between the walk and the stat is a change like any other.
                # The next tick sees the store as it now is.
                continue
            count += 1
    return count, total


def tail(store: Store, recorded: str | None) -> tuple[str, ...]:
    """The end of a step's log, for the pane that auto-tails it.

    `tee` appends as the run writes (#29), so this reads a file that is still growing and a partial last line is normal rather than an error.
    """
    path = store.log_file(recorded)
    if path is None:
        return ()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        # A recorded path can outlive its file, and the pane is not the place to raise about it — `show` already reports a missing log.
        return ()
    return tuple(text.splitlines()[-LOG_TAIL:])


def key_name(key: str | int) -> str:
    """One string for the reducer, whichever of the two shapes curses returned.

    `get_wch` gives a `str` for anything typed and an `int` for a keypad code, and `keyname` turns the second into the `KEY_DOWN` spelling `tui.py` already matches on.
    """
    if isinstance(key, str):
        return key
    return curses.keyname(key).decode(errors="replace")


def paint(screen, lines: list[str]) -> None:
    """Put `render`'s lines on the terminal, and nothing else.

    Writing the bottom-right cell scrolls the screen and curses raises rather than doing it, so the last line gives up its final column — which is a corner of the border, and the only thing on screen that is not exactly what `render` returned.
    """
    height, width = screen.getmaxyx()
    screen.erase()
    for index, line in enumerate(lines[:height]):
        room = width - 1 if index == height - 1 else width
        if room <= 0:
            continue
        try:
            screen.addnstr(index, 0, line, room)
        except curses.error:
            continue
    screen.noutrefresh()
    curses.doupdate()


def loop(screen, ctx: view.Context) -> None:
    """Read a key, call the reducer, run what it asked for, repaint."""
    curses.curs_set(0)
    screen.timeout(POLL_MS)
    store = ctx.store
    state = tui.State()
    runs: dict[str, subprocess.Popen] = {}
    rows: list[dict] = []
    seen: tuple[int, int] | None = None

    while not state.quitting:
        beat = pulse(store.root)
        if beat != seen:
            rows = view.rows(ctx)
            seen = beat
        # Reaped rather than waited on, because a child that has finished is no longer evidence of anything: the lock and the store are.
        runs = {key: run for key, run in runs.items() if run.poll() is None}
        mark_running(rows, {key: run.pid for key, run in runs.items()})

        height, width = screen.getmaxyx()
        # `viewport` is what the reducer scrolls by and `list_capacity` is what the paint uses, so filling one from the other is what stops the two disagreeing about how far a page is.
        row = tui.selected(state, rows)
        state = replace(
            state,
            viewport=tui.list_capacity(height),
            log_lines=tail(store, tui.log_path(row)),
            log_shown=tui.log_pane_open(state, row, width, height),
        )
        paint(screen, tui.render(state, rows, width, height))

        try:
            pressed = screen.get_wch()
        except curses.error:
            # The 1s timer expiring, which is the common case and no work.
            continue
        state, commands = tui.handle_key(state, rows, key_name(pressed))
        for command in commands:
            child = spawn(store, command)
            key = _command_key(command)
            if key:
                runs[key] = child
        if commands:
            # A spawn changes the store as soon as the child takes the lock, so do not wait out the rest of this second before looking.
            seen = None


def run() -> int:
    """`ticket tui`.

    Loaded with syncing off: the network is touched by `R` alone (#28), which spawns `ticket refresh` like any other verb.
    """
    ctx = view.Context.load(no_sync=True)
    curses.wrapper(loop, ctx)
    return 0
