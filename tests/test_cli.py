import json
import textwrap

import pytest

from ticket.cli import main
from ticket.store import Store

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
      - id: review-spec
        gate: true
        needs: [evaluate]
      - id: draft-pr
        run: scripts/draft-pr.sh
        needs: [review-spec]
    reviews:
      - id: docs-tests
        order: 1
        dispatch: bot
        prompt: prompts/reviews/docs-tests.md
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG)
    (tmp_path / "prompts" / "reviews").mkdir(parents=True)
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate.\n")
    (tmp_path / "prompts" / "reviews" / "docs-tests.md").write_text(
        "Check docs and tests.\n"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "draft-pr.sh"
    script.write_text("#!/bin/sh\necho 'ticket-pr: acme/api#115'\n")
    script.chmod(0o755)
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


def test_track_creates_a_ticket(env, capsys):
    assert main(["track", "ABC-123", "--repo", "acme/api"]) == 0
    assert main(["ABC-123"]) == 0
    assert "ABC-123" in capsys.readouterr().out


def test_a_bare_key_shows_the_row(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["ABC-123"])
    out = capsys.readouterr().out
    assert "evaluate" in out


@pytest.mark.parametrize("key", ["..", "../etc", "a/b", "a\\b", " ", "a b"])
def test_track_rejects_a_key_that_is_not_a_safe_path_segment(
    env, capsys, tmp_path, key
):
    """`track` writes the key straight into a store path (`_ticket_file`)
    with no sanitisation; a path-shaped key must be refused before any store
    write rather than interpolated into a path."""
    assert main(["track", key, "--repo", "a/b"]) == 1
    assert "not a usable ticket key" in capsys.readouterr().err
    store = tmp_path / "store"
    assert not any(store.rglob("*.json"))


def test_track_rejects_a_dash_leading_key_before_it_reaches_the_store(
    env, capsys, tmp_path
):
    """argparse claims it as a flag, which is refusal enough — what matters is
    that nothing is written."""
    assert main(["track", "-x", "--repo", "a/b"]) != 0
    assert not any((tmp_path / "store").rglob("*.json"))


@pytest.mark.parametrize("key", ["ABC-123", "123", "eng_14", "add-tracker-block"])
def test_track_accepts_any_key_shape_the_tracker_uses(env, capsys, tmp_path, key):
    """The tool has no opinion on key shape; a shop that wants one sets
    `key_pattern:`."""
    assert main(["track", key, "--repo", "a/b"]) == 0


def test_track_enforces_key_pattern_when_the_config_sets_one(env, capsys, tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(config.read_text() + '\nkey_pattern: "^[A-Z]+-[0-9]+$"\n')
    assert main(["track", "eng_14", "--repo", "a/b"]) == 1
    err = capsys.readouterr().err
    assert "key_pattern" in err and "^[A-Z]+-[0-9]+$" in err
    assert main(["track", "ABC-123", "--repo", "a/b"]) == 0


def test_a_bare_key_still_shows_the_ticket_but_a_verb_never_does(env, capsys):
    """`main` rewrites a bare key to `show <key>`. With loose keys that rule
    must not swallow a verb: `refresh` is a verb, not a ticket named refresh."""
    main(["track", "refresh", "--repo", "a/b"])
    capsys.readouterr()
    assert main(["refresh"]) == 0
    assert "evaluate" not in capsys.readouterr().out


def test_an_unknown_key_is_an_error_not_a_traceback(env, capsys):
    assert main(["ABC-999"]) == 1
    assert "not tracked" in capsys.readouterr().err


def test_bare_ticket_prints_the_queue(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["track", "ABC-124", "--repo", "acme/api"])
    main([])
    out = capsys.readouterr().out
    assert "ABC-123" in out and "ABC-124" in out


def test_queue_json_is_machine_readable(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    capsys.readouterr()
    main(["--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["key"] == "ABC-123"
    assert rows[0]["next"]["kind"] == "step"


def test_next_runs_the_resolved_step(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    assert main(["next", "ABC-123"]) == 0
    assert fake_bin.calls_to("claude")


def test_next_stops_at_a_gate(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["next", "ABC-123"])
    fake_bin.log.unlink()
    assert main(["next", "ABC-123"]) == 0
    out = capsys.readouterr().out
    assert "parked" in out
    assert fake_bin.calls == []


def test_release_advances_past_the_gate(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["next", "ABC-123"])
    main(["release", "ABC-123", "review-spec"])
    main(["next", "ABC-123"])
    assert "acme/api#115" in capsys.readouterr().out


def test_run_reruns_a_named_step(env, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["run", "ABC-123", "evaluate"])
    main(["run", "ABC-123", "evaluate"])
    assert len(fake_bin.calls_to("claude")) == 2


def test_run_rejects_an_unknown_step(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    assert main(["run", "ABC-123", "nonesuch"]) == 1
    assert "unknown step" in capsys.readouterr().err


def test_skip_marks_a_step_and_the_resolver_walks_past(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["skip", "ABC-123", "evaluate", "--reason", "trivial"])
    main(["next", "ABC-123"])
    assert "parked" in capsys.readouterr().out


def test_skip_resolves_a_review_id_when_it_is_not_a_step(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["skip", "ABC-123", "docs-tests"])
    assert main(["ABC-123"]) == 0


def test_dry_run_changes_nothing(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    capsys.readouterr()
    main(["next", "ABC-123", "--dry-run"])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    # "evaluate done" would say the step ran, on the one path that runs nothing.
    assert "done" not in out
    assert fake_bin.calls == []
    main(["ABC-123"])
    assert "evaluate" in capsys.readouterr().out


def test_a_write_verb_without_a_key_is_an_error(env, capsys):
    assert main(["next"]) == 2


def test_repo_overrides_are_applied_for_the_ticket_repo(env, capsys, monkeypatch):
    config = env / "config.yml"
    # `draft-pr` has no dependent, so skipping it does not strand anything
    # (config.py rejects a skip that would — see
    # test_a_repo_skip_that_strands_a_dependent_is_an_error in test_config.py).
    config.write_text(
        CONFIG + "\nrepos:\n  acme/api:\n    steps:\n      skip: [draft-pr]\n"
    )
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["ABC-123"])
    out = capsys.readouterr().out
    assert "review-spec" in out
    assert "draft-pr" not in out


def test_next_warns_about_recorded_steps_the_config_no_longer_defines(
    env, capsys, tmp_path
):
    """A config edit collapsed two steps into one; the state file still holds the old ids.
    They are kept, excluded from the DAG, and named."""
    main(["track", "ABC-123", "--repo", "acme/api"])
    store = Store(tmp_path / "store")
    ticket = store.read_ticket("ABC-123")
    ticket["steps"] = {
        "evaluate": {"status": "done"},
        "jira-sync": {"status": "done"},
        "worktree": {"status": "done"},
    }
    store.write_ticket(ticket)

    assert main(["next", "ABC-123", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "jira-sync" in err
    assert "worktree" in err
    kept = Store(tmp_path / "store").read_ticket("ABC-123")["steps"]
    assert "jira-sync" in kept and "worktree" in kept
