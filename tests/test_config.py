import textwrap
from pathlib import Path

import pytest

from ticket.config import Config, config_path, load_config
from ticket.errors import ConfigError

SAMPLE = textwrap.dedent("""
    store: ~/.ticket

    models:
      opus: claude-opus-5
      haiku: claude-haiku-4-5-20251001

    defaults:
      model: opus

    steps:
      - id: evaluate
        model: opus
        prompt: prompts/evaluate.md
      - id: review-spec
        gate: true
        needs: [evaluate]
      - id: worktree
        run: scripts/worktree.sh
        needs: [review-spec]
      - id: describe
        model: haiku
        prompt: prompts/describe.md
        needs: [worktree]

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

    repos:
      acme/api:
        reviews: [docs-tests]
      acme/infra:
        steps:
          skip: [describe]
""")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(text)
    return path


def test_loads_steps_with_inferred_kinds(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    kinds = {s.id: s.kind for s in cfg.steps}
    assert kinds == {
        "evaluate": "handoff",
        "review-spec": "gate",
        "worktree": "script",
        "describe": "handoff",
    }


def test_needs_defaults_to_empty_tuple(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.step("evaluate").needs == ()
    assert cfg.step("review-spec").needs == ("evaluate",)


def test_model_alias_resolves_to_id(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.model_id("opus") == "claude-opus-5"
    assert cfg.model_id("haiku") == "claude-haiku-4-5-20251001"


def test_unknown_alias_is_an_error(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    with pytest.raises(ConfigError, match="sonnet"):
        cfg.model_id("sonnet")


def test_reviews_sorted_by_order(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert [r.id for r in cfg.reviews] == ["docs-tests", "architecture"]


def test_repo_override_replaces_review_list(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE)).for_repo("acme/api")
    assert [r.id for r in cfg.reviews] == ["docs-tests"]


def test_repo_override_subtracts_skipped_steps(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE)).for_repo("acme/infra")
    assert [s.id for s in cfg.steps] == ["evaluate", "review-spec", "worktree"]
    assert [r.id for r in cfg.reviews] == ["docs-tests", "architecture"]


def test_unknown_repo_is_the_base_config(tmp_path):
    base = load_config(write(tmp_path, SAMPLE))
    assert base.for_repo("acme/nothing") == base


def test_root_is_the_config_directory(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.root == tmp_path


def test_store_env_var_beats_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "elsewhere"))
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.store == tmp_path / "elsewhere"


def test_store_tilde_expands(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.store == Path("~/.ticket").expanduser()


def test_store_defaults_to_the_config_directory(tmp_path):
    # Decision #24: no `store:` key means state lives beside the config, so
    # `$TICKET_CONFIG` alone relocates the whole root.
    bare = SAMPLE.replace("store: ~/.ticket\n", "")
    cfg = load_config(write(tmp_path, bare))
    assert cfg.store == tmp_path


def test_relative_store_anchors_to_the_config_directory(tmp_path, monkeypatch):
    # Decision #25: not to the cwd, which changes per invocation.
    monkeypatch.chdir(tmp_path.parent)
    rel = SAMPLE.replace("store: ~/.ticket", "store: state")
    cfg = load_config(write(tmp_path, rel))
    assert cfg.store == tmp_path / "state"


def test_relative_worktree_root_anchors_to_the_config_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)
    rel = SAMPLE + "\nworktrees:\n  root: trees\n"
    cfg = load_config(write(tmp_path, rel))
    assert cfg.worktrees.root == tmp_path / "trees"
    assert cfg.worktrees.path_for("ABC-1") == tmp_path / "trees" / "ABC-1"


def test_worktree_root_defaults_to_home(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.worktrees.root == Path("~/worktrees").expanduser()


def test_config_path_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_CONFIG", str(tmp_path / "other.yml"))
    assert config_path() == tmp_path / "other.yml"


def test_duplicate_step_id_is_an_error(tmp_path):
    bad = SAMPLE.replace(
        "\nreviews:", "\n  - id: evaluate\n    gate: true\n\nreviews:"
    )
    with pytest.raises(ConfigError, match="duplicate step id: evaluate"):
        load_config(write(tmp_path, bad))


def test_unknown_needs_target_is_an_error(tmp_path):
    bad = SAMPLE.replace("needs: [evaluate]", "needs: [nonesuch]")
    with pytest.raises(ConfigError, match="nonesuch"):
        load_config(write(tmp_path, bad))


def test_step_with_no_recognised_kind_is_an_error(tmp_path):
    bad = SAMPLE.replace("  - id: worktree\n    run: scripts/worktree.sh\n",
                         "  - id: worktree\n")
    with pytest.raises(ConfigError, match="worktree"):
        load_config(write(tmp_path, bad))


def test_repo_override_naming_an_unknown_review_is_an_error(tmp_path):
    bad = SAMPLE.replace("reviews: [docs-tests]", "reviews: [nonesuch]")
    with pytest.raises(ConfigError, match="nonesuch"):
        load_config(write(tmp_path, bad))


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no config"):
        load_config(tmp_path / "absent.yml")


def test_a_cycle_in_needs_is_an_error(tmp_path):
    """`needs` describes a DAG; a cycle would otherwise surface as `at rest`."""
    bad = SAMPLE.replace(
        "\nreviews:",
        "\n  - id: loop-a\n    gate: true\n    needs: [loop-b]\n"
        "  - id: loop-b\n    gate: true\n    needs: [loop-a]\n\nreviews:",
    )
    with pytest.raises(ConfigError, match="cycle"):
        load_config(write(tmp_path, bad))


def test_duplicate_review_order_is_an_error(tmp_path):
    bad = SAMPLE.replace("    order: 2\n", "    order: 1\n")
    with pytest.raises(ConfigError, match="duplicate order"):
        load_config(write(tmp_path, bad))


def test_a_repo_skip_that_strands_a_dependent_is_an_error(tmp_path):
    """`repos.steps.skip` removes the step; `ticket skip` marks it satisfied."""
    bad = SAMPLE.replace("skip: [describe]", "skip: [review-spec]")
    with pytest.raises(ConfigError, match="but worktree needs it"):
        load_config(write(tmp_path, bad)).for_repo("acme/infra")


def test_worktrees_default_to_enabled(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.worktrees.enabled is True
    assert cfg.worktrees.branch_for("ABC-123", "acme/api") == "ABC-123"


def test_worktrees_can_be_turned_off_and_the_branch_templated(tmp_path):
    extra = SAMPLE + textwrap.dedent("""
        worktrees:
          enabled: false
          root: ~/elsewhere
          branch: "{owner}/{key}"
    """)
    cfg = load_config(write(tmp_path, extra))
    assert cfg.worktrees.enabled is False
    assert cfg.worktrees.branch_for("ABC-123", "acme/api") == "acme/ABC-123"


def test_repo_path_is_read_from_the_repo_override(tmp_path):
    extra = SAMPLE.replace(
        "  acme/api:\n    reviews: [docs-tests]",
        "  acme/api:\n    reviews: [docs-tests]\n    path: ~/code/api",
    )
    cfg = load_config(write(tmp_path, extra))
    assert cfg.repo_path("acme/api") == Path("~/code/api").expanduser()
    assert cfg.repo_path("acme/nothing") is None


def test_sync_defaults_on(tmp_path):
    assert load_config(write(tmp_path, SAMPLE)).sync is True
