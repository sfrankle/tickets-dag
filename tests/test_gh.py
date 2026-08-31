import json

import pytest

from ticket import gh
from ticket.errors import GhError


def test_run_returns_stdout(fake_bin):
    fake_bin.respond("gh api", stdout="hello\n")
    assert gh.run(["gh", "api", "x"]) == "hello\n"


def test_run_retries_three_times_then_raises(fake_bin):
    fake_bin.respond("gh api", exit_code=1, stderr="boom")
    slept = []
    with pytest.raises(GhError, match="boom"):
        gh.run(["gh", "api", "x"], sleep=slept.append)
    assert len(fake_bin.calls_to("gh")) == 3
    assert slept == [1, 2]


def test_run_stops_retrying_once_it_succeeds(fake_bin):
    fake_bin.respond("gh api", stdout="ok")
    gh.run(["gh", "api", "x"], sleep=lambda _s: None)
    assert len(fake_bin.calls_to("gh")) == 1


def test_gh_json_parses(fake_bin):
    fake_bin.respond("gh api", stdout=json.dumps({"a": 1}))
    assert gh.gh_json(["gh", "api", "x"]) == {"a": 1}


def test_pr_head(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    assert gh.pr_head("acme/api#115") == "9c1f0ab"
    call = fake_bin.calls_to("gh")[0]
    assert "115" in call and "acme/api" in call


def test_pr_head_reports_an_unexpected_shape_as_a_gh_error(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"number": 115}))
    with pytest.raises(GhError, match="headRefOid"):
        gh.pr_head("acme/api#115")


def test_pr_comment_posts_the_body(fake_bin):
    gh.pr_comment("acme/api#115", "hello world", dry_run=False)
    call = fake_bin.calls_to("gh")[0]
    assert call[1:4] == ["pr", "comment", "115"]
    assert "hello world" in call


def test_pr_comment_dry_run_posts_nothing(fake_bin):
    gh.pr_comment("acme/api#115", "hello world", dry_run=True)
    assert fake_bin.calls_to("gh") == []


def test_pr_comment_is_not_retried(fake_bin):
    """Posting a comment is not idempotent: a retry would post it three times."""
    fake_bin.respond("gh pr comment", exit_code=1, stderr="boom")
    with pytest.raises(GhError):
        gh.pr_comment("acme/api#115", "hello", dry_run=False)
    assert len([c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]]) == 1


def test_sync_fetches_and_fast_forwards(tmp_path, fake_bin):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    assert gh.sync(checkout) is None
    argvs = [" ".join(c) for c in fake_bin.calls_to("git")]
    assert any(a.startswith("git fetch --prune") for a in argvs)
    assert any("merge --ff-only" in a for a in argvs)


def test_sync_leaves_a_dirty_tree_alone(tmp_path, fake_bin):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    fake_bin.respond("git status --porcelain", stdout=" M README.md\n")
    reason = gh.sync(checkout)
    assert "uncommitted changes" in reason
    assert not any("merge" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_sync_reports_rather_than_raises_when_fetch_fails(tmp_path, fake_bin):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    fake_bin.respond("git fetch", exit_code=1, stderr="no network")
    assert "fetch failed" in gh.sync(checkout)


def test_sync_on_nothing_is_a_no_op(fake_bin):
    assert gh.sync(None) is None
    assert fake_bin.calls == []


def test_sync_on_a_directory_with_no_git_is_reported_not_fetched(tmp_path, fake_bin):
    """A directory that exists but was never a git checkout must not fall
    through to a real `git fetch`, which would just fail."""
    not_a_checkout = tmp_path / "plain-dir"
    not_a_checkout.mkdir()
    reason = gh.sync(not_a_checkout)
    assert "not a checkout" in reason
    assert fake_bin.calls == []


def test_pr_reviews_normalises_shape(fake_bin):
    payload = [
        {
            "id": "PRR_1",
            "user": {"login": "review-bot"},
            "body": "b",
            "submitted_at": "t",
        },
    ]
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=json.dumps(payload)
    )
    assert gh.pr_reviews("acme/api#115") == [
        {"id": "PRR_1", "author": "review-bot", "body": "b", "submitted_at": "t"}
    ]


def test_pr_comments_normalises_shape(fake_bin):
    payload = [
        {"id": 42, "user": {"login": "sfrankle"}, "body": "b", "created_at": "t"}
    ]
    fake_bin.respond(
        "gh api repos/acme/api/issues/115/comments", stdout=json.dumps(payload)
    )
    assert gh.pr_comments("acme/api#115") == [
        {"id": "42", "author": "sfrankle", "body": "b", "submitted_at": "t"}
    ]
