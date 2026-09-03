"""`view.row` is the one query layer: `show`, `show --json` and the TUI all read it.

Issue #28. The dict is the contract, so these tests are about its shape and
about the two derived lists — `steps` from the config walked against the
ticket's records, `reviews` from the PR document.
"""

import textwrap

import pytest

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
    ticket = store.read_ticket(key)
    ticket["prs"] = ["acme/api#115"]
    store.write_ticket(ticket)
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
    """Skipping outranks the count, the same way `_uncollected` honours it:
    otherwise the view would say `dispatched` while `next` had moved on."""
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
