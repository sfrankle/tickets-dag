"""`view.row` is the one query layer: `show`, `show --json` and the TUI all read it.

Issue #28. The dict is the contract, so these tests are about its shape and
about the two derived lists — `steps` from the config walked against the
ticket's records, `reviews` from the PR document.
"""

import json
import os
import textwrap

import pytest

from tests.conftest import dead_pid, write_lock
from ticket import view
from ticket.cli import main
from ticket.store import Store

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5}
    defaults: {model: opus}
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
      - id: review-spec
        gate: true
        needs: [evaluate]
    reviews:
      - id: correctness
        order: 1
        dispatch: bot
        prompt: prompts/reviews/correctness.md
      - id: security
        order: 2
        dispatch: bot
        prompt: prompts/reviews/security.md
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG)
    (tmp_path / "prompts" / "reviews").mkdir(parents=True)
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate.\n")
    for review in ("correctness", "security"):
        (tmp_path / "prompts" / "reviews" / f"{review}.md").write_text("Review.\n")
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


@pytest.fixture
def store(env):
    return Store(env / "store")


@pytest.fixture
def tracked(env):
    main(["track", "ABC-123", "--repo", "acme/api"])
    return env


def row_for(key: str = "ABC-123") -> dict:
    ctx = view.Context.load()
    return view.row(ctx, view.load_ticket(ctx, key))


def with_pr(store: Store, pr: dict | None = None, key: str = "ABC-123") -> None:
    """Register a PR on the ticket, and optionally write its document."""
    with_prs(store, ["acme/api#115"], key=key)
    if pr is not None:
        store.write_pr({"pr": "acme/api#115", "key": key, **pr})


# --- steps ----------------------------------------------------------------


def test_steps_mirror_the_config_in_order(tracked):
    assert [s["id"] for s in row_for()["steps"]] == ["evaluate", "review-spec"]


def test_a_step_that_has_not_run_has_no_status(tracked):
    assert row_for()["steps"][0]["status"] is None


def test_a_step_carries_the_word_the_store_holds(tracked):
    main(["skip", "ABC-123", "evaluate"])
    assert row_for()["steps"][0]["status"] == "skipped"


def test_a_step_carries_its_kind(tracked):
    kinds = {s["id"]: s["kind"] for s in row_for()["steps"]}
    assert kinds["review-spec"] == "gate"


def test_a_recorded_log_that_is_gone_is_flagged(tracked, store):
    ticket = store.read_ticket("ABC-123")
    ticket["steps"] = {
        "evaluate": {"status": "done", "log": "tickets/ABC-123/logs/gone.log"}
    }
    store.write_ticket(ticket)
    step = row_for()["steps"][0]
    assert step["log"] == "tickets/ABC-123/logs/gone.log"
    assert step["log_missing"] is True


# --- reviews --------------------------------------------------------------


def test_reviews_mirror_the_config_in_order(tracked, store):
    with_pr(store)
    assert [r["id"] for r in row_for()["reviews"]] == ["correctness", "security"]


def test_a_ticket_with_no_pr_has_no_reviews(tracked):
    assert row_for()["reviews"] == []


def test_an_undispatched_review_is_pending(tracked, store):
    with_pr(store, {"dispatched": [], "collected": [], "skipped": []})
    assert row_for()["reviews"][0]["status"] == "pending"


def test_a_dispatched_review_is_dispatched(tracked, store):
    with_pr(store, {"dispatched": [{"review": "correctness"}], "collected": []})
    assert row_for()["reviews"][0]["status"] == "dispatched"


def test_a_collected_review_is_collected(tracked, store):
    with_pr(
        store,
        {
            "dispatched": [{"review": "correctness"}],
            "collected": [{"review": "correctness"}],
        },
    )
    assert row_for()["reviews"][0]["status"] == "collected"


def test_a_review_dispatched_again_is_dispatched_not_collected(tracked, store):
    """#28's re-dispatch rule: counts, not "has it ever been collected"."""
    with_pr(
        store,
        {
            "dispatched": [{"review": "correctness"}, {"review": "correctness"}],
            "collected": [{"review": "correctness"}],
        },
    )
    assert row_for()["reviews"][0]["status"] == "dispatched"


def test_a_skipped_review_is_skipped_even_after_dispatch(tracked, store):
    """Skipping outranks the count, the same way `_uncollected` honours it: otherwise the view would say `dispatched` while `next` had moved on."""
    with_pr(
        store,
        {
            "dispatched": [{"review": "correctness"}],
            "collected": [],
            "skipped": ["correctness"],
        },
    )
    assert row_for()["reviews"][0]["status"] == "skipped"


# --- rows -----------------------------------------------------------------


def test_rows_covers_tracked_tickets_only(tracked, store):
    store.write_ticket({"key": "ZZZ-999", "tracked": False})
    assert [r["key"] for r in view.rows(view.Context.load())] == ["ABC-123"]


def test_the_existing_keys_are_unchanged(tracked):
    row = row_for()
    assert row["key"] == "ABC-123"
    assert row["repo"] == "acme/api"
    assert row["pr"] is None
    assert row["open_findings"] == 0
    assert row["next"]["kind"] == "step"


# --- prs ------------------------------------------------------------------


def with_prs(
    store: Store,
    refs: list[str],
    active: str | None = None,
    key: str = "ABC-123",
) -> None:
    """Register several PRs, and optionally point `active` at one of them."""
    ticket = store.read_ticket(key)
    ticket["prs"] = refs
    if active:
        ticket["active"] = active
    store.write_ticket(ticket)


