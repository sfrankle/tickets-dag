import textwrap
from pathlib import Path

import pytest

from ticket.config import config_path, load_config
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
    bad = SAMPLE.replace("\nreviews:", "\n  - id: evaluate\n    gate: true\n\nreviews:")
    with pytest.raises(ConfigError, match="duplicate step id: evaluate"):
        load_config(write(tmp_path, bad))


def test_unknown_needs_target_is_an_error(tmp_path):
    bad = SAMPLE.replace("needs: [evaluate]", "needs: [nonesuch]")
    with pytest.raises(ConfigError, match="nonesuch"):
        load_config(write(tmp_path, bad))


def test_step_with_no_recognised_kind_is_an_error(tmp_path):
    bad = SAMPLE.replace(
        "  - id: worktree\n    run: scripts/worktree.sh\n", "  - id: worktree\n"
    )
    with pytest.raises(ConfigError, match="worktree"):
        load_config(write(tmp_path, bad))


def test_repo_override_naming_an_unknown_review_is_an_error(tmp_path):
    bad = SAMPLE.replace("reviews: [docs-tests]", "reviews: [nonesuch]")
    with pytest.raises(ConfigError, match="nonesuch"):
        load_config(write(tmp_path, bad))


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no config"):
        load_config(tmp_path / "absent.yml")


