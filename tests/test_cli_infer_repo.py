"""`ticket track KEY` with no `--repo`: work the repo out, or say what is missing.

Issue #8: "`ticket track <jira-key>` errors needing a repo. Not always 1-1
jira-pr; not sure about repo being required." The tracker here is a real
script, because the whole point is that the engine only knows how to run one.
"""

import json
import textwrap

import pytest

from ticket.cli import main
from ticket.store import Store

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5}
    defaults: {model: opus}

    owner: sfrankle

    tracker:
      summary: [{tracker}, "{key}"]

    steps:
      - id: evaluate
        prompt: prompts/evaluate.md

    repos:
      sfrankle/content-security-mode:
        aliases: [CSM]
      sfrankle/tickets-dag:
        aliases: [DAG]

    infer:
      repo:
        patterns:
          - "[{alias}]"
          - "[{repo}]"
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    tracker = scripts / "summary.sh"
    # Echoes whatever the harness wrote for this key, so a test sets a title by
    # writing a file rather than by patching anything.
    tracker.write_text(
        '#!/bin/sh\ncat "$(dirname "$0")/../titles/$1" 2>/dev/null || true\n'
    )
    tracker.chmod(0o755)
    (tmp_path / "titles").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate.\n")
    config = tmp_path / "config.yml"
    config.write_text(
        CONFIG.replace("{tracker}", str(tracker)).replace('"{key}"', '"{key}"')
    )
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


def title(env, key: str, text: str) -> None:
    (env / "titles" / key).write_text(text + "\n")


def row(env, key: str) -> dict:
    return Store(env / "store").read_ticket(key)


def test_track_needs_no_repo(env):
    title(env, "ABC-123", "[CSM] tighten the session cookie")
    assert main(["track", "ABC-123"]) == 0


def test_the_repo_comes_from_the_summary(env):
    title(env, "ABC-123", "[CSM] tighten the session cookie")
    main(["track", "ABC-123"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/content-security-mode"


def test_the_summary_is_recorded_too(env):
    title(env, "ABC-123", "[CSM] tighten the session cookie")
    main(["track", "ABC-123"])
    assert row(env, "ABC-123")["summary"] == "[CSM] tighten the session cookie"


def test_an_explicit_repo_beats_inference(env):
    title(env, "ABC-123", "[CSM] tighten the session cookie")
    main(["track", "ABC-123", "--repo", "sfrankle/tickets-dag"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/tickets-dag"


def test_an_explicit_repo_may_be_an_alias(env):
    main(["track", "ABC-123", "--repo", "DAG"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/tickets-dag"


def test_an_explicit_bare_repo_gains_the_owner(env):
    main(["track", "ABC-123", "--repo", "tickets-dag"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/tickets-dag"


def test_an_unresolved_repo_still_tracks_the_ticket(env, capsys):
    title(env, "ABC-123", "nothing here names a repo")
    assert main(["track", "ABC-123"]) == 0
    capsys.readouterr()
    assert row(env, "ABC-123")["tracked"] is True


def test_an_unresolved_repo_warns_and_says_why(env, capsys):
    title(env, "ABC-123", "nothing here names a repo")
    main(["track", "ABC-123"])
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "no repo named in the summary" in err


def test_an_unresolved_repo_names_the_way_out(env, capsys):
    """A warning that does not say what to type is a warning you ignore."""
    title(env, "ABC-123", "nothing here names a repo")
    main(["track", "ABC-123"])
    assert "ticket track ABC-123 --repo" in capsys.readouterr().err


def test_an_ambiguous_summary_is_not_guessed(env, capsys):
    title(env, "ABC-123", "[CSM] and [DAG] both")
    main(["track", "ABC-123"])
    err = capsys.readouterr().err
    assert "sfrankle/content-security-mode" in err
    assert "sfrankle/tickets-dag" in err
    assert row(env, "ABC-123").get("repo", "") == ""


def test_a_later_track_fills_in_the_repo_it_could_not_infer(env):
    title(env, "ABC-123", "nothing here names a repo")
    main(["track", "ABC-123"])
    main(["track", "ABC-123", "--repo", "CSM"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/content-security-mode"


def test_inference_does_not_overwrite_a_repo_already_recorded(env):
    """Re-tracking must not move a ticket someone pointed at a repo by hand."""
    main(["track", "ABC-123", "--repo", "DAG"])
    title(env, "ABC-123", "[CSM] retitled later")
    main(["track", "ABC-123"])
    assert row(env, "ABC-123")["repo"] == "sfrankle/tickets-dag"


def test_the_json_row_carries_the_inferred_repo(env, capsys):
    title(env, "ABC-123", "[CSM] tighten the session cookie")
    main(["track", "ABC-123"])
    capsys.readouterr()
    main(["show", "ABC-123", "--json"])
    assert json.loads(capsys.readouterr().out)["repo"] == (
        "sfrankle/content-security-mode"
    )


def test_config_shows_the_owner(env, capsys):
    main(["config"])
    assert "owner: sfrankle" in capsys.readouterr().out


def test_config_shows_each_repo_with_its_aliases(env, capsys):
    main(["config"])
    out = capsys.readouterr().out
    assert "sfrankle/content-security-mode (CSM)" in out


def test_config_shows_the_infer_patterns(env, capsys):
    main(["config"])
    assert "infer.repo: 2 pattern" in capsys.readouterr().out


def test_config_json_carries_the_owner_and_aliases(env, capsys):
    main(["config", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["owner"] == "sfrankle"
    assert data["repos"]["sfrankle/tickets-dag"]["aliases"] == ["DAG"]
