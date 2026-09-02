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
    tracker:
      summary: [faketracker, issue, view, "{key}", --plain]
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
    fix:
      easy:
        run: scripts/fix-easy.sh
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
    fixer = scripts / "fix-easy.sh"
    fixer.write_text('#!/bin/sh\necho "fixer took $TICKET_FINDING_ID"\n')
    fixer.chmod(0o755)
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


def started(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "9c1f0ab"}))
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["run", "ABC-123", "draft-pr"])
    # `describe` needs draft-pr and is declared before the reviews, so decision
    # #20 (a runnable step outranks dispatching a new review) makes it run
    # here too — otherwise `ticket review`/`ticket next` would hit `describe`
    # instead of a review, contradicting resolve.py's established order
    # (see test_resolve.py::test_a_runnable_step_outranks_dispatching_a_new_review).
    main(["run", "ABC-123", "describe"])


def test_review_dispatches_the_next_one(env, fake_bin, capsys):
    started(fake_bin)
    assert main(["review", "ABC-123"]) == 0
    comment = next(c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"])
    assert "/review docs-tests" in " ".join(comment)


def test_review_accepts_an_explicit_id(env, fake_bin):
    started(fake_bin)
    main(["review", "ABC-123", "architecture"])
    assert fake_bin.calls_to("claude")


def test_next_dispatches_a_review_once_a_pr_exists(env, fake_bin):
    started(fake_bin)
    main(["next", "ABC-123"])
    comment = next(c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"])
    assert "/review docs-tests" in " ".join(comment)


def test_collect_ingests_and_next_moves_to_fix(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "claude"},
                    "body": (FIXTURES / "example-review.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
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
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "claude"},
                    "body": (FIXTURES / "example-review.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
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
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "claude"},
                    "body": (FIXTURES / "example-review.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
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
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "claude"},
                    "body": (FIXTURES / "example-review.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    main(["collect", "ABC-123"])
    assert main(["effort", "ABC-123", "f02", "easy"]) == 0
    capsys.readouterr()
    main(["findings", "ABC-123", "--json"])
    findings = json.loads(capsys.readouterr().out)
    assert [f["effort"] for f in findings if f["id"] == "f02"] == ["easy"]


def test_fix_without_waiting_survives_a_missing_pr_document(env, fake_bin, capsys):
    """cmd_fix must not crash reading `.get("head")` off a PR doc that has not been written yet (no dispatch/collection has run), when the wait is not wanted — the head is only needed on the waiting path."""
    from ticket.store import Store

    started(fake_bin)
    store = Store(env / "store")
    ids = store.add_findings(
        "acme/api#115",
        [{"file": "a.py", "summary": "s", "severity": "maintenance", "effort": "easy"}],
    )
    finding_id = ids[0]
    assert store.read_pr("acme/api#115") is None
    assert main(["fix", "ABC-123", finding_id, "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert f"{finding_id}:" in out


def test_the_row_shows_open_finding_count(env, fake_bin, capsys):
    from ticket.store import Store

    started(fake_bin)
    Store(env / "store").add_findings(
        "acme/api#115",
        [{"file": "a.py", "summary": "s", "severity": "blocking", "effort": "easy"}],
    )
    capsys.readouterr()
    main(["ABC-123"])
    assert "1 open" in capsys.readouterr().out


def _seed_findings(env, *findings):
    from ticket.store import Store

    store = Store(env / "store")
    store.add_findings("acme/api#115", list(findings))
    return store


def _easy(summary="s"):
    return {
        "file": "a.py",
        "summary": summary,
        "severity": "maintenance",
        "effort": "easy",
    }


def test_fix_works_one_finding_per_run(env, fake_bin, monkeypatch, capsys):
    """Issue #13: a run that handed out a second finding while the first was still with the fixer got both dropped — the fixer runs one action per PR.
    One run, one finding, and it waits for the commit before returning."""
    from ticket import cli

    started(fake_bin)
    store = _seed_findings(env, _easy("first"), _easy("second"))
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "aaa"}))

    order = []
    monkeypatch.setattr(
        cli.fix_module,
        "wait_for_head",
        lambda pr_ref, before, **kw: (order.append("wait"), "bbb")[1],
    )
    real_run = cli.steps_module.tee

    def spy_tee(argv, **kwargs):
        order.append("handoff")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.steps_module, "tee", spy_tee)

    assert main(["fix", "ABC-123"]) == 0

    assert order == ["handoff", "wait"]
    assert store.read_pr("acme/api#115")["head"] == "bbb"
    out = capsys.readouterr().out
    assert "fixer took f01" in out
    assert "f02" not in out.replace("still open", "")


def test_fix_refuses_a_second_handoff_while_the_first_is_in_flight(
    env, fake_bin, monkeypatch, capsys
):
    from ticket import cli

    started(fake_bin)
    _seed_findings(env, _easy("first"))
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "aaa"}))
    monkeypatch.setattr(cli.fix_module, "wait_for_head", lambda *a, **kw: "bbb")

    assert main(["fix", "ABC-123", "--no-wait"]) == 0
    capsys.readouterr()
    assert main(["fix", "ABC-123", "f01"]) == 1
    assert "was handed to the easy fixer" in capsys.readouterr().err


