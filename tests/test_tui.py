"""The TUI's pure layer (#36): `render` as text, `handle_key` as data.

No curses and no subprocess, because there is nothing here that needs either.
`commands` are argv lists and these tests never execute one — that is the point of returning them rather than running them.

The rows are hand-built dicts in the shape `view.row` returns.
Building them here rather than through a store keeps these tests about the screen: what `view` puts in the dict is `test_view.py`'s question.
"""

import ast
from pathlib import Path

from ticket import tui
from ticket.tui import State, contextual, handle_key, render

TUI_SOURCE = Path(tui.__file__)

# Anything that writes to the store, spawns, or touches the terminal. `view` is
# the one engine module `tui.py` is allowed to import (#28).
DENIED = {
    "ticket.cli",
    "ticket.collect",
    "ticket.effort",
    "ticket.fix",
    "ticket.gh",
    "ticket.reviews",
    "ticket.steps",
    "ticket.store",
    "curses",
    "os",
    "pathlib",
    "shutil",
    "subprocess",
    "sys",
}


def make_row(key, **overrides):
    row = {
        "key": key,
        "repo": "acme/api",
        "summary": f"{key} summary",
        "pr": None,
        "prs": [],
        "next": {"kind": "step", "target": "implement", "reason": "needs are met"},
        "open_findings": 0,
        "steps": [
            {
                "id": "evaluate",
                "kind": "handoff",
                "status": "done",
                "log": None,
                "log_missing": False,
            },
            {
                "id": "implement",
                "kind": "handoff",
                "status": None,
                "log": None,
                "log_missing": False,
            },
        ],
        "reviews": [],
        "running": None,
        "lock": None,
        "lock_path": None,
    }
    row.update(overrides)
    return row


def gate_row(key="ABC-140"):
    return make_row(
        key,
        next={
            "kind": "gate",
            "target": "review-spec",
            "reason": "parked at review-spec",
        },
        steps=[
            {
                "id": "review-spec",
                "kind": "gate",
                "status": None,
                "log": None,
                "log_missing": False,
            }
        ],
    )


# --- render -----------------------------------------------------------------


def test_render_is_exactly_the_terminal_at_80x24():
    lines = render(State(), [make_row("ABC-123"), gate_row()], 80, 24)
    assert len(lines) == 24
    assert {len(line) for line in lines} == {80}


def test_render_names_what_enter_will_do():
    lines = render(State(), [make_row("ABC-123")], 80, 24)
    assert "ENTER run implement" in lines[-2]


def test_render_at_a_gate_shows_the_command_to_copy():
    """No key to press, so the line is the literal `ticket release` to type."""
    lines = render(State(), [gate_row("ABC-140")], 80, 24)
    assert "ticket release ABC-140 review-spec" in lines[-2]
    assert "ENTER" not in lines[-2]


def test_render_shows_the_stored_status_word():
    rows = [make_row("ABC-123")]
    screen = "\n".join(render(State(), rows, 120, 40))
    assert "[x] evaluate" in screen
    assert "done" in screen
    # Nothing invented alongside the store's vocabulary (#28, bug one).
    assert "active" not in screen
    assert "blocked" not in screen


def test_render_at_120x40_keeps_the_panel_off_the_content():
    rows = [make_row("ABC-123", summary="x" * 200)]
    lines = render(State(), rows, 120, 40)
    assert len(lines) == 40
    assert {len(line) for line in lines} == {120}
    # 32% of 120 is 38 columns of panel, so the divider sits at the same column on every row.
    assert {line[37] for line in lines[:-2]} == {"+", "|"}


def test_render_at_60x20_is_one_pane_and_tab_swaps_it():
    rows = [make_row("ABC-123", summary="Fix auth token expiry")]
    listing = render(State(), rows, 60, 20)
    assert len(listing) == 20
    assert {len(line) for line in listing} == {60}
    assert "tickets" in listing[0]
    assert "acme/api" not in "\n".join(listing[:-2])

    swapped, _ = handle_key(State(), rows, "\t")
    detail = render(swapped, rows, 60, 20)
    assert "ABC-123  acme/api" in detail[0]
    assert "Fix auth token expiry" in "\n".join(detail)


def test_a_row_with_no_summary_falls_back_to_the_repo_then_the_key():
    rows = [
        make_row("ABC-1", summary=""),
        make_row("ABC-2", summary="", repo=""),
    ]
    screen = "\n".join(render(State(), rows, 80, 24))
    assert "ABC-1   acme/api" in screen
    assert "ABC-2" in screen


