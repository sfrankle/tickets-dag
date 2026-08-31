from pathlib import Path

from ticket.config import load_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_example_config_loads():
    cfg = load_config(EXAMPLES / "config.yml")
    assert next(s.id for s in cfg.steps) == "evaluate"
    assert [r.id for r in cfg.reviews] == ["docs-tests", "architecture", "security"]


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
    assert [r.id for r in cfg.for_repo("acme/api").reviews] == ["docs-tests"]
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


def test_the_worktree_script_honours_the_worktree_setting():
    text = (EXAMPLES / "scripts" / "worktree.sh").read_text()
    assert "TICKET_USE_WORKTREES" in text
    assert "ticket-worktree:" in text


def test_every_review_prompt_states_the_output_format():
    cfg = load_config(EXAMPLES / "config.yml")
    for review in cfg.reviews:
        text = cfg.path_to(review.prompt).read_text()
        assert "**Verdict:**" in text, review.id
        assert "importance, not effort" in text, review.id
