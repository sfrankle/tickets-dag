import json
import textwrap

import pytest

from ticket.cli import main

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
    (tmp_path / "prompts" / "reviews" / "docs-tests.md").write_text("Check docs and tests.\n")
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
    main(["release", "review-spec", "ABC-123"])
    main(["next", "ABC-123"])
    assert "acme/api#115" in capsys.readouterr().out


def test_run_reruns_a_named_step(env, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["run", "evaluate", "ABC-123"])
    main(["run", "evaluate", "ABC-123"])
    assert len(fake_bin.calls_to("claude")) == 2


def test_run_rejects_an_unknown_step(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    assert main(["run", "nonesuch", "ABC-123"]) == 1
    assert "unknown step" in capsys.readouterr().err


def test_skip_marks_a_step_and_the_resolver_walks_past(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["skip", "evaluate", "ABC-123", "--reason", "trivial"])
    main(["next", "ABC-123"])
    assert "parked" in capsys.readouterr().out


def test_skip_resolves_a_review_id_when_it_is_not_a_step(env, capsys):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["skip", "docs-tests", "ABC-123"])
    assert main(["ABC-123"]) == 0


def test_dry_run_changes_nothing(env, capsys, fake_bin):
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["next", "ABC-123", "--dry-run"])
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
    config.write_text(CONFIG + "\nrepos:\n  acme/api:\n    steps:\n      skip: [draft-pr]\n")
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["ABC-123"])
    out = capsys.readouterr().out
    assert "review-spec" in out
    assert "draft-pr" not in out