def test_fix_says_what_it_is_waiting_for(env, fake_bin, monkeypatch, capsys):
    """The owner saw a long silence and could not tell a wait from a hang."""
    import functools

    from ticket import cli

    started(fake_bin)
    _seed_findings(env, _easy("first"))
    heads = iter(["aaa", "aaa", "bbb"])
    monkeypatch.setattr(cli.gh, "pr_head", lambda pr_ref: next(heads))
    monkeypatch.setattr(
        cli.fix_module,
        "wait_for_head",
        functools.partial(cli.fix_module.wait_for_head, poll=lambda _s: None),
    )

    assert main(["fix", "ABC-123"]) == 0
    out = capsys.readouterr().out
    assert "waiting for a new commit on acme/api#115" in out
    assert "still aaa (1/30" in out


def test_fix_does_not_wait_on_a_hard_finding(env, fake_bin, monkeypatch, capsys):
    """A hard fix commits locally; the remote head never moves, so a wait would
    poll until it gave up."""
    from ticket import cli

    started(fake_bin)
    _seed_findings(env, {"summary": "retry loop", "effort": "hard"})

    def fail_wait(*a, **kw):
        raise AssertionError("waited on a hard fix")

    monkeypatch.setattr(cli.fix_module, "wait_for_head", fail_wait)
    assert main(["fix", "ABC-123"]) == 0


def test_fix_no_wait_says_the_finding_is_still_with_the_fixer(
    env, fake_bin, monkeypatch, capsys
):
    from ticket import cli

    started(fake_bin)
    _seed_findings(env, _easy("first"))

    def fail_wait(*a, **kw):
        raise AssertionError("waited despite --no-wait")

    monkeypatch.setattr(cli.fix_module, "wait_for_head", fail_wait)
    capsys.readouterr()
    assert main(["fix", "ABC-123", "--no-wait"]) == 0
    assert "not waiting (--no-wait)" in capsys.readouterr().out


def test_fix_skips_the_wait_baseline_on_a_dry_run(env, fake_bin, capsys):
    started(fake_bin)
    _seed_findings(env, _easy("first"))
    capsys.readouterr()
    assert main(["fix", "ABC-123", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would run" in out
    assert "fixer took" not in out


def test_fix_wait_baselines_on_the_live_head(env, fake_bin, monkeypatch):
    """The stored head is None for a PR we only ever collected from, and stale
    once an earlier fix moved it. Either would make the wait return at once."""
    from ticket import cli
    from ticket.store import Store

    started(fake_bin)
    store = Store(env / "store")
    finding_id = store.add_findings(
        "acme/api#115",
        [{"file": "a.py", "summary": "s", "severity": "maintenance", "effort": "easy"}],
    )[0]
    assert store.read_pr("acme/api#115") is None
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "aaa"}))

    seen = {}

    def fake_wait(pr_ref, before, **kwargs):
        seen["before"] = before
        return "bbb"

    monkeypatch.setattr(cli.fix_module, "wait_for_head", fake_wait)
    assert main(["fix", "ABC-123", finding_id]) == 0

    assert seen["before"] == "aaa"
    assert store.read_pr("acme/api#115")["head"] == "bbb"


