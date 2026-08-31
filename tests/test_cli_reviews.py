import json
import textwrap
from pathlib import Path

import pytest

from ticket.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "reviews"

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: draft-pr
        run: scripts/draft-pr.sh
      - id: describe
        prompt: prompts/describe.md
        needs: [draft-pr]
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
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG)
    prompts = tmp_path / "prompts" / "reviews"
    prompts.mkdir(parents=True)
    (tmp_path / "prompts" / "describe.md").write_text("Describe.\n")
    (prompts / "docs-tests.md").write_text("Check docs and tests.\n")
    (prompts / "architecture.md").write_text("Check architecture.\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "draft-pr.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'ticket-pr: acme/api#115'\n"
        f"echo 'ticket-worktree: {tmp_path}'\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


def started(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["run", "draft-pr", "ABC-123"])
    # `describe` needs draft-pr and is declared before the reviews, so decision
    # #20 (a runnable step outranks dispatching a new review) makes it run
    # here too — otherwise `ticket review`/`ticket next` would hit `describe`
    # instead of a review, contradicting resolve.py's established order
    # (see test_resolve.py::test_a_runnable_step_outranks_dispatching_a_new_review).
    main(["run", "describe", "ABC-123"])


def test_review_dispatches_the_next_one(env, fake_bin, capsys):
    started(fake_bin)
    assert main(["review", "ABC-123"]) == 0
    comment = [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]][0]
    assert "/review docs-tests" in " ".join(comment)


def test_review_accepts_an_explicit_id(env, fake_bin):
    started(fake_bin)
    main(["review", "ABC-123", "architecture"])
    assert fake_bin.calls_to("claude")


def test_next_dispatches_a_review_once_a_pr_exists(env, fake_bin):
    started(fake_bin)
    main(["next", "ABC-123"])
    comment = [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]][0]
    assert "/review docs-tests" in " ".join(comment)


