"""The TUI's pure layer: screen state, `render`, and the key reducer (#28, #36).

Nothing here writes to the store, spawns a process or touches a terminal.
`handle_key` returns argv lists and the caller runs them, so the one writer stays the `ticket` CLI and its lock (#28, "the TUI is a frontend").
The curses adapter is #37 and is deliberately too small to hold a bug.

Rows are `view.row` dicts passed in, never held: a repaint after the poll is a new call with fresh rows rather than a mutation, and nothing here reads a row through the engine.
`view.open_suffix` is the one import, and it is a formatting rule rather than a reader: the count a row already carries has to read the same way in both frontends.

Two rules from #28 that this file exists to keep:

Step and review status are the words the store holds.
A display vocabulary invented here would drift from the resolver's, which is the first of the four bugs #26 shipped.

The only filter is "does this want me now", spelled `next.kind` being neither `rest` nor a gate.
There is no ready/blocked category, because `collect` sounds blocked and is runnable while a gate sounds ready and is the actual stop.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, replace

from .view import open_suffix

# Panel geometry. The list grows with the terminal and never with the content
# (#28), and below 90 columns the two panes stop sharing a line at all.
PANEL_FRACTION = 0.32
PANEL_MIN = 24
PANEL_MAX = 44
NARROW_WIDTH = 90
# Under 20 rows the log pane does not auto-open; there is not enough screen for
# the pipeline and a tail at once.
SHORT_HEIGHT = 20
FOOTER_LINES = 2

ENTER = frozenset({"\n", "\r", "ENTER", "KEY_ENTER"})
TAB = frozenset({"\t", "TAB", "KEY_TAB"})
ESCAPE = frozenset({"\x1b", "ESC", "ESCAPE"})
BACKSPACE = frozenset({"\x7f", "\b", "BACKSPACE", "KEY_BACKSPACE"})
DOWN = frozenset({"j", "DOWN", "KEY_DOWN"})
UP = frozenset({"k", "UP", "KEY_UP"})

# Marker per stored status. `running` is derived rather than stored, and an
# absent record is a step that has not run — neither is a new status word.
MARKERS = {
    "done": "x",
    "released": "x",
    "collected": "x",
    "failed": "!",
    "skipped": "-",
    "dispatched": ">",
    "running": ">",
    "pending": " ",
    None: " ",
}

HELP = [
    "ENTER  run whatever the resolver says is next",
    "o      open the PR        t  track a new key",
    "f      findings           R  refresh (network)",
    "p      cycle the inspected PR",
    "w      collapse the list to keys only",
    "Tab    focus              /  search",
    "b      only what wants attention",
    "L      collapse the log   j k  move",
    "?      this help          q  quit",
]


@dataclass(frozen=True)
class State:
    """Screen state only. The queue itself is `rows`, passed to every call.

    `cursor` indexes the *visible* rows rather than the full list, because search and the attention filter both change what is visible and a cursor into the unfiltered list would jump the moment a filter is toggled.
    Every branch that changes what is visible therefore re-clamps the cursor.

    `offset` is the viewport's first visible row, and `viewport` is how many rows the list showed on the last paint.
    The reducer needs the second to keep the cursor inside the first without knowing the terminal's height; `render` clamps both again, which is what makes a resize harmless rather than surprising.

    `inspecting` is an index into `prs` rather than a ref, so `p` is one modulo and a ticket with a single PR has no cycle to speak of.

    `mode` is where typed characters go: `list` for the key map, `search` for the `/` buffer, `track` for the key `t` is collecting.
    `log_lines` is what the adapter has tailed so far — screen state like the rest, since only the adapter reads files.
    """

    cursor: int = 0
    offset: int = 0
    viewport: int = 0
    search: str = ""
    mode: str = "list"
    entry: str = ""
    filtered: bool = False
    focus: str = "list"
    narrow_pane: str = "list"
    keys_only: bool = False
    log_collapsed: bool = False
    log_lines: tuple[str, ...] = ()
    log_offset: int = 0
    inspecting: int = 0
    help_open: bool = False
    quitting: bool = False


@dataclass(frozen=True)
class Contextual:
    """What ENTER will do, named before it is pressed.

    `command` is `None` wherever ENTER is inert, so "there is nothing to press here" is one fact held in one place rather than two that can disagree.
    """

    label: str
    command: list[str] | None


def wants_attention(row: dict) -> bool:
    """The only filter in the TUI, and the whole of it.

    `rest` is nothing to do and a gate is a stop this tool deliberately does not release (#28), so everything else is work waiting on the person.
    """
    return row["next"]["kind"] not in ("rest", "gate")


def _matches(row: dict, needle: str) -> bool:
    """Subsequence match over key, repo and summary, folded to lower case."""
    haystack = f"{row['key']} {row.get('repo', '')} {row.get('summary', '')}".lower()
    position = 0
    for char in needle.lower():
        position = haystack.find(char, position) + 1
        if position == 0:
            return False
    return True


def visible_rows(state: State, rows: list[dict]) -> list[dict]:
    """The rows the list is currently showing, in queue order."""
    shown = [row for row in rows if not state.filtered or wants_attention(row)]
    if state.search:
        shown = [row for row in shown if _matches(row, state.search)]
    return shown


def _clamp(index: int, total: int) -> int:
    """An index seated inside a list of `total` rows, and 0 when there are none."""
    return min(max(index, 0), max(total - 1, 0))


def _row_at(shown: list[dict], cursor: int) -> dict | None:
    return shown[_clamp(cursor, len(shown))] if shown else None


def selected(state: State, rows: list[dict]) -> dict | None:
    return _row_at(visible_rows(state, rows), state.cursor)


def contextual(state: State, rows: list[dict]) -> Contextual:
    """The contextual line and ENTER's argv, derived together.

    #28 makes this the safety mechanism: the line names what ENTER will do, and if the two were computed separately they would eventually disagree — which is exactly the failure the line exists to prevent.

    A gate names the literal command to copy and returns no argv.
    Releasing a gate from the TUI is out of scope for v1, so this is the whole of what the TUI has to say about one.
    """
    return _contextual(selected(state, rows))


def _contextual(row: dict | None) -> Contextual:
    if row is None:
        return Contextual("no tickets", None)
    key = row["key"]
    action = row["next"]
    target = action["target"] or ""
    if row.get("running"):
        # The engine would refuse the second run anyway; disabling ENTER here
        # keeps the common case away from that error (#28, run model).
        return Contextual(f"running {target}  ·  ENTER disabled".rstrip(), None)
    kind = action["kind"]
    if kind == "gate":
        return Contextual(f"ticket release {key} {target}", None)
    if kind == "step":
        return Contextual(f"ENTER run {target}", ["run", key, target])
    if kind == "review":
        return Contextual(f"ENTER dispatch {target}", ["review", key, target])
    if kind == "collect":
        return Contextual("ENTER collect", ["collect", key])
    if kind == "fix":
        return Contextual(f"ENTER fix {target}", ["fix", key, target])
    return Contextual("nothing to run", None)


def panel_width(width: int) -> int:
    """Columns the list panel occupies, borders included."""
    return min(max(int(width * PANEL_FRACTION), PANEL_MIN), PANEL_MAX)


def list_capacity(height: int) -> int:
    """Rows the list shows, which is the same number the reducer scrolls by.

    Exported so the adapter can put it in `viewport`, and called by `render` so the reducer, the paint and the adapter cannot disagree about how far a page is.
    """
    return max(height - FOOTER_LINES - 2, 0)


def _clamp_offset(offset: int, cursor: int, capacity: int, total: int) -> int:
    """Keep the cursor inside the viewport, which is the whole point of one.

    #26 sliced the rendered lines to the terminal height instead, so with more tickets than rows the selection scrolled off screen and could not be reached.
    """
    if capacity <= 0:
        return 0
    offset = max(0, min(offset, max(0, total - capacity)))
    if cursor < offset:
        return cursor
    if cursor >= offset + capacity:
        return cursor - capacity + 1
    return offset


def _inspected(state: State, row: dict) -> dict | None:
    """The PR the detail pane is looking at.

    `inspecting` counts from the active PR rather than from `prs[0]`, so the pane opens on the one ENTER will act against and `p` walks away from it.
    """
    prs = row.get("prs") or []
    if not prs:
        return None
    active = next((i for i, pr in enumerate(prs) if pr["active"]), len(prs) - 1)
    return prs[(active + state.inspecting) % len(prs)]


def _log_path(row: dict | None) -> str | None:
    """The log the pane tails: the running step's, else the last one recorded."""
    if row is None:
        return None
    running = row.get("running")
    if running and running.get("log"):
        return running["log"]
    logs = [step["log"] for step in row.get("steps") or [] if step.get("log")]
    return logs[-1] if logs else None


# --- reducer ----------------------------------------------------------------


def _clamp_cursor(state: State, rows: list[dict]) -> State:
    """Re-seat the cursor after anything that changes what is visible."""
    total = len(visible_rows(state, rows))
    cursor = _clamp(state.cursor, total)
    capacity = state.viewport or total
    offset = _clamp_offset(state.offset, cursor, capacity, total)
    return replace(state, cursor=cursor, offset=offset, inspecting=0)


def _typed(state: State, rows: list[dict], key: str) -> tuple[State, list[list[str]]]:
    """Characters while `/` or `t` is collecting them."""
    buffer = "search" if state.mode == "search" else "entry"
    text = getattr(state, buffer)
    if key in ESCAPE:
        # Cancelling a search restores the full list, so the cursor is re-seated
        # the same way toggling the filter is.
        cleared = replace(state, mode="list", **{buffer: ""})
        return _clamp_cursor(cleared, rows), []
    if key in ENTER:
        if state.mode == "track" and text:
            return replace(state, mode="list", entry=""), [["track", text]]
        return replace(state, mode="list"), []
    if key in BACKSPACE:
        return _clamp_cursor(replace(state, **{buffer: text[:-1]}), rows), []
    if len(key) == 1 and key.isprintable():
        return _clamp_cursor(replace(state, **{buffer: text + key}), rows), []
    return state, []


def handle_key(
    state: State, rows: list[dict], key: str
) -> tuple[State, list[list[str]]]:
    """The key map from #28 as a pure reducer.

    `commands` are argv lists for the `ticket` CLI, minus the interpreter and the module — the caller builds `[sys.executable, "-m", "ticket", ...]` around them and is the only thing that ever runs one.
    """
    if state.mode != "list":
        return _typed(state, rows, key)

    row = selected(state, rows)

    if key == "q":
        return replace(state, quitting=True), []
    if key == "?":
        return replace(state, help_open=not state.help_open), []
    if key in ENTER:
        command = contextual(state, rows).command
        return state, [command] if command else []
    if key in TAB:
        # Two jobs, because the reducer has no width and cannot tell which layout is on screen: `narrow_pane` is what the one-pane path reads and is inert in the two-pane one, while `focus` decides whether `j`/`k` move the cursor or scroll the log.
        # The log can only take focus when there is a tail to scroll, which is the adapter's answer, arriving as `log_lines`.
        pane = "detail" if state.narrow_pane == "list" else "list"
        scrollable = bool(state.log_lines) and not state.log_collapsed
        if state.focus == "log":
            focus = "list"
        elif scrollable:
            focus = "log"
        else:
            focus = "detail" if pane == "detail" else "list"
        return replace(state, narrow_pane=pane, focus=focus), []
    if key in ESCAPE:
        return replace(state, narrow_pane="list", focus="list", help_open=False), []
    if key == "/":
        return replace(state, mode="search"), []
    if key == "b":
        return _clamp_cursor(replace(state, filtered=not state.filtered), rows), []
    if key == "t":
        return replace(state, mode="track", entry=""), []
    if key == "w":
        return replace(state, keys_only=not state.keys_only), []
    if key == "L":
        return replace(state, log_collapsed=not state.log_collapsed), []
    if key == "R":
        return state, [["refresh"]]
    if key in DOWN or key in UP:
        step = 1 if key in DOWN else -1
        if state.focus == "log":
            highest = max(len(state.log_lines) - 1, 0)
            offset = min(max(state.log_offset + step, 0), highest)
            return replace(state, log_offset=offset), []
        return _clamp_cursor(replace(state, cursor=state.cursor + step), rows), []

    if row is None:
        return state, []
    if key == "p":
        prs = row.get("prs") or []
        if len(prs) < 2:
            return state, []
        return replace(state, inspecting=(state.inspecting + 1) % len(prs)), []
    if key == "o":
        return (state, [["open", row["key"]]]) if row.get("pr") else (state, [])
    if key == "f":
        inspected = _inspected(state, row)
        if inspected is None:
            return state, []
        command = ["findings", row["key"]]
        if not inspected["active"]:
            # `p` moves what the detail pane inspects without moving the engine's pointer, so an older PR has to be named explicitly.
            # `--pr` takes the number rather than the ref: `cli.pick_pr` matches on the `#N` suffix, so a whole `owner/repo#N` matches nothing and the run fails.
            command += ["--pr", inspected["ref"].rsplit("#", 1)[-1]]
        return state, [command]
    return state, []


# --- render -----------------------------------------------------------------


def _cell(text: str, width: int) -> str:
    """One panel-width cell. Truncation is a stable prefix, never an ellipsis that moves as the panel resizes."""
    return text[:width].ljust(width)


def _title(text: str, width: int) -> str:
    return f"- {text} ".ljust(width, "-")[:width]


def _list_lines(
    state: State, shown: list[dict], width: int, offset: int, capacity: int
) -> list[str]:
    # The key column is sized to the longest *visible* key, so the summaries
    # stay aligned while scrolling without reserving room for a key the filter
    # has taken away.
    keys = max((len(row["key"]) for row in shown), default=0)
    lines = []
    for index in range(offset, min(len(shown), offset + capacity)):
        row = shown[index]
        marker = ">" if index == state.cursor else " "
        if state.keys_only:
            lines.append(f"{marker}{row['key']}"[:width])
            continue
        running = "*" if row.get("running") else " "
        # A row with no summary shows the repo, and with neither is just the key.
        text = row.get("summary") or row.get("repo") or ""
        lines.append(f"{marker}{row['key']:<{keys}} {running} {text}".rstrip()[:width])
    return lines


def _pipeline_lines(row: dict, width: int) -> list[str]:
    """Steps and reviews as one pipeline in config order, the way the resolver walks them."""
    action = row["next"]
    entries: list[tuple[str, str, str]] = []
    for step in row.get("steps") or []:
        status = step["status"]
        if (
            row.get("running")
            and action["kind"] == "step"
            and action["target"] == step["id"]
        ):
            status = "running"
        entries.append((step["id"], step["kind"], status or ""))
    for review in row.get("reviews") or []:
        entries.append((review["id"], "review", review["status"]))
    ids = max((len(entry[0]) for entry in entries), default=0)
    lines = []
    for name, kind, status in entries:
        marker = MARKERS.get(status or None, " ")
        lines.append(f"[{marker}] {name:<{ids}}  {kind:<8} {status}".rstrip()[:width])
    return lines


def _detail_lines(state: State, row: dict | None, width: int) -> list[str]:
    if row is None:
        return ["no ticket selected"]
    lines = [(row.get("summary") or row.get("repo") or row["key"])[:width]]
    prs = row.get("prs") or []
    inspected = _inspected(state, row)
    if inspected:
        count = inspected["open_findings"]
        findings = f"    {count} open findings" if count else ""
        lines.append(f"PR: {inspected['ref']}{findings}"[:width])
    if len(prs) > 1:
        # `next` only ever drives the active PR, so an older one's open findings
        # are invisible unless they are listed here (#28, multiple PRs).
        for pr in prs:
            active = "*" if pr["active"] else " "
            cursor = ">" if pr is inspected else " "
            suffix = open_suffix(pr["open_findings"])
            lines.append(f" {cursor}{active} {pr['ref']}{suffix}"[:width])
    lines.append("")
    lines += _pipeline_lines(row, width)
    lines.append("")
    action = row["next"]
    lines.append(f"next: {action['kind']} {action['target'] or ''}".rstrip()[:width])
    lines += textwrap.wrap(action["reason"], max(width, 1)) or [""]
    if row.get("lock") == "stale":
        lines.append(f"stale lock: {row['lock_path']}"[:width])
    return lines


def _log_lines(state: State, width: int, capacity: int) -> list[str]:
    tail = list(state.log_lines)[state.log_offset :]
    if not tail:
        return ["(no output yet)"[:width]]
    return [f"> {line}"[:width] for line in tail[:capacity]]


def _header(row: dict | None) -> str:
    if row is None:
        return "detail"
    return f"{row['key']}  {row.get('repo', '')}".rstrip()


def _log_open(state: State, row: dict | None, height: int) -> bool:
    if state.log_collapsed or height < SHORT_HEIGHT:
        return False
    return _log_path(row) is not None


def _footer(
    state: State, shown: list[dict], row: dict | None, total: int, width: int
) -> list[str]:
    """The two footer lines. `render` has the visible rows and the selection already, so the footer is given them rather than filtering the queue three more times to find them again."""
    parts = [_contextual(row).label]
    if row:
        if row.get("open_findings"):
            parts.append(f"f {row['open_findings']} findings")
        if row.get("pr"):
            parts.append(f"o PR {row['pr']}")
    status = [f"{len(shown)}/{total} tickets"]
    if state.filtered:
        status.append("b attention only")
    if state.mode == "search" or state.search:
        status.append(f"/ {state.search}")
    if state.mode == "track":
        status.append(f"track {state.entry}")
    status.append("? help")
    status.append("q quit")
    return [
        _cell(" " + "    ".join(parts), width),
        _cell(" " + "  ·  ".join(status), width),
    ]


def _frame(
    left: list[str],
    right: list[str],
    left_title: str,
    right_title: str,
    log_title: str | None,
    log: list[str],
    width: int,
    height: int,
) -> list[str]:
    """Two panes side by side, with the log pane cut into the right one."""
    panel = min(panel_width(width), max(width - 20, PANEL_MIN))
    left_w = panel - 2
    right_w = width - panel - 1
    body = max(height - 2, 0)
    lines = [f"+{_title(left_title, left_w)}+{_title(right_title, right_w)}+"]
    log_h = len(log) + 1 if log_title is not None else 0
    detail_h = max(body - log_h, 0)
    for index in range(body):
        cell = _cell(left[index] if index < len(left) else "", left_w)
        if log_title is not None and index == detail_h:
            lines.append(f"|{cell}+{_title(log_title, right_w)}+")
            continue
        if index > detail_h and log_title is not None:
            entry = log[index - detail_h - 1] if index - detail_h - 1 < len(log) else ""
        else:
            entry = right[index] if index < len(right) else ""
        lines.append(f"|{cell}|{_cell(entry, right_w)}|")
    lines.append(f"+{'-' * left_w}+{'-' * right_w}+")
    return lines


def _one_pane(lines: list[str], title: str, width: int, height: int) -> list[str]:
    inner = width - 2
    body = max(height - 2, 0)
    out = [f"+{_title(title, inner)}+"]
    for index in range(body):
        out.append(f"|{_cell(lines[index] if index < len(lines) else '', inner)}|")
    out.append(f"+{'-' * inner}+")
    return out


def render(state: State, rows: list[dict], width: int, height: int) -> list[str]:
    """The whole screen as `height` lines of exactly `width` columns.

    Nothing scrolls horizontally: every cell is truncated to the space it has, so a long summary is short here and whole in the detail pane.
    """
    shown = visible_rows(state, rows)
    cursor = _clamp(state.cursor, len(shown))
    state = replace(state, cursor=cursor)
    row = _row_at(shown, cursor)
    body = max(height - FOOTER_LINES, 0)
    capacity = list_capacity(height)
    offset = _clamp_offset(state.offset, cursor, capacity, len(shown))
    footer = _footer(state, shown, row, len(rows), width)

    if state.help_open:
        return _one_pane(HELP, "help", width, body) + footer

    if width < NARROW_WIDTH:
        # A different path rather than the same one with smaller numbers: one
        # pane at a time is a different screen, and `Tab` is what swaps them.
        inner = width - 2
        if state.narrow_pane == "list":
            lines = _list_lines(state, shown, inner, offset, capacity)
            title = "tickets"
        else:
            lines = _detail_lines(state, row, inner)
            title = _header(row)
        return _one_pane(lines, title, width, body) + footer

    panel = min(panel_width(width), max(width - 20, PANEL_MIN))
    left = _list_lines(state, shown, panel - 2, offset, capacity)
    right_w = width - panel - 1
    detail = _detail_lines(state, row, right_w)
    log_title = None
    log: list[str] = []
    if _log_open(state, row, height):
        path = _log_path(row)
        log_title = f"log  {path.rsplit('/', 1)[-1]}" if path else "log"
        log = _log_lines(state, right_w, max((body - 2) // 3, 1))
    frame = _frame(left, detail, "tickets", _header(row), log_title, log, width, body)
    return frame + footer