def test_reset_clears_the_step_and_everything_below(env, fake_bin, capsys):
    started(fake_bin)
    main(["run", "ABC-123", "describe"])
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
        assert main(["run", "ABC-123", "describe"]) == 1
    assert "locked" in capsys.readouterr().err


def test_no_sync_skips_the_fetch(env, fake_bin):
    started(fake_bin)
    fake_bin.log.unlink()
    main(["--no-sync", "collect", "ABC-123"])
    assert not any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_top_level_json_flag_before_the_verb_is_not_clobbered(env, fake_bin, capsys):
    """`--json` parsed before the verb must survive: a subparser's own
    `json=False` default must not overwrite it in the shared namespace."""
    started(fake_bin)
    capsys.readouterr()
    assert main(["--json", "show", "ABC-123"]) == 0
    out = capsys.readouterr().out
    json.loads(out)  # would raise if it fell back to the plain-text renderer


def test_no_sync_after_the_verb_also_skips_the_fetch(env, fake_bin):
    started(fake_bin)
    fake_bin.log.unlink()
    main(["collect", "ABC-123", "--no-sync"])
    assert not any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_no_sync_does_not_leak_into_os_environ(env, fake_bin):
    started(fake_bin)
    import os

    assert "TICKET_NO_SYNC" not in os.environ
    main(["--no-sync", "collect", "ABC-123"])
    assert "TICKET_NO_SYNC" not in os.environ


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


def test_refresh_dry_run_makes_no_write_and_no_tracker_call(
    env, fake_bin, fake_tracker, capsys, store
):
    started(fake_bin)
    fake_tracker("ABC-123: fresh summary\n")
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    capsys.readouterr()
    assert main(["refresh", "ABC-123", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert fake_bin.calls_to("faketracker") == []
    assert store.read_pr("acme/api#115") is None
    ticket = store.read_ticket("ABC-123")
    assert ticket.get("summary", "") == ""


def test_refresh_without_dry_run_writes_pr_and_calls_the_tracker(
    env, fake_bin, fake_tracker, capsys, store
):
    started(fake_bin)
    fake_tracker("ABC-123: fresh summary\n")
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    assert main(["refresh", "ABC-123"]) == 0
    assert fake_bin.calls_to("faketracker")
    assert "ABC-123" in " ".join(fake_bin.calls_to("faketracker")[0])
    pr = store.read_pr("acme/api#115")
    assert pr is not None
    assert pr["head"] == "deadbee"
    ticket = store.read_ticket("ABC-123")
    assert ticket["summary"] == "ABC-123: fresh summary"


def test_open_uses_gh_web(env, fake_bin):
    started(fake_bin)
    main(["open", "ABC-123"])
    assert any("--web" in c for c in fake_bin.calls_to("gh"))


def test_open_on_a_malformed_ref_is_a_clean_ticket_error(env, fake_bin, capsys):
    """cmd_open must use gh.split_ref (the sole module that knows how to
    split owner/repo#number) so a malformed ref raises GhError, not a bare
    ValueError from a manual `.split("#")`."""
    from ticket.store import Store

    started(fake_bin)
    store = Store(env / "store")
    ticket = store.read_ticket("ABC-123")
    ticket["prs"] = ["not-a-valid-ref"]
    store.write_ticket(ticket)
    assert main(["open", "ABC-123"]) == 1
    assert "not owner/repo#number" in capsys.readouterr().err


def test_pr_flag_picks_the_pr(env, fake_bin, capsys):
    started(fake_bin)
    assert main(["findings", "ABC-123", "--pr", "115"]) == 0


def test_dry_run_on_review_posts_nothing(env, fake_bin):
    started(fake_bin)
    main(["review", "ABC-123", "--dry-run"])
    assert [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]] == []


def test_refresh_skips_the_tracker_when_the_command_is_not_configured(
    env, fake_bin, fake_tracker, capsys, store, tmp_path
):
    """No `tracker:` block is the default, and it must not be an error."""
    started(fake_bin)
    fake_tracker("ABC-123: fresh summary\n")
    config = tmp_path / "config.yml"
    config.write_text(
        CONFIG.replace(
            'tracker:\n  summary: [faketracker, issue, view, "{key}", --plain]\n', ""
        )
    )
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    assert main(["refresh", "ABC-123"]) == 0
    assert fake_bin.calls_to("faketracker") == []
    assert store.read_ticket("ABC-123").get("summary", "") == ""


def test_refresh_survives_a_tracker_binary_that_is_not_installed(
    env, fake_bin, capsys, store
):
    """The command is configured but absent: skip it, keep refreshing gh."""
    started(fake_bin)
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "deadbee"}))
    assert main(["refresh", "ABC-123"]) == 0
    assert store.read_pr("acme/api#115")["head"] == "deadbee"