def test_fix_defaults_to_the_default_model_with_no_args(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(SAMPLE)
    cfg = load_config(path)
    assert (cfg.fix.model, cfg.fix.args) == ("opus", ())


def test_an_unknown_fix_model_fails_at_load(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(SAMPLE + "\nfix:\n  model: sonnet\n")
    with pytest.raises(ConfigError, match=r"fix\.model"):
        load_config(path)


def test_malformed_yaml_is_a_config_error_not_a_traceback(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("steps: [\n  - id: evaluate\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


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


def test_local_review_with_no_resolvable_model_is_an_error(tmp_path):
    """Mirrors the handoff no-model check: a `dispatch: local` review with no
    `model:` and no `defaults.model` must fail at load time, not mid-DAG."""
    bad = SAMPLE.replace(
        "    dispatch: local\n    model: opus\n", "    dispatch: local\n"
    )
    bad = bad.replace("defaults:\n  model: opus\n", "")
    with pytest.raises(ConfigError, match="architecture"):
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


def test_tracker_defaults_to_no_lookup(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.tracker.summary == ()


def test_tracker_summary_is_an_argv_list_with_the_key_left_to_substitute(tmp_path):
    text = SAMPLE + textwrap.dedent("""
        tracker:
          summary: [jira, issue, view, "{key}", --plain]
    """)
    cfg = load_config(write(tmp_path, text))
    assert cfg.tracker.summary == ("jira", "issue", "view", "{key}", "--plain")
    assert cfg.tracker.summary_argv("ABC-123") == [
        "jira",
        "issue",
        "view",
        "ABC-123",
        "--plain",
    ]


def test_tracker_summary_must_be_a_list(tmp_path):
    text = SAMPLE + '\ntracker:\n  summary: "jira issue view {key}"\n'
    with pytest.raises(ConfigError, match=r"tracker\.summary"):
        load_config(write(tmp_path, text))


def test_tracker_summary_rejects_an_empty_list(tmp_path):
    text = SAMPLE + "\ntracker:\n  summary: []\n"
    with pytest.raises(ConfigError, match=r"tracker\.summary"):
        load_config(write(tmp_path, text))


def test_tracker_must_be_a_mapping(tmp_path):
    text = SAMPLE + "\ntracker: jira\n"
    with pytest.raises(ConfigError, match="tracker:"):
        load_config(write(tmp_path, text))


SEVERITIES = textwrap.dedent("""
    models:
      opus: claude-opus-5

    defaults:
      model: opus

    severities:
      - id: nit
        marker: "N"
      - id: must-fix
        marker: "M"
        default: true

    steps:
      - id: evaluate
        model: opus
        prompt: prompts/evaluate.md
""")


def test_severities_default_to_the_built_in_three(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert [s.id for s in cfg.severities] == [
        "blocking",
        "maintenance",
        "architecture",
    ]
    assert [s.marker for s in cfg.severities] == ["🔴", "🟡", "🔵"]
    assert cfg.default_severity == "maintenance"


def test_configured_severities_replace_the_default_set(tmp_path):
    cfg = load_config(write(tmp_path, SEVERITIES))
    assert [s.id for s in cfg.severities] == ["nit", "must-fix"]
    assert cfg.default_severity == "must-fix"


def test_severity_for_marker_finds_the_id_in_a_summary(tmp_path):
    cfg = load_config(write(tmp_path, SEVERITIES))
    assert cfg.severity_for_marker("M Must fix these") == "must-fix"
    assert cfg.severity_for_marker("nothing here") is None


def test_severity_rank_follows_config_order(tmp_path):
    cfg = load_config(write(tmp_path, SEVERITIES))
    assert cfg.severity_rank("nit") < cfg.severity_rank("must-fix")
    assert cfg.severity_rank("unknown") > cfg.severity_rank("must-fix")


def test_duplicate_severity_id_is_an_error(tmp_path):
    text = SEVERITIES.replace("- id: must-fix", "- id: nit")
    with pytest.raises(ConfigError, match="duplicate severity id: nit"):
        load_config(write(tmp_path, text))


def test_duplicate_severity_marker_is_an_error(tmp_path):
    text = SEVERITIES.replace('marker: "M"', 'marker: "N"')
    with pytest.raises(ConfigError, match="duplicate severity marker"):
        load_config(write(tmp_path, text))


def test_severities_need_exactly_one_default(tmp_path):
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(write(tmp_path, SEVERITIES.replace("    default: true\n", "")))


def test_two_default_severities_is_an_error(tmp_path):
    text = SEVERITIES.replace(
        '    marker: "N"\n', '    marker: "N"\n    default: true\n'
    )
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(write(tmp_path, text))


def test_empty_severities_list_is_an_error(tmp_path):
    text = SEVERITIES[: SEVERITIES.index("severities:")] + "severities: []\n"
    with pytest.raises(ConfigError, match="severities"):
        load_config(write(tmp_path, text))


def test_severity_without_an_id_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="every severity needs an id"):
        load_config(write(tmp_path, SEVERITIES.replace("- id: nit", "- marker2: nit")))


def test_marker_is_optional(tmp_path):
    cfg = load_config(write(tmp_path, SEVERITIES.replace('    marker: "N"\n', "")))
    assert cfg.severities[0].marker is None
    assert cfg.severity_for_marker("N") is None


PARSE = SAMPLE + textwrap.dedent("""
    parse:
      sources:
        - author: odd-bot[bot]
          bullet: '^\\s{0,3}>>\\s+'
""")


def test_parse_sources_are_optional(tmp_path):
    """The built-in grammar is the road; a profile is the escape hatch."""
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.parse_sources == ()
    assert cfg.parse_source("odd-bot[bot]") is None


def test_a_parse_source_is_found_by_author(tmp_path):
    cfg = load_config(write(tmp_path, PARSE))
    assert cfg.parse_source("odd-bot[bot]").bullet.pattern == r"^\s{0,3}>>\s+"
    assert cfg.parse_source("someone-else") is None
    assert cfg.parse_source(None) is None


def test_a_parse_source_needs_an_author(tmp_path):
    text = PARSE.replace("- author: odd-bot[bot]\n", "- ")
    with pytest.raises(ConfigError, match="parse source"):
        load_config(write(tmp_path, text))


def test_a_bad_parse_regex_fails_at_load(tmp_path):
    """A typo here should fail on load, not on the one review that needs it."""
    text = PARSE.replace(r"'^\s{0,3}>>\s+'", "'^(unclosed'")
    with pytest.raises(ConfigError, match="not a valid regex"):
        load_config(write(tmp_path, text))


def test_a_details_override_needs_its_named_groups(tmp_path):
    text = PARSE.replace(r"bullet: '^\s{0,3}>>\s+'", "details: '<block>(.*?)</block>'")
    with pytest.raises(ConfigError, match="named groups"):
        load_config(write(tmp_path, text))


def test_a_parse_source_that_overrides_nothing_is_an_error(tmp_path):
    text = PARSE.replace("      bullet: '^\\s{0,3}>>\\s+'\n", "")
    with pytest.raises(ConfigError, match="overrides nothing"):
        load_config(write(tmp_path, text))

# --- unknown keys ---------------------------------------------------------
#
# Issues #6 and #8: a typo under a known block used to be dropped in silence,
# so `tracker: {sumary: ...}` looked configured and did nothing. A key this
# loader does not know is a mistake, and mistakes are loud.


def test_an_unknown_top_level_key_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, SAMPLE + "\nreviwes: []\n"))
    assert "reviwes" in str(exc.value)


def test_an_unknown_key_under_tracker_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, SAMPLE + "\ntracker:\n  sumary: [jira]\n"))
    assert "sumary" in str(exc.value)
    assert "tracker" in str(exc.value)


def test_an_unknown_key_on_a_step_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                SAMPLE.replace(
                    "    gate: true", "    gate: true\n    need: [evaluate]"
                ),
            )
        )
    assert "need" in str(exc.value)


def test_an_unknown_key_on_a_review_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                SAMPLE.replace("    dispatch: bot", "    dispatch: bot\n    oder: 3"),
            )
        )
    assert "oder" in str(exc.value)


def test_an_unknown_key_under_worktrees_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, SAMPLE + "\nworktrees:\n  enabld: false\n"))
    assert "enabld" in str(exc.value)


def test_an_unknown_key_under_a_repo_override_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, SAMPLE + "\n    revews: [docs-tests]\n"))
    assert "revews" in str(exc.value)


def test_an_unknown_key_under_defaults_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(
                tmp_path,
                SAMPLE.replace(
                    "defaults:\n  model: opus",
                    "defaults:\n  model: opus\n  modell: haiku",
                ),
            )
        )
    assert "modell" in str(exc.value)


def test_a_known_key_everywhere_still_loads(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert next(s.id for s in cfg.steps) == "evaluate"