def test_the_list_is_a_viewport_under_the_cursor():
    """#26's bug: the rendered lines were sliced to the terminal height, so past row 20 the selection could not be reached."""
    rows = [make_row(f"ABC-{n:03d}") for n in range(40)]
    state = State(cursor=37, viewport=tui.list_capacity(24))
    screen = "\n".join(render(state, rows, 80, 24))
    assert ">ABC-037" in screen
    assert "ABC-000" not in screen


def test_the_log_pane_does_not_auto_open_under_20_rows():
    row = make_row(
        "ABC-123",
        running={"pid": 1, "since": "x", "log": "tickets/ABC-123/logs/implement.log"},
    )
    assert "implement.log" in "\n".join(render(State(), [row], 100, 24))
    assert "implement.log" not in "\n".join(render(State(), [row], 100, 19))


def test_keys_only_drops_the_summaries():
    rows = [make_row("ABC-123", summary="Fix auth token expiry")]
    screen = "\n".join(render(State(keys_only=True), rows, 100, 30))
    assert ">ABC-123" in screen
    # The full summary is still in the detail pane, which is what `w` buys room for.
    assert screen.count("Fix auth token expiry") == 1


# --- handle_key -------------------------------------------------------------


def test_enter_at_a_gate_emits_nothing():
    state, commands = handle_key(State(), [gate_row()], "\n")
    assert commands == []
    assert state == State()


def test_enter_on_a_failed_step_runs_it():
    row = make_row(
        "ABC-123",
        steps=[
            {
                "id": "implement",
                "kind": "handoff",
                "status": "failed",
                "log": None,
                "log_missing": False,
            }
        ],
    )
    _, commands = handle_key(State(), [row], "\n")
    assert commands == [["run", "ABC-123", "implement"]]


def test_enter_on_a_running_row_emits_nothing():
    """The engine's lock would refuse it; the TUI keeps the common case away from that error."""
    row = make_row("ABC-123", running={"pid": 4823, "since": "x", "log": None})
    _, commands = handle_key(State(), [row], "\n")
    assert commands == []
    assert contextual(State(), [row]).command is None


def test_enter_collects_and_fixes_by_the_resolver_s_answer():
    collect = make_row(
        "ABC-1",
        pr="acme/api#1",
        next={"kind": "collect", "target": "correctness", "reason": "r"},
    )
    fix = make_row(
        "ABC-2", pr="acme/api#2", next={"kind": "fix", "target": "f03", "reason": "r"}
    )
    assert handle_key(State(), [collect], "\n")[1] == [["collect", "ABC-1"]]
    assert handle_key(State(), [fix], "\n")[1] == [["fix", "ABC-2", "f03"]]


def test_p_cycles_the_inspected_pr_without_emitting():
    row = make_row(
        "ABC-123",
        pr="acme/api#115",
        prs=[
            {"ref": "acme/api#112", "open_findings": 3, "active": False},
            {"ref": "acme/api#115", "open_findings": 2, "active": True},
        ],
    )
    state, commands = handle_key(State(), [row], "p")
    assert commands == []
    assert state.inspecting == 1
    assert handle_key(state, [row], "p")[0].inspecting == 0


def test_f_names_an_older_pr_because_p_did_not_move_the_pointer():
    row = make_row(
        "ABC-123",
        pr="acme/api#115",
        prs=[
            {"ref": "acme/api#112", "open_findings": 3, "active": False},
            {"ref": "acme/api#115", "open_findings": 2, "active": True},
        ],
    )
    # The pane opens on the active PR, which is the one the engine's pointer names.
    assert handle_key(State(), [row], "f")[1] == [["findings", "ABC-123"]]
    # The number, not the ref: `cli.pick_pr` matches on the `#N` suffix.
    assert handle_key(State(inspecting=1), [row], "f")[1] == [
        ["findings", "ABC-123", "--pr", "112"]
    ]


def test_b_narrows_to_what_wants_attention():
    """`collect` sounds blocked and is runnable, a gate sounds ready and is the stop (#28, bug three)."""
    rows = [
        make_row("ABC-1", next={"kind": "collect", "target": "c", "reason": "r"}),
        gate_row("ABC-2"),
        make_row("ABC-3", next={"kind": "rest", "target": None, "reason": "r"}),
    ]
    state, commands = handle_key(State(), rows, "b")
    assert commands == []
    assert [row["key"] for row in tui.visible_rows(state, rows)] == ["ABC-1"]