SEVERITY_CONFIG = CONFIG.replace(
    "tracker:",
    textwrap.dedent("""\
    severities:
      - id: must-fix
        marker: "[!]"
      - id: consider
        marker: "[?]"
        default: true
    tracker:"""),
)


@pytest.fixture
def severity_env(env, monkeypatch):
    (env / "config.yml").write_text(SEVERITY_CONFIG)
    return env


def test_findings_are_listed_in_configured_severity_order(
    severity_env, fake_bin, capsys
):
    """Config order is the order, so a custom vocabulary sorts by its own
    importance rather than falling to the end as unknown."""
    started(fake_bin)
    _seed_findings(
        severity_env,
        {"summary": "lower", "severity": "consider"},
        {"summary": "higher", "severity": "must-fix"},
    )
    capsys.readouterr()

    assert main(["findings", "ABC-123"]) == 0

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [ln.split()[2] for ln in lines] == ["must-fix", "consider"]


def one_review(fake_bin, body_file="example-review.md", author="claude"):
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": author},
                    "body": (FIXTURES / body_file).read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))


def test_collect_does_not_call_a_source_foreign_when_nothing_was_dispatched(
    env, fake_bin, capsys
):
    """`(not one of ours)` only means something once something of ours is out there.
    With an empty `dispatched` it was on every line and said nothing."""
    started(fake_bin)
    one_review(fake_bin, author="review-bot")
    capsys.readouterr()
    assert main(["collect", "ABC-123"]) == 0
    out = capsys.readouterr().out
    assert "not one of ours" not in out
    assert "review-bot" in out
    assert "3 findings" in out


def test_collect_still_marks_a_foreign_source_when_we_dispatched_something(
    env, fake_bin, capsys
):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "review-bot"},
                    "body": (FIXTURES / "human-comment.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
    )
    fake_bin.respond("claude", stdout=json.dumps([]))
    capsys.readouterr()
    assert main(["collect", "ABC-123"]) == 0
    assert "not one of ours" in capsys.readouterr().out


def test_attribute_unwedges_a_dispatch_recorded_as_none_of_ours(env, fake_bin, capsys):
    """The whole cycle: a body we could not read is collected as none of ours, `docs-tests` stays outstanding no matter how often collect runs, and `attribute` is what clears it."""
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=json.dumps(
            [
                {
                    "id": "PRR_1",
                    "user": {"login": "review-bot"},
                    "body": (FIXTURES / "human-comment.md").read_text(),
                    "submitted_at": "t",
                }
            ]
        ),
    )
    fake_bin.respond("claude", stdout=json.dumps([]))
    main(["collect", "ABC-123"])
    capsys.readouterr()

    # The source id `attribute` needs is on the line `reviews` prints for it.
    assert main(["reviews", "ABC-123"]) == 0
    assert "[PRR_1]" in capsys.readouterr().out

    assert main(["collect", "ABC-123"]) == 0
    assert "not collected yet" in capsys.readouterr().out

    assert main(["attribute", "ABC-123", "PRR_1", "docs-tests"]) == 0
    assert "PRR_1: docs-tests" in capsys.readouterr().out
    assert main(["collect", "ABC-123"]) == 0
    assert "not collected yet" not in capsys.readouterr().out


