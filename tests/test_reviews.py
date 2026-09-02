import json
import textwrap

import pytest

from ticket.config import load_config
from ticket.errors import TicketError
from ticket.reviews import bot_body, dispatch

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: draft-pr
        run: scripts/draft-pr.sh
    reviews:
      - id: docs-tests
        order: 1
        dispatch: bot
        prompt: prompts/reviews/docs-tests.md
      - id: architecture
        order: 2
        dispatch: local
        model: opus
        prompt: prompts/reviews/architecture.md
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    prompts = tmp_path / "prompts" / "reviews"
    prompts.mkdir(parents=True)
    (prompts / "docs-tests.md").write_text("Check the docs and the tests.\n")
    (prompts / "architecture.md").write_text("Check the architecture.\n")
    return load_config(path)


def ticket_doc():
    return {"key": "ABC-123", "repo": "acme/api", "prs": ["acme/api#115"], "steps": {}}


def test_bot_body_shape(cfg):
    body = bot_body(cfg.review("docs-tests"), "Check the docs and the tests.\n")
    assert body.startswith("/review docs-tests\n")
    assert "<details>" in body and "</details>" in body
    assert "Check the docs and the tests." in body


def test_bot_dispatch_posts_a_comment(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    dispatch(cfg, store, ticket_doc(), "acme/api#115", cfg.review("docs-tests"))
    comment = next(c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"])
    assert "/review docs-tests" in " ".join(comment)


def test_bot_dispatch_records_the_head_sha_at_firing_time(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    dispatch(cfg, store, ticket_doc(), "acme/api#115", cfg.review("docs-tests"))
    pr = store.read_pr("acme/api#115")
    assert pr["dispatched"][0] == {
        "review": "docs-tests",
        "at": pr["dispatched"][0]["at"],
        "head": "9c1f0ab",
        "transport": "bot",
    }


def test_local_dispatch_runs_claude_with_the_prompt(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    dispatch(cfg, store, ticket_doc(), "acme/api#115", cfg.review("architecture"))
    assert "Check the architecture." in fake_bin.stdin_to("claude")[0]
    assert "claude-opus-5" in fake_bin.calls_to("claude")[0]
    assert store.read_pr("acme/api#115")["dispatched"][0]["transport"] == "local"


def test_local_dispatch_posts_its_output_as_a_pr_comment(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    fake_bin.respond("claude", stdout="the review body")
    dispatch(cfg, store, ticket_doc(), "acme/api#115", cfg.review("architecture"))
    comment = next(c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"])
    assert "the review body" in " ".join(comment)


def test_dispatch_is_not_repeated_against_the_same_head(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    ticket = ticket_doc()
    dispatch(cfg, store, ticket, "acme/api#115", cfg.review("docs-tests"))
    with pytest.raises(Exception, match="already dispatched"):
        dispatch(cfg, store, ticket, "acme/api#115", cfg.review("docs-tests"))


def test_a_review_can_be_redispatched_once_the_head_moves(cfg, store, fake_bin):
    """The review cycle loops; refusing every repeat would strand it after one
    round. Only a repeat against the same head is a mistake."""
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    ticket = ticket_doc()
    dispatch(cfg, store, ticket, "acme/api#115", cfg.review("docs-tests"))
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    dispatch(cfg, store, ticket, "acme/api#115", cfg.review("docs-tests"))
    heads = [d["head"] for d in store.read_pr("acme/api#115")["dispatched"]]
    assert heads == ["9c1f0ab", "deadbee"]


def test_a_local_review_runs_in_the_worktree(cfg, store, fake_bin, tmp_path):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    dispatch(cfg, store, ticket, "acme/api#115", cfg.review("architecture"))
    assert fake_bin.calls_to("claude")


def test_a_local_review_names_a_checkout_that_is_not_there(
    cfg, store, fake_bin, tmp_path
):
    """`config --validate` only warns about a `repos.<repo>.path` that is not
    cloned, so the checkout can be missing at dispatch time. That has to read
    as a TicketError naming the path, not a FileNotFoundError traceback out of
    subprocess."""
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    ticket = ticket_doc()
    ticket["worktree"] = str(tmp_path / "never-cloned")
    with pytest.raises(TicketError, match="never-cloned"):
        dispatch(cfg, store, ticket, "acme/api#115", cfg.review("architecture"))


def test_dry_run_posts_nothing_and_records_nothing(cfg, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    dispatch(
        cfg, store, ticket_doc(), "acme/api#115", cfg.review("docs-tests"), dry_run=True
    )
    assert [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]] == []
    assert store.read_pr("acme/api#115") is None


def test_dry_run_still_syncs_the_worktree(cfg, store, fake_bin, tmp_path):
    """Decision #22: --no-sync is sync's opt-out, deliberately separate from
    --dry-run. A dry run must not post/write, but it should still fetch to
    keep the checkout fresh -- this covers the local-transport dry-run path
    with a worktree present."""
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    dispatch(
        cfg, store, ticket, "acme/api#115", cfg.review("architecture"), dry_run=True
    )
    assert fake_bin.calls_to("claude") == []
    assert [c for c in fake_bin.calls_to("git") if c[1:2] == ["fetch"]] != []
