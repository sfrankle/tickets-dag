from pathlib import Path

import yaml

from ticket.config import load_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_example_config_loads():
    cfg = load_config(EXAMPLES / "config.yml")
    assert next(s.id for s in cfg.steps) == "evaluate"
    assert [r.id for r in cfg.reviews] == ["correctness", "security"]


def test_every_example_prompt_referenced_by_the_config_exists():
    cfg = load_config(EXAMPLES / "config.yml")
    for step in cfg.steps:
        if step.prompt:
            assert cfg.path_to(step.prompt).is_file(), step.id
        if step.run:
            assert cfg.path_to(step.run).is_file(), step.id
    for review in cfg.reviews:
        assert cfg.path_to(review.prompt).is_file(), review.id


def test_example_repo_overrides_resolve():
    cfg = load_config(EXAMPLES / "config.yml")
    assert [r.id for r in cfg.for_repo("acme/api").reviews] == ["correctness"]
    assert "describe" not in [s.id for s in cfg.for_repo("acme/infra").steps]


def test_the_example_config_configures_worktrees_and_sync():
    cfg = load_config(EXAMPLES / "config.yml")
    assert cfg.worktrees.enabled is True
    assert cfg.sync is True
    assert cfg.repo_path("acme/api") is not None


def test_the_implement_step_can_write_files():
    """A handoff that edits code needs agent mode, which lives in `args:`."""
    cfg = load_config(EXAMPLES / "config.yml")
    assert "--permission-mode" in cfg.step("implement").args


def test_the_example_fix_block_can_write_files():
    """A hard fix is a handoff that edits code, so it needs agent mode too."""
    cfg = load_config(EXAMPLES / "config.yml")
    assert "--permission-mode" in cfg.fix.args


def test_the_worktree_script_honours_the_worktree_setting():
    text = (EXAMPLES / "input" / "scripts" / "worktree.sh").read_text()
    assert "TICKET_USE_WORKTREES" in text
    assert "ticket-worktree:" in text


def test_the_example_config_declares_its_severities():
    """The example prompts spell out a legend, so the config it ships with has
    to declare the same one rather than lean on the built-in default."""
    cfg = load_config(EXAMPLES / "config.yml")
    raw = yaml.safe_load((EXAMPLES / "config.yml").read_text())
    assert "severities" in raw
    assert [s.id for s in cfg.severities] == [
        "blocking",
        "maintenance",
        "architecture",
    ]


def test_every_review_prompt_states_the_output_format():
    """Self-contained, every one of them.

    A `bot` review is posted as a PR comment and a `local` review runs in the
    ticket's checkout, so a prompt that points at a sibling file ships a
    reference the reviewer cannot resolve.
    """
    cfg = load_config(EXAMPLES / "config.yml")
    for review in cfg.reviews:
        text = cfg.path_to(review.prompt).read_text()
        assert "**Verdict:**" in text, review.id
        assert "importance, not effort" in text, review.id
        for severity in cfg.severities:
            assert severity.marker in text, (review.id, severity.id)
        assert "prompts/reviews/" not in text, review.id