def test_attribute_rejects_a_review_that_is_not_in_config(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    assert main(["attribute", "ABC-123", "PRR_1", "not-a-review"]) != 0
    assert "unknown review" in capsys.readouterr().err


def test_collect_says_it_is_not_waiting_for_an_outstanding_review(
    env, fake_bin, capsys
):
    started(fake_bin)
    main(["review", "ABC-123"])
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    capsys.readouterr()
    assert main(["collect", "ABC-123"]) == 0
    out = capsys.readouterr().out
    assert "not waiting" in out
    assert "docs-tests" in out
    # `outstanding` counts dispatches against collection records carrying that
    # review id, so it means "not collected yet", which is true whether the
    # review has yet to post or posted and was recorded as none of ours.
    # "collect again once it posts" answers only the first, so the line has to
    # name the remedy for the second too — running collect again never clears it.
    assert "not collected yet" in out
    assert "once it posts" in out
    assert "ticket attribute ABC-123 <source-id> docs-tests" in out
    assert "ticket skip docs-tests" in out


def test_collect_recollect_re_reads_a_named_source(env, fake_bin, capsys):
    started(fake_bin)
    main(["review", "ABC-123"])
    one_review(fake_bin)
    main(["collect", "ABC-123"])
    capsys.readouterr()
    assert main(["collect", "ABC-123", "--recollect", "PRR_1"]) == 0
    out = capsys.readouterr().out
    assert "re-read" in out
    assert "0 new findings" in out


def test_collect_recollect_reports_the_findings_it_recovers(env, fake_bin, capsys):
    """A re-read that recovers nothing satisfies every other assertion here, so the recovering case needs its own."""
    started(fake_bin)
    main(["review", "ABC-123"])
    one_review(fake_bin)
    main(["collect", "ABC-123"])
    one_review(fake_bin, body_file="wide-review.md")
    capsys.readouterr()
    assert main(["collect", "ABC-123", "--recollect", "PRR_1"]) == 0
    assert "re-read docs-tests: 2 new findings" in capsys.readouterr().out


def test_collect_recollect_of_an_unknown_source_exits_non_zero(env, fake_bin, capsys):
    """A mistyped source id looks like a successful re-read to a script if it exits 0."""
    started(fake_bin)
    main(["review", "ABC-123"])
    one_review(fake_bin)
    capsys.readouterr()
    assert main(["collect", "ABC-123", "--recollect", "PRR_9"]) == 1
    assert "PRR_9" in capsys.readouterr().err


def test_next_does_not_count_a_re_read_as_a_new_source(env, fake_bin, capsys):
    """`ticket next` runs the same collect, and a re-read is an old source read again."""
    started(fake_bin)
    main(["review", "ABC-123"])
    one_review(fake_bin, body_file="human-comment.md")
    fake_bin.respond("claude", stdout=json.dumps([]))
    main(["collect", "ABC-123"])
    one_review(fake_bin)
    capsys.readouterr()
    assert main(["next", "ABC-123"]) == 0
    out = capsys.readouterr().out
    assert "collected 0 new sources" not in out
    assert "re-read 1" in out


def test_open_takes_a_pr_like_every_other_verb(env, fake_bin):
    """`open` was the one verb hardcoded to `prs[-1]` (issue #27)."""
    from ticket.store import Store

    started(fake_bin)
    store = Store(env / "store")
    ticket = store.read_ticket("ABC-123")
    ticket["prs"] = ["acme/api#115", "acme/api#200"]
    store.write_ticket(ticket)
    assert main(["open", "ABC-123", "--pr", "115"]) == 0
    viewed = next(c for c in fake_bin.calls_to("gh") if "--web" in c)
    assert "115" in viewed


def test_open_without_a_pr_flag_still_takes_the_newest(env, fake_bin):
    from ticket.store import Store

    started(fake_bin)
    store = Store(env / "store")
    ticket = store.read_ticket("ABC-123")
    ticket["prs"] = ["acme/api#115", "acme/api#200"]
    store.write_ticket(ticket)
    assert main(["open", "ABC-123"]) == 0
    viewed = next(c for c in fake_bin.calls_to("gh") if "--web" in c)
    assert "200" in viewed


def test_open_names_the_prs_it_knows_when_the_flag_misses(env, fake_bin, capsys):
    started(fake_bin)
    assert main(["open", "ABC-123", "--pr", "999"]) == 1
    assert "acme/api#115" in capsys.readouterr().err