def test_a_ticket_with_no_pr_has_no_prs(tracked):
    assert row_for()["prs"] == []


def test_prs_lists_every_registered_pr_in_order(tracked, store):
    with_prs(store, ["acme/api#112", "acme/api#115"])
    assert [p["ref"] for p in row_for()["prs"]] == ["acme/api#112", "acme/api#115"]


def test_each_pr_counts_its_own_open_findings(tracked, store):
    with_prs(store, ["acme/api#112", "acme/api#115"])
    store.add_findings(
        "acme/api#112",
        [{"body": "one"}, {"body": "two"}, {"body": "three"}],
        key="ABC-123",
    )
    store.add_findings("acme/api#115", [{"body": "four"}], key="ABC-123")
    counts = {p["ref"]: p["open_findings"] for p in row_for()["prs"]}
    assert counts == {"acme/api#112": 3, "acme/api#115": 1}


def test_a_resolved_finding_is_not_open(tracked, store):
    with_prs(store, ["acme/api#115"])
    store.add_findings("acme/api#115", [{"body": "one"}, {"body": "two"}], "ABC-123")
    doc = store.read_findings("acme/api#115", "ABC-123")
    doc["findings"][0]["status"] = "fixed"
    store.write_findings(doc)
    assert row_for()["prs"][0]["open_findings"] == 1


def test_the_newest_pr_is_active_by_default(tracked, store):
    with_prs(store, ["acme/api#112", "acme/api#115"])
    assert [p["active"] for p in row_for()["prs"]] == [False, True]


def test_the_pointer_decides_which_pr_is_active_not_the_newest(tracked, store):
    """#33 made `--pr` a pointer that sticks, so `prs[-1]` is only the fallback."""
    with_prs(store, ["acme/api#112", "acme/api#115"], active="acme/api#112")
    row = row_for()
    assert [p["active"] for p in row["prs"]] == [True, False]
    assert row["pr"] == "acme/api#112"


def test_open_findings_still_counts_the_active_pr_only(tracked, store):
    """The top-level count keeps its meaning: nothing reading `--json` breaks."""
    with_prs(store, ["acme/api#112", "acme/api#115"], active="acme/api#115")
    store.add_findings("acme/api#112", [{"body": "one"}, {"body": "two"}], "ABC-123")
    store.add_findings("acme/api#115", [{"body": "three"}], "ABC-123")
    assert row_for()["open_findings"] == 1


# --- running and lock -----------------------------------------------------


def test_an_unlocked_ticket_is_not_running(tracked):
    row = row_for()
    assert row["lock"] is None
    assert row["lock_path"] is None
    assert row["running"] is None


def test_a_live_pid_holds_the_lock(tracked, store):
    write_lock(store, f"{os.getpid()}\n")
    row = row_for()
    assert row["lock"] == "held"
    assert row["running"]["pid"] == os.getpid()
    assert row["running"]["since"]


def test_running_names_the_log_of_the_step_next_would_run(tracked, store):
    ticket = store.read_ticket("ABC-123")
    ticket["steps"] = {"evaluate": {"status": "running", "log": "tickets/e.log"}}
    store.write_ticket(ticket)
    write_lock(store, f"{os.getpid()}\n")
    row = row_for()
    assert row["next"]["target"] == "evaluate"
    assert row["running"]["log"] == "tickets/e.log"


def test_a_dead_pid_is_a_stale_lock_and_nothing_is_running(tracked, store):
    path = write_lock(store, f"{dead_pid()}\n")
    row = row_for()
    assert row["lock"] == "stale"
    assert row["lock_path"] == str(path)
    assert row["running"] is None


def test_a_lock_that_recorded_no_pid_is_stale(tracked, store):
    """A run killed between creating the file and writing its pid: unknown is not alive, the same reading `unlock` takes."""
    write_lock(store, "")
    assert row_for()["lock"] == "stale"


# --- show -----------------------------------------------------------------


def test_show_reports_a_stale_lock(tracked, store, capsys):
    write_lock(store, f"{dead_pid()}\n")
    main(["show", "ABC-123"])
    out = capsys.readouterr().out
    assert "stale lock" in out
    assert "ticket unlock ABC-123" in out


def test_show_reports_a_running_step(tracked, store, capsys):
    write_lock(store, f"{os.getpid()}\n")
    main(["show", "ABC-123"])
    assert f"running: pid {os.getpid()}" in capsys.readouterr().out


def test_show_lists_the_prs_when_there_is_more_than_one(tracked, store, capsys):
    with_prs(store, ["acme/api#112", "acme/api#115"], active="acme/api#112")
    store.add_findings("acme/api#115", [{"body": "one"}], "ABC-123")
    main(["show", "ABC-123"])
    out = capsys.readouterr().out
    assert "* acme/api#112" in out
    assert "acme/api#115  1 open" in out


def test_show_does_not_list_a_single_pr_twice(tracked, store, capsys):
    with_prs(store, ["acme/api#115"])
    main(["show", "ABC-123"])
    assert capsys.readouterr().out.count("acme/api#115") == 1


def test_json_carries_the_new_fields(tracked, store, capsys):
    write_lock(store, f"{dead_pid()}\n")
    with_prs(store, ["acme/api#115"])
    main(["show", "ABC-123", "--json"])
    row = json.loads(capsys.readouterr().out)
    assert row["prs"] == [{"ref": "acme/api#115", "open_findings": 0, "active": True}]
    assert row["lock"] == "stale"
    assert row["running"] is None