def test_collect_ingests_and_next_moves_to_fix(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps([{
            "id": "PRR_1", "user": {"login": "claude"},
            "body": (FIXTURES / "example-review.md").read_text(), "submitted_at": "t",
        }]),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    assert main(["collect", "ABC-123"]) == 0
    capsys.readouterr()
    main(["--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["next"]["kind"] == "fix"
    assert row["open_findings"] == 3


def test_findings_lists_by_status(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps([{
            "id": "PRR_1", "user": {"login": "claude"},
            "body": (FIXTURES / "example-review.md").read_text(), "submitted_at": "t",
        }]),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    main(["collect", "ABC-123"])
    capsys.readouterr()
    main(["findings", "ABC-123"])
    out = capsys.readouterr().out
    assert "f01" in out and "open" in out


def test_reviews_lists_ours_and_theirs(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    capsys.readouterr()
    main(["reviews", "ABC-123"])
    out = capsys.readouterr().out
    assert "docs-tests" in out
    assert "architecture" in out


def test_decide_closes_a_finding(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps([{
            "id": "PRR_1", "user": {"login": "claude"},
            "body": (FIXTURES / "example-review.md").read_text(), "submitted_at": "t",
        }]),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    main(["collect", "ABC-123"])
    assert main(["decide", "ABC-123", "f01", "covered by ABC-140"]) == 0
    capsys.readouterr()
    main(["findings", "ABC-123"])
    assert "wontfix" in capsys.readouterr().out


def test_effort_overrides(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps([{
            "id": "PRR_1", "user": {"login": "claude"},
            "body": (FIXTURES / "example-review.md").read_text(), "submitted_at": "t",
        }]),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    main(["collect", "ABC-123"])
    assert main(["effort", "ABC-123", "f02", "easy"]) == 0
    capsys.readouterr()
    main(["findings", "ABC-123", "--json"])
    findings = json.loads(capsys.readouterr().out)
    assert [f["effort"] for f in findings if f["id"] == "f02"] == ["easy"]


def test_reset_clears_the_step_and_everything_below(env, fake_bin, capsys):
    started(fake_bin)
    main(["run", "describe", "ABC-123"])
    assert main(["reset", "ABC-123", "draft-pr", "--force"]) == 0
    capsys.readouterr()
    main(["ABC-123"])
    out = capsys.readouterr().out
    assert "next: step draft-pr" in out
    assert "describe" in out


def test_reset_gives_back_the_pr_the_step_registered(env, fake_bin, capsys):
    """Otherwise the resolver keeps looping reviews on a PR the reset discarded."""
    started(fake_bin)
    main(["reset", "ABC-123", "draft-pr", "--force"])
    capsys.readouterr()
    main(["--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["pr"] is None
    assert row["next"]["kind"] == "step"


def test_a_second_run_on_a_locked_ticket_is_refused(env, fake_bin, capsys):
    """The advisory lock exists; this is the test that it is actually taken."""
    from ticket.config import load_config
    from ticket.store import Store

    started(fake_bin)
    store = Store(load_config().store)
    with store.lock("ABC-123"):
        assert main(["run", "describe", "ABC-123"]) == 1
    assert "locked" in capsys.readouterr().err


def test_no_sync_skips_the_fetch(env, fake_bin):
    started(fake_bin)
    fake_bin.log.unlink()
    main(["--no-sync", "collect", "ABC-123"])
    assert not any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_reset_without_force_asks_first(env, fake_bin, capsys, monkeypatch):
    started(fake_bin)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["reset", "ABC-123", "draft-pr"]) == 0
    capsys.readouterr()
    main(["ABC-123"])
    assert "done" in capsys.readouterr().out


def test_refresh_updates_the_head(env, fake_bin, capsys):
    started(fake_bin)
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    assert main(["refresh", "ABC-123"]) == 0
    capsys.readouterr()
    main(["reviews", "ABC-123", "--json"])
    assert "deadbee" in capsys.readouterr().out


def _add_fake_jira(fake_bin, summary: str) -> None:
    """`fake_bin` only wires up gh/git/claude; refresh also shells out to
    `jira` if present, so tests that care about it add a copy by hand."""
    import stat
    import sys

    body = (Path(__file__).parent / "fakes" / "fake_tool.py").read_text().split("\n", 1)[1]
    target = fake_bin.directory / "jira"
    target.write_text(f"#!{sys.executable}\n{body}")
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    fake_bin.respond("jira issue view", stdout=summary)


def test_refresh_dry_run_makes_no_write_and_no_jira_call(env, fake_bin, capsys, store):
    started(fake_bin)
    _add_fake_jira(fake_bin, "ABC-123: fresh summary\n")
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    capsys.readouterr()
    assert main(["refresh", "ABC-123", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert fake_bin.calls_to("jira") == []
    assert store.read_pr("acme/api#115") is None
    ticket = store.read_ticket("ABC-123")
    assert ticket.get("summary", "") == ""


def test_refresh_without_dry_run_writes_pr_and_calls_jira(env, fake_bin, capsys, store):
    started(fake_bin)
    _add_fake_jira(fake_bin, "ABC-123: fresh summary\n")
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    assert main(["refresh", "ABC-123"]) == 0
    assert fake_bin.calls_to("jira")
    pr = store.read_pr("acme/api#115")
    assert pr is not None
    assert pr["head"] == "deadbee"
    ticket = store.read_ticket("ABC-123")
    assert ticket["summary"] == "ABC-123: fresh summary"


def test_open_uses_gh_web(env, fake_bin):
    started(fake_bin)
    main(["open", "ABC-123"])
    assert any("--web" in c for c in fake_bin.calls_to("gh"))


def test_pr_flag_picks_the_pr(env, fake_bin, capsys):
    started(fake_bin)
    assert main(["findings", "ABC-123", "--pr", "115"]) == 0


def test_dry_run_on_review_posts_nothing(env, fake_bin):
    started(fake_bin)
    main(["review", "ABC-123", "--dry-run"])
    assert [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]] == []
