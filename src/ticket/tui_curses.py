"""The TUI's curses adapter (#28, #37): read a key, run the reducer, spawn, paint.

Deliberately small.
Everything with a decision in it lives in `tui.py` as a pure function with a test, because this file is the one part of the TUI a test cannot drive.
What is left here is a terminal, a clock, a stat and a `Popen`.

The adapter is also the only writer, and it writes the way #28 requires: never to the store's documents, only by running `ticket` itself in a child process that takes `store.lock(key)` on its own.
Mutual exclusion therefore stays the engine's, and the reducer refusing ENTER on a row it sees as running is a convenience that keeps the common case away from that error.
"""

from __future__ import annotations

import curses
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from . import tui, view
from .errors import TicketError
from .store import Store, is_safe_key

# One timer, one second (#28).
# A tick that finds the store unchanged does no work at all: rows are rebuilt only when the mtimes move.
POLL_MS = 1000

# How much of a log the pane holds in memory, and how far back from the end it reads to find it.
# The pane shows a screenful, and the rest is what `j` scrolls back through.
LOG_TAIL = 500
LOG_TAIL_BYTES = 64 * 1024


def spawn(
    store: Store, command: tui.Command, *, popen=subprocess.Popen
) -> tuple[subprocess.Popen, Path | None]:
    """Run one `ticket` command as a detached child.

    `-m` rather than the console script, so the run is always the same interpreter and the same installed version as the TUI it was started from.
    `start_new_session=True` puts the child in its own session, so a twenty-minute handoff outlives the TUI and survives the terminal closing — which is the whole reason the TUI spawns instead of running the verb in-process.

    A command with no ticket to it — `refresh` — has nowhere in the store to write to, and a `logs/` directory at the store root would read as a pre-#27 layout to the migration, so its output is discarded rather than misfiled.

    A key that cannot be a path segment is treated the same way, and for a sharper reason: `t` collects whatever is typed and `track` is the one verb whose ticket does not exist yet, so the text reaches here before any child has validated it.
    `cli.main` guards the same thing before its lock interpolates a key, but this runs first — naming the err file would `mkdir` the directory `../../oops` names before `track` ever got the chance to refuse it.
    Discarding the output rather than refusing the spawn keeps the rejection the CLI's, with its sentence.

    Returns the child and the file its output went to, so the caller can drop that file if the run had nothing to say.
    """
    argv = [sys.executable, "-m", "ticket", *command.argv]
    if command.key is None or not is_safe_key(command.key):
        return popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ), None
    path = store.spawn_err_path(command.key)
    # Our end closes as soon as `Popen` returns, by which point the child has its own dup of the descriptor.
    with open(path, "wb") as stream:
        return popen(
            argv,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ), path


def discard_empty(path: Path | None) -> None:
    """Drop a spawn err file the run had nothing to write to.

    One is named per keyed spawn, before the child exists and therefore before anything knows whether it will crash (see `spawn`), so a `run`, `open` or `findings` that works leaves a 0-byte file per keypress in that ticket's `logs/` — beside the real ones, and outnumbering them by the end of a session.
    Only ever called on a child that has already been reaped: the file is the live one's stdout.
    """
    if path is None:
        return
    try:
        if path.stat().st_size == 0:
            path.unlink()
    except OSError:
        # Already gone, or not ours to remove. Either way there is nothing here worth interrupting a repaint for.
        pass


def mark_running(rows: list[dict], pids: dict[str, int]) -> list[dict]:
    """Report a run this TUI started that the lock file has not caught up with.

    Liveness has two sources (#28) and this is the second one: `locks/<KEY>.lock` covers every run, including ones started in another terminal, but only once the child has got as far as taking it.
    Between the spawn and that moment the `Popen` handle is the only thing that knows, and without it ENTER would stay live on a row that is already starting.

    A new list of rows rather than a marker written into the ones passed in.
    The overlay has to disappear when the handle does, and the rows the adapter holds are a cache it rebuilds only when the store's mtimes move: marking one in place makes the overlay outlive the child by however long the store stays still, which for `o` and `f` — neither of which writes anything — is forever.
    Rebuilding it from `pids` every tick means there is nothing to clear.
    """
    marked = []
    for row in rows:
        pid = pids.get(row["key"])
        if pid is not None and not row.get("running"):
            marked.append({**row, "running": view.running(pid)})
        else:
            marked.append(row)
    return marked


