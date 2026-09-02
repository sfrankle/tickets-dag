"""What `examples/` may be asked, and nothing more.

The example is documentation: it is copied, edited, and free to change its
repos, its ids and its counts. See `tests/CLAUDE.md` — this module asks only
whether it is self-consistent, whether it agrees with itself across files, and
whether it teaches something the engine would break on. Engine behaviour is
proved in `test_config.py` and `test_cli_surface.py`, against configs written
for the purpose.
"""

from pathlib import Path

import yaml

from ticket.cli import config_problems
from ticket.config import Config, load_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example() -> Config:
    return load_config(EXAMPLES / "config.yml")


# --- is it self-consistent? -----------------------------------------------


def test_the_example_config_loads():
    cfg = example()
    assert cfg.steps and cfg.reviews


def test_the_example_config_ships_nothing_broken():
    """#23.3, and the reason `--validate` can be trusted at all.

    Every `prompt:` and `run:` the example declares — steps, reviews and both
    `fix:` routes — resolves to a file that is there, readable, and executable
    where it has to be. A placeholder `repos.<repo>.path` is deliberately not
    in scope: that is a warning, and what the validator does with one is
    settled in `test_cli_surface.py`.
    """
    assert config_problems(example()) == []


# --- does it agree with itself across files? ------------------------------


def test_the_example_config_declares_a_severity_vocabulary():
    """The review prompts below spell out a legend, so the config that ships
    beside them has to declare one rather than lean on the built-in default.
    Which severities it picks is the example's business."""
    raw = yaml.safe_load((EXAMPLES / "config.yml").read_text())
    assert raw.get("severities")


def test_every_review_prompt_states_the_output_format():
    """Self-contained, every one of them.

    A `bot` review is posted as a PR comment and a `local` review runs in the
    ticket's checkout, so a prompt that points at a sibling file ships a
    reference the reviewer cannot resolve. Every marker is read off the config,
    so changing the vocabulary changes what this demands of the prompts.
    """
    cfg = example()
    for review in cfg.reviews:
        text = cfg.path_to(review.prompt).read_text()
        assert "**Verdict:**" in text, review.id
        assert "importance, not effort" in text, review.id
        for severity in cfg.severities:
            assert severity.marker in text, (review.id, severity.id)
        assert "prompts/reviews/" not in text, review.id


# --- does it teach something the engine would break on? -------------------


def test_a_handoff_that_edits_code_is_given_agent_mode():
    """`implement` and a `hard` fix both write files, and neither can without
    `--permission-mode` in `args:`. An example missing it ships a step that
    silently does nothing for everyone who copies it."""
    cfg = example()
    assert "--permission-mode" in cfg.step("implement").args
    assert "--permission-mode" in cfg.fix.args


def test_the_easy_fix_script_keeps_the_store_local_id_out_of_the_comment():
    """The finding's content goes in the comment; the id stays in the store, and reaches the script through the environment instead."""
    text = (EXAMPLES / "input" / "scripts" / "fix-easy.sh").read_text()
    body = text.split("body=$(")[1].split("EOF\n)")[0]
    assert "TICKET_FINDING_ID" not in body
    assert "TICKET_FINDING_SUMMARY" in body
    assert "TICKET_FINDING_TRAILER" in body


def test_the_worktree_script_honours_the_worktree_setting():
    """`TICKET_USE_WORKTREES` is the engine's half of the contract and
    `ticket-worktree:` on stdout is the script's; a script ignoring either one
    leaves every later step running in the wrong directory."""
    text = (EXAMPLES / "input" / "scripts" / "worktree.sh").read_text()
    assert "TICKET_USE_WORKTREES" in text
    assert "ticket-worktree:" in text