def test_b_reseats_a_cursor_the_filter_left_behind():
    rows = [
        make_row("ABC-1", next={"kind": "rest", "target": None, "reason": "r"}),
        make_row("ABC-2", next={"kind": "rest", "target": None, "reason": "r"}),
        make_row("ABC-3"),
    ]
    state, _ = handle_key(State(cursor=2), rows, "b")
    assert state.cursor == 0
    assert tui.selected(state, rows)["key"] == "ABC-3"


def test_slash_narrows_the_visible_rows_as_it_is_typed():
    rows = [make_row("ABC-123"), make_row("XYZ-9", summary="rate limit")]
    state, commands = handle_key(State(), rows, "/")
    assert commands == []
    for char in "xyz":
        state, emitted = handle_key(state, rows, char)
        assert emitted == []
    assert [row["key"] for row in tui.visible_rows(state, rows)] == ["XYZ-9"]
    assert handle_key(state, rows, "\n")[0].mode == "list"


def test_escape_cancels_a_search_and_restores_the_list():
    rows = [make_row("ABC-123"), make_row("XYZ-9")]
    state = State(mode="search", search="xyz", cursor=0)
    state, _ = handle_key(state, rows, "\x1b")
    assert state.search == ""
    assert len(tui.visible_rows(state, rows)) == 2


def test_j_and_k_move_the_cursor_and_carry_the_viewport():
    rows = [make_row(f"ABC-{n:03d}") for n in range(40)]
    state = State(viewport=10)
    for _ in range(12):
        state, _ = handle_key(state, rows, "j")
    assert state.cursor == 12
    assert state.offset == 3
    state, _ = handle_key(state, rows, "k")
    assert (state.cursor, state.offset) == (11, 3)


def test_tab_hands_focus_to_the_log_and_takes_it_back():
    """Focus has to be reachable: `j` scrolling the log is dead code if no key sets it."""
    rows = [make_row("ABC-123")]
    state, _ = handle_key(State(log_lines=("a", "b")), rows, "\t")
    assert state.focus == "log"
    assert handle_key(state, rows, "\t")[0].focus == "list"


def test_tab_leaves_the_log_alone_when_there_is_nothing_to_scroll():
    rows = [make_row("ABC-123")]
    assert handle_key(State(), rows, "\t")[0].focus != "log"
    collapsed = State(log_lines=("a",), log_collapsed=True)
    assert handle_key(collapsed, rows, "\t")[0].focus != "log"


def test_j_scrolls_the_log_when_the_log_has_focus():
    state = State(focus="log", log_lines=("a", "b", "c"))
    assert handle_key(state, [make_row("ABC-1")], "j")[0].log_offset == 1


def test_toggles_emit_nothing():
    rows = [make_row("ABC-123")]
    for key, field in (("w", "keys_only"), ("L", "log_collapsed"), ("?", "help_open")):
        state, commands = handle_key(State(), rows, key)
        assert commands == []
        assert getattr(state, field) is True


def test_t_collects_a_key_and_enter_tracks_it():
    rows = [make_row("ABC-123")]
    state, _ = handle_key(State(), rows, "t")
    for char in "XYZ-9":
        state, commands = handle_key(state, rows, char)
        assert commands == []
    state, commands = handle_key(state, rows, "\n")
    assert commands == [["track", "XYZ-9"]]
    assert (state.mode, state.entry) == ("list", "")


def test_o_and_r_emit_the_verbs_they_are_named_after():
    row = make_row("ABC-123", pr="acme/api#115")
    assert handle_key(State(), [row], "o")[1] == [["open", "ABC-123"]]
    assert handle_key(State(), [row], "R")[1] == [["refresh"]]
    # Nothing to open before the first PR.
    assert handle_key(State(), [make_row("ABC-1")], "o")[1] == []


def test_q_asks_to_quit_rather_than_emitting_a_command():
    state, commands = handle_key(State(), [make_row("ABC-1")], "q")
    assert (state.quitting, commands) == (True, [])


def test_no_key_emits_anything_on_an_empty_queue():
    """`R` is the exception and is left out: refresh takes no key and refreshes every tracked row."""
    for key in "\n otpwTab/bLjk?":
        assert handle_key(State(), [], key)[1] == []


# --- guard ------------------------------------------------------------------


def test_tui_imports_nothing_that_writes():
    """#28's hard constraint, enforced rather than remembered.

    The TUI is a frontend: every mutation is a subprocess call the adapter makes, so nothing in the pure layer may reach the store, the network or a process.
    """
    tree = ast.parse(TUI_SOURCE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "ticket." if node.level else ""
            imported.add(f"{prefix}{node.module}")
    assert not imported & DENIED, sorted(imported & DENIED)