def pulse(root: Path) -> tuple[int, int]:
    """A value that changes whenever anything `view.rows` reads does.

    Count and summed mtimes together, so an edit in place, a new file and a deleted one all move it.
    Cheaper than rebuilding rows, which is the point: the timer fires every second and almost every tick finds nothing.

    `logs/` is walked past deliberately.
    `tee` appends to a running step's log as the run writes (#29), so counting it would move the beat on every tick of every run — exactly when the store is busiest and the cache is worth the most — and nothing `rows` reads lives in there anyway.
    The pane's own tail is a separate read, and it is the pane that wants to see a log grow.
    """
    count = 0
    total = 0
    # `scandir` rather than `os.walk`, so the mtime comes off the directory entry the walk already read instead of a second lookup by path.
    pending = [root]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != "logs":
                        pending.append(Path(entry.path))
                    continue
                total += entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                # A file rotated away between the walk and the stat is a change like any other.
                # The next tick sees the store as it now is.
                continue
            count += 1
    return count, total


def tail(store: Store, recorded: str | None) -> tuple[str, ...]:
    """The end of a step's log, for the pane that auto-tails it.

    `tee` appends as the run writes (#29), so this reads a file that is still growing and a partial last line is normal rather than an error.
    Only the last `LOG_TAIL_BYTES` are read: a long run's log outgrows the pane by orders of magnitude, and this runs once a second for as long as the pane is open.
    """
    path = store.log_file(recorded)
    if path is None:
        return ()
    start = 0
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            start = max(0, handle.tell() - LOG_TAIL_BYTES)
            handle.seek(start)
            block = handle.read()
    except OSError:
        # A recorded path can outlive its file, and the pane is not the place to raise about it — `show` already reports a missing log.
        return ()
    lines = block.decode(errors="replace").splitlines()
    if start:
        # A block that does not start at the top of the file starts mid-line.
        lines = lines[1:]
    return tuple(lines[-LOG_TAIL:])


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
        try:
            screen.addnstr(index, 0, line, room)
        except curses.error:
            continue
    screen.refresh()


def loop(screen, ctx: view.Context) -> None:
    """Read a key, call the reducer, run what it asked for, repaint."""
    curses.curs_set(0)
    screen.timeout(POLL_MS)
    store = ctx.store
    state = tui.State()
    # A list rather than a dict keyed by ticket: `o` and `f` are not gated on `running`, so a second press on the same row is an ordinary thing to do and a dict would drop the first handle on the floor unreaped.
    runs: list[tuple[str, subprocess.Popen, Path | None]] = []
    cached: list[dict] = []
    seen: tuple[int, int] | None = None
    painted: tuple | None = None
    tailed: str | None = None

    while not state.quitting:
        beat = pulse(store.root)
        if beat != seen:
            cached = view.rows(ctx)
            seen = beat
        # Reaped rather than waited on, because a child that has finished is no longer evidence of anything: the lock and the store are.
        alive = []
        for key, run, err in runs:
            if run.poll() is None:
                alive.append((key, run, err))
            else:
                discard_empty(err)
        runs = alive
        rows = mark_running(cached, {key: run.pid for key, run, _ in runs})

        height, width = screen.getmaxyx()
        # `prepare` fills in the fields only a terminal can answer for and says which log the pane wants, so the ordering between them is a tested pure function rather than four calls in here.
        state, log = tui.prepare(state, rows, width, height)
        lines = tail(store, log)
        # The offset belongs to the log it was scrolled through. Moving the cursor to another ticket, or a rotated log coming back shorter, would otherwise leave it scrolled past the end of a file with content in it and the pane would say "(no output yet)" about it.
        offset = 0 if log != tailed else min(state.log_offset, max(len(lines) - 1, 0))
        state = replace(state, log_lines=lines, log_offset=offset)
        tailed = log

        # A tick that changed nothing has nothing to redraw, and an idle TUI over ssh should not spend a screenful of bytes a second saying so.
        # `state` carries the tail, so a log that grew still repaints.
        frame = (beat, height, width, state, tuple(key for key, _, _ in runs))
        if frame != painted:
            paint(screen, tui.render(state, rows, width, height))
            painted = frame

        try:
            pressed = screen.get_wch()
        except curses.error:
            # The 1s timer expiring, which is the common case and no work.
            continue
        state, commands = tui.handle_key(state, rows, key_name(pressed))
        for command in commands:
            child, err = spawn(store, command)
            if command.key:
                runs.append((command.key, child, err))
        if commands:
            # A spawn changes the store as soon as the child takes the lock, so do not wait out the rest of this second before looking.
            seen = None


def run() -> int:
    """`ticket tui`.

    Loaded with syncing off: the network is touched by `R` alone (#28), which spawns `ticket refresh` like any other verb.
    """
    ctx = view.Context.load(no_sync=True)
    try:
        curses.wrapper(loop, ctx)
    except curses.error as exc:
        # `ticket tui` piped, redirected or run under CI has no terminal to set up, and the raw `setupterm` error says nothing about which verb to reach for instead.
        raise TicketError(
            f"`ticket tui` needs a terminal curses can drive ({exc}). "
            f"Try `ticket show` or `ticket next` instead."
        ) from exc
    return 0
