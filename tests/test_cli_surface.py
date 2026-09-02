"""The CLI surface itself: which verbs the engine owns, which names come from config, and the one model of "a step exists" that every verb has to share.

Issue #12.
The bug the owner reported reads as a registration model — `ticket show KEY-1` lists `evaluate` and calls it `next`, then `ticket skip KEY-1 evaluate` answers "evaluate is not tracked" — but there was never a registration step to be missing.
`run`, `skip` and `release` took their positionals as `<step> <key>` while every other verb took `<key>` first, so that command looked up a ticket named `evaluate`.
The model is: config declares a stage, the store only records what has happened to it.
"""

import json
import os
import textwrap

import pytest

from tests.conftest import dead_pid, write_lock
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
    (tmp_path / "prompts" / "reviews" / "docs-tests.md").write_text("Docs.\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "draft-pr.sh"
    script.write_text("#!/bin/sh\necho 'ticket-pr: acme/api#115'\n")
    script.chmod(0o755)
    monkeypatch.setenv("TICKET_CONFIG", str(config))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


@pytest.fixture
def tracked(env):
    main(["track", "ABC-123", "--repo", "acme/api"])
    return env


# --- one model of "a step exists" -----------------------------------------


def test_the_reported_contradiction_is_gone(tracked, capsys):
    """`show` names `evaluate` as next; `skip KEY evaluate` must then work.

    Verbatim from the issue, minus the key.
    """
    assert main(["show", "ABC-123"]) == 0
    shown = capsys.readouterr().out
    assert "next: step evaluate" in shown

    assert main(["skip", "ABC-123", "evaluate"]) == 0
    out, err = capsys.readouterr()
    assert "not tracked" not in err
    assert "skipped step evaluate" in out


def test_a_declared_step_needs_no_per_key_registration(tracked, capsys):
    """Nothing has ever written a record for `draft-pr` on this ticket.

    It is in config, so every verb takes it anyway.
    """
    for argv in (
        ["skip", "ABC-123", "draft-pr", "--dry-run"],
        ["run", "ABC-123", "draft-pr", "--dry-run"],
        ["reset", "ABC-123", "draft-pr", "--dry-run"],
    ):
        assert main(argv) == 0, argv
        assert "not tracked" not in capsys.readouterr().err


def test_show_next_skip_and_run_agree_on_the_step_set(tracked, capsys):
    """Whatever `show` lists, `skip` and `run` accept, and nothing else."""
    main(["show", "ABC-123"])
    shown = capsys.readouterr().out
    listed = [
        line.split()[0]
        for line in shown.splitlines()
        if line.startswith("  ") and line.strip()
    ]
    assert listed == ["evaluate", "review-spec", "draft-pr"]

    for step in listed:
        assert main(["skip", "ABC-123", step, "--dry-run"]) == 0, step
        assert main(["run", "ABC-123", step, "--dry-run"]) == 0, step
        capsys.readouterr()

    assert main(["skip", "ABC-123", "nonesuch"]) == 1
    err = capsys.readouterr().err
    assert "not tracked" not in err
    assert "nonesuch" in err


def test_an_unknown_step_is_never_reported_as_an_untracked_ticket(tracked, capsys):
    assert main(["run", "ABC-123", "nonesuch"]) == 1
    err = capsys.readouterr().err
    assert "unknown step" in err
    assert "ticket track" not in err


def test_key_first_is_the_order_for_every_verb_that_takes_a_step(tracked, capsys):
    main(["run", "ABC-123", "evaluate", "--dry-run"])
    main(["release", "ABC-123", "review-spec", "--dry-run"])
    out = capsys.readouterr().out
    assert "evaluate" in out and "review-spec" in out


def test_the_old_step_first_order_still_works_and_says_it_moved(tracked, capsys):
    """Muscle memory, and every README written before this change.

    The swap is only taken when the first word is not a tracked key and the second is, so it can never silently pick the wrong ticket.
    """
    assert main(["skip", "evaluate", "ABC-123"]) == 0
    out, err = capsys.readouterr()
    assert "skipped step evaluate" in out
    assert "ticket skip ABC-123 evaluate" in err


def test_the_swap_is_not_taken_when_the_key_is_already_first(tracked, capsys):
    """The right order must not be "corrected" into anything, or noted at."""
    assert main(["skip", "ABC-123", "evaluate"]) == 0
    out, err = capsys.readouterr()
    assert "skipped step evaluate" in out
    assert "the key comes first" not in err


def test_the_swap_is_not_taken_when_both_words_name_tracked_tickets(env, capsys):
    """A ticket keyed after a stage must not pull the swap onto itself."""
    main(["track", "ABC-123", "--repo", "acme/api"])
    main(["track", "evaluate", "--repo", "acme/api"])
    capsys.readouterr()

    assert main(["skip", "ABC-123", "evaluate"]) == 0
    out, err = capsys.readouterr()
    assert "skipped step evaluate" in out
    assert "the key comes first" not in err


def test_a_mistyped_key_is_reported_as_the_key_the_user_typed(tracked, capsys):
    """Two words that both look like keys, one of them tracked.

    `ABC-999` names no stage, so this is a typo in the key, not the old order.
    Swapping it would answer about `ABC-123` and print a note claiming a
    correction the user never made.
    """
    assert main(["skip", "ABC-999", "ABC-123"]) == 1
    err = capsys.readouterr().err
    assert "ABC-999" in err
    assert "the key comes first" not in err


def test_neither_word_tracked_leaves_the_key_where_it_was(tracked, capsys):
    assert main(["skip", "evaluate", "ZZZ-1"]) == 1
    err = capsys.readouterr().err
    assert "the key comes first" not in err
    assert "evaluate" in err


def test_the_hints_the_cli_prints_use_the_order_the_cli_takes(tracked, capsys):
    """A hint is a copy-paste target. One printing the old order would trip
    the deprecation path of the very change that wrote it."""
    main(["run", "ABC-123", "review-spec"])
    assert "ticket release ABC-123 review-spec" in capsys.readouterr().out

    main(["skip", "ABC-123", "evaluate"])
    capsys.readouterr()
    main(["next", "ABC-123"])
    assert "ticket release ABC-123 review-spec" in capsys.readouterr().out


def test_decide_and_findings_keep_the_key_first(tracked, capsys):
    """They already did; pinned so the whole surface stays one shape.

    Both stop on the missing PR, which is only reachable once the key parsed as a key: the wrong order would have said "f01 is not tracked".
    """
    assert main(["findings", "ABC-123"]) == 1
    assert main(["decide", "ABC-123", "f01", "covered elsewhere"]) == 1
    err = capsys.readouterr().err
    assert "no PR" in err
    assert "not tracked" not in err


# --- management verbs versus stages ---------------------------------------


def test_stages_lists_what_config_declares(tracked, capsys):
    assert main(["stages"]) == 0
    out = capsys.readouterr().out
    for name in ("evaluate", "review-spec", "draft-pr", "docs-tests"):
        assert name in out
    assert "handoff" in out and "gate" in out and "script" in out


def test_stages_list_flag_is_accepted(tracked, capsys):
    assert main(["stages", "--list"]) == 0
    assert "evaluate" in capsys.readouterr().out


def test_stages_json_is_machine_readable(tracked, capsys):
    assert main(["stages", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in doc["steps"]] == ["evaluate", "review-spec", "draft-pr"]
    assert [r["id"] for r in doc["reviews"]] == ["docs-tests"]


def test_help_never_names_a_config_declared_stage(env, capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in ("evaluate", "review-spec", "draft-pr", "docs-tests"):
        assert name not in out


def test_help_separates_engine_verbs_from_stage_verbs(env, capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "management commands" in out
    assert "stage commands" in out
    # Management verbs are the ones argparse lists; the stage verbs are held back into their own block, below.
    head, _, tail = out.partition("stage commands")
    assert "track" in head and "refresh" in head and "show" in head
    assert "run" in tail and "skip" in tail and "collect" in tail
    assert "ticket stages --list" in out


# --- config and validation ------------------------------------------------


def test_config_shows_the_resolved_config(tracked, capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "config.yml" in out
    assert "store" in out
    assert "evaluate" in out and "docs-tests" in out


def test_config_json_is_machine_readable(tracked, capsys):
    assert main(["config", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["valid"] is True
    assert doc["problems"] == []
    assert doc["steps"][0]["id"] == "evaluate"


def test_config_validate_passes_on_a_good_config(tracked, capsys):
    assert main(["config", "--validate"]) == 0
    assert "ok" in capsys.readouterr().out


def test_config_validate_names_a_prompt_that_is_not_there(tracked, capsys):
    (tracked / "prompts" / "evaluate.md").unlink()
    assert main(["config", "--validate"]) == 1
    out = capsys.readouterr().out
    assert "evaluate" in out
    assert "prompts/evaluate.md" in out


def test_config_validate_names_a_script_that_is_not_there(tracked, capsys):
    (tracked / "scripts" / "draft-pr.sh").unlink()
    assert main(["config", "--validate"]) == 1
    assert "draft-pr" in capsys.readouterr().out


def test_config_validate_names_a_script_that_cannot_be_run(tracked, capsys):
    (tracked / "scripts" / "draft-pr.sh").chmod(0o644)
    assert main(["config", "--validate"]) == 1
    out = capsys.readouterr().out
    assert "draft-pr" in out and "not executable" in out


def test_config_validate_reports_a_cycle_rather_than_raising(tracked, capsys):
    (tracked / "config.yml").write_text(
        textwrap.dedent("""
            models: {opus: claude-opus-5}
            defaults: {model: opus}
            steps:
              - id: a
                prompt: prompts/evaluate.md
                needs: [b]
              - id: b
                prompt: prompts/evaluate.md
                needs: [a]
        """)
    )
    assert main(["config", "--validate"]) == 1
    assert "cycle" in capsys.readouterr().out


def test_config_validate_reports_an_unknown_key(tracked, capsys):
    (tracked / "config.yml").write_text(CONFIG + "\ntracker:\n  sumary: [jira]\n")
    assert main(["config", "--validate"]) == 1
    out = capsys.readouterr().out
    assert "sumary" in out


# --- reading a step's log -------------------------------------------------


def test_log_prints_what_a_step_recorded(tracked, capsys, fake_bin):
    fake_bin.respond("claude", stdout="evaluated the ticket")
    main(["run", "ABC-123", "evaluate"])
    capsys.readouterr()
    assert main(["log", "ABC-123", "evaluate"]) == 0
    assert "evaluated the ticket" in capsys.readouterr().out


def test_log_says_so_when_a_step_has_never_run(tracked, capsys):
    assert main(["log", "ABC-123", "draft-pr"]) == 0
    assert "no log recorded" in capsys.readouterr().out


def test_log_rejects_a_step_config_does_not_declare(tracked, capsys):
    assert main(["log", "ABC-123", "nonesuch"]) == 1
    assert "unknown step" in capsys.readouterr().err


# --- clearing a lock a dead run left behind (issue #27) --------------------


def locks(env):
    return Store(env / "store")


def test_unlock_clears_a_lock_whose_run_is_gone(tracked, capsys):
    path = write_lock(locks(tracked), f"{dead_pid()}\n")
    assert main(["unlock", "ABC-123"]) == 0
    assert not path.exists()
    assert "cleared" in capsys.readouterr().out


def test_unlock_refuses_while_the_run_is_still_alive(tracked, capsys):
    path = write_lock(locks(tracked), f"{os.getpid()}\n")
    assert main(["unlock", "ABC-123"]) == 1
    assert path.exists(), "a live run's lock must survive"
    err = capsys.readouterr().err
    assert str(os.getpid()) in err


def test_unlock_says_so_when_there_is_no_lock(tracked, capsys):
    assert main(["unlock", "ABC-123"]) == 0
    assert "not locked" in capsys.readouterr().out


def test_unlock_does_not_take_the_lock_it_is_clearing(tracked, capsys):
    path = write_lock(locks(tracked), "")
    assert main(["unlock", "ABC-123"]) == 0
    assert not path.exists()
