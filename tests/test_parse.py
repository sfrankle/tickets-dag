import json
import textwrap
from pathlib import Path

import pytest

from ticket.collect import _fingerprint
from ticket.config import load_config
from ticket.parse import (
    DASHES,
    MAX_SUMMARY,
    _cap,
    haiku_prompt,
    parse,
    parse_haiku,
    parse_script,
    script_parse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reviews"

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("x\n")
    return load_config(path)


def body(name):
    return (FIXTURES / name).read_text()


def test_script_parses_the_example_review(cfg):
    findings = parse_script(cfg, body("example-review.md"))
    assert [f["severity"] for f in findings] == [
        "blocking",
        "blocking",
        "maintenance",
    ]


def test_script_extracts_the_file_from_a_bullet(cfg):
    findings = parse_script(cfg, body("example-review.md"))
    assert findings[0]["file"] == "workflows/manifest-status-update.md"
    assert findings[2]["file"] == "README.md"


def test_script_keeps_the_whole_bullet_as_the_body(cfg):
    findings = parse_script(cfg, body("example-review.md"))
    assert "collect.team-prs" in findings[0]["body"]


def test_script_summary_is_one_line(cfg):
    findings = parse_script(cfg, body("example-review.md"))
    assert "\n" not in findings[0]["summary"]


def test_none_blocks_produce_no_findings(cfg):
    findings = parse_script(cfg, body("example-review.md"))
    assert all(f["severity"] != "architecture" for f in findings)


def test_script_marks_its_findings_parsed_by_script(cfg):
    assert all(
        f["parsed_by"] == "script" for f in parse_script(cfg, body("example-review.md"))
    )


def test_script_returns_none_without_a_verdict_line(cfg):
    assert parse_script(cfg, body("malformed.md")) is None


def test_script_returns_none_for_a_human_comment(cfg):
    assert parse_script(cfg, body("human-comment.md")) is None


def test_script_returns_none_for_a_foreign_bot(cfg):
    assert parse_script(cfg, body("review-bot.md")) is None


def test_script_never_sets_effort(cfg):
    """Severity is importance, not effort. Parsing must not set effort at all."""
    assert all("effort" not in f for f in parse_script(cfg, body("example-review.md")))


def test_haiku_fallback_is_used_for_a_human_comment(cfg, fake_bin):
    payload = [
        {
            "severity": "blocking",
            "summary": "retry spins on 429",
            "body": "cap the retry",
            "file": "src/api/retry.py",
        }
    ]
    fake_bin.respond("claude", stdout=json.dumps(payload))
    findings = parse(cfg, body("human-comment.md"))
    assert findings[0]["parsed_by"] == "haiku"
    assert "claude-haiku-4-5-20251001" in fake_bin.calls_to("claude")[0]


def test_script_path_costs_no_model_call(cfg, fake_bin):
    parse(cfg, body("example-review.md"))
    assert fake_bin.calls_to("claude") == []


def test_haiku_returning_junk_raises_rather_than_inventing_findings(cfg, fake_bin):
    fake_bin.respond("claude", stdout="I could not parse that.")
    with pytest.raises(Exception, match="did not return JSON"):
        parse(cfg, body("human-comment.md"))


def test_a_fenced_json_answer_is_accepted(cfg, fake_bin):
    """Models fence JSON routinely; failing on that is a self-inflicted retry."""
    payload = [{"severity": "blocking", "summary": "s", "body": "b", "file": "f.py"}]
    fake_bin.respond("claude", stdout="```json\n" + json.dumps(payload) + "\n```\n")
    assert parse_haiku(cfg, body("human-comment.md"))[0]["severity"] == "blocking"


def test_the_review_body_goes_on_stdin(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps([{"summary": "x"}]))
    parse_haiku(cfg, body("human-comment.md"))
    assert "retry loop" in fake_bin.stdin_to("claude")[0]


def test_haiku_output_is_coerced_to_the_finding_shape(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps([{"summary": "no severity given"}]))
    findings = parse_haiku(cfg, body("human-comment.md"))
    assert findings[0]["severity"] == "maintenance"
    assert findings[0]["file"] is None


CUSTOM = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    severities:
      - id: must-fix
        marker: "[!]"
      - id: consider
        marker: "[?]"
        default: true
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
""")

CUSTOM_REVIEW = textwrap.dedent("""
    <details>
    <summary>[!] Must fix</summary>

    * `src/api/retry.py` — retries are unbounded on 429.

    </details>

    <details>
    <summary>[?] Consider</summary>

    * `README.md` — names the removed `--legacy` flag.

    </details>

    **Verdict:** changes requested.
""")


@pytest.fixture
def custom_cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CUSTOM)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("x\n")
    return load_config(path)


def test_script_keys_off_the_configured_markers(custom_cfg):
    findings = parse_script(custom_cfg, CUSTOM_REVIEW)
    assert [f["severity"] for f in findings] == ["must-fix", "consider"]


def test_script_ignores_a_review_using_another_config_s_markers(custom_cfg):
    """The default 🔴/🟡/🔵 mean nothing to a config that never declared them."""
    assert parse_script(custom_cfg, body("example-review.md")) is None


def test_haiku_prompt_names_the_configured_severities(custom_cfg):
    prompt = haiku_prompt(custom_cfg)
    assert '"must-fix", "consider"' in prompt
    assert "blocking" not in prompt


def test_haiku_severity_outside_the_configured_set_falls_back(custom_cfg, fake_bin):
    fake_bin.respond(
        "claude", stdout=json.dumps([{"summary": "s", "severity": "blocking"}])
    )
    findings = parse_haiku(custom_cfg, body("human-comment.md"))
    assert findings[0]["severity"] == "consider"


# --- #9: the built-in grammar has to be wide enough for a bot we never met ---


def test_details_with_attributes_is_recognised(cfg):
    """`<details open>` is the same block. A literal `<details>` misses it."""
    assert parse_script(cfg, body("wide-review.md")) is not None


def test_titled_paragraphs_parse_as_findings(cfg):
    """A `**Title - summary** (path): text` paragraph is one finding, not zero."""
    findings = parse_script(cfg, body("wide-review.md"))
    assert [f["severity"] for f in findings] == ["maintenance", "maintenance"]


def test_a_trailing_summary_table_is_not_a_finding(cfg):
    findings = parse_script(cfg, body("wide-review.md"))
    assert all("|" not in f["summary"] for f in findings)
    assert len(findings) == 2


def test_a_bullet_and_a_titled_paragraph_both_still_work(cfg):
    assert len(parse_script(cfg, body("example-review.md"))) == 3


def test_recognised_but_empty_is_not_the_same_as_unrecognised(cfg):
    """Every section says None: ours, and genuinely zero findings."""
    result = script_parse(cfg, body("all-clear.md"))
    assert result.recognised is True
    assert result.findings == []
    assert parse_script(cfg, body("all-clear.md")) == []


def test_a_section_we_cannot_split_is_unrecognised(cfg):
    """Markers and a verdict, but prose we have no rule for.

    Hand that to haiku rather than record the source with zero findings.
    """
    assert script_parse(cfg, body("unrecognised-sections.md")).recognised is False
    assert parse_script(cfg, body("unrecognised-sections.md")) is None


def test_the_haiku_fallback_fires_on_an_unrecognised_shape(cfg, fake_bin):
    payload = [{"severity": "blocking", "summary": "backoff is uncapped", "body": "b"}]
    fake_bin.respond("claude", stdout=json.dumps(payload))
    findings = parse(cfg, body("unrecognised-sections.md"))
    assert [f["parsed_by"] for f in findings] == ["haiku"]


def test_an_all_clear_review_costs_no_model_call(cfg, fake_bin):
    assert parse(cfg, body("all-clear.md")) == []
    assert fake_bin.calls_to("claude") == []


PROFILE_CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    parse:
      sources:
        - author: odd-bot[bot]
          details: '<block[^>]*>\\s*<head>(?P<summary>.*?)</head>(?P<body>.*?)</block>'
          bullet: '^\\s{0,3}>>\\s+'
          verdict: '^== end =='
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
""")

ODD_REVIEW = textwrap.dedent("""
    <block>
    <head>🔴 Blocking</head>

    >> `src/api/retry.py` — retries are unbounded on 429.

    </block>

    == end ==
""")


@pytest.fixture
def profile_cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(PROFILE_CONFIG)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("x\n")
    return load_config(path)


def test_a_source_profile_overrides_the_builtin_for_that_author(profile_cfg):
    findings = parse_script(profile_cfg, ODD_REVIEW, author="odd-bot[bot]")
    assert [f["severity"] for f in findings] == ["blocking"]
    assert findings[0]["file"] == "src/api/retry.py"


def test_a_source_profile_applies_to_no_one_else(profile_cfg):
    assert parse_script(profile_cfg, ODD_REVIEW, author="someone-else") is None


def test_the_builtin_still_works_for_an_author_with_a_profile_configured(profile_cfg):
    """The profile is an escape hatch, not a replacement for the built-in."""
    assert len(parse_script(profile_cfg, body("example-review.md"))) == 3


# --- #11: summary is derived, file is a path or nothing, dedupe means something


def test_summary_lifts_the_bolded_lead(cfg):
    findings = parse_script(cfg, body("wide-review.md"))
    assert findings[0]["summary"] == (
        "Foo.kt:43 - the null branch of this guard is permitted"
    )


def test_summary_is_not_the_body(cfg):
    for finding in parse_script(cfg, body("wide-review.md")):
        assert finding["summary"] != finding["body"]
        assert len(finding["summary"]) < len(finding["body"])


def test_body_keeps_the_full_text(cfg):
    findings = parse_script(cfg, body("wide-review.md"))
    assert "nothing in the logs" in findings[0]["body"]


def test_summary_is_capped(cfg):
    long = textwrap.dedent("""
        <details>
        <summary>🟡 Maintenance</summary>

        * the rejection path emits a metric and no log line at all, so there is
          no way to tell which caller triggered it, which matters because three
          separate callers reach it and only one of them is retried by anything
          upstream of this module.

        </details>

        **Verdict:** changes requested.
    """)
    summary = parse_script(cfg, long)[0]["summary"]
    assert len(summary) <= MAX_SUMMARY
    assert "\n" not in summary


def test_summary_cuts_at_the_first_sentence_when_there_is_no_bold_lead(cfg):
    review = textwrap.dedent("""
        <details>
        <summary>🟡 Maintenance</summary>

        * The retry loop never caps its backoff. Three callers reach it.

        </details>

        **Verdict:** changes requested.
    """)
    assert parse_script(cfg, review)[0]["summary"] == (
        "The retry loop never caps its backoff."
    )


def test_a_short_bold_lead_is_extended_rather_than_left_as_a_location(cfg):
    review = textwrap.dedent("""
        <details>
        <summary>🟡 Maintenance</summary>

        **Foo.kt:43**: the null branch of this guard is silently permitted, so
        the case it is meant to catch passes through unrecorded.

        </details>

        **Verdict:** changes requested.
    """)
    summary = parse_script(cfg, review)[0]["summary"]
    assert summary.startswith("Foo.kt:43: the null branch")
    assert len(summary) <= MAX_SUMMARY


def test_file_prefers_the_path_given_in_parens(cfg):
    findings = parse_script(cfg, body("wide-review.md"))
    assert findings[0]["file"] == "src/Foo.kt"
    assert findings[1]["file"] == "src/Bar.kt"


def test_a_code_identifier_is_not_recorded_as_a_file(cfg):
    """`logger.warn` is a symbol. No file beats a wrong file."""
    findings = parse_script(cfg, body("symbol-not-a-path.md"))
    assert findings[0]["file"] is None


def test_a_reworded_repost_dedupes(cfg):
    """The point of deriving a summary: `_fingerprint` stops being full-text equality, so a re-posted finding with one clause reworded is a duplicate."""
    first = textwrap.dedent("""
        <details open>
        <summary>🟡 Maintenance</summary>

        **Foo.kt:43 - the null branch of this guard is permitted** (`src/Foo.kt`):
        so the case it is meant to catch passes through with nothing in the logs.

        </details>

        **Verdict:** changes requested.
    """)
    again = first.replace(
        "so the case it is meant to catch passes through with nothing in the logs.",
        "which means the case it exists to catch is never recorded anywhere.",
    )
    one = parse_script(cfg, first)[0]
    two = parse_script(cfg, again)[0]
    assert one["body"] != two["body"]
    assert _fingerprint(one) == _fingerprint(two)


def test_dedupe_still_separates_two_different_findings(cfg):
    findings = parse_script(cfg, body("wide-review.md"))
    assert _fingerprint(findings[0]) != _fingerprint(findings[1])


# --- review of #9/#11: shapes the widened grammar has to survive ---


def test_a_fenced_diff_inside_a_finding_is_not_three_findings(cfg):
    """Widening bullets to `-` and `+` made every suggested-diff line a finding.

    A fence is part of the finding it sits in, whatever its lines start with.
    """
    findings = parse_script(cfg, body("fenced-diff.md"))
    assert len(findings) == 1
    assert "if (x != null) return;" in findings[0]["body"]
    assert findings[0]["file"] == "src/Foo.kt"


def test_a_section_whose_findings_are_a_table_goes_to_haiku(cfg):
    """Rows are not findings, but a section made of them is not empty either.

    Recording it as ours with zero findings is the silent loss #9 was filed about.
    """
    assert script_parse(cfg, body("findings-as-table.md")).recognised is False
    assert parse_script(cfg, body("findings-as-table.md")) is None


def test_a_lead_paragraph_under_a_prose_line_is_still_a_finding(cfg):
    """Prose before it must not swallow the finding glued underneath."""
    findings = parse_script(cfg, body("prose-then-lead.md"))
    assert len(findings) == 2
    assert findings[0]["file"] == "src/Foo.kt"
    assert findings[1]["file"] == "src/Bar.kt"


def test_a_path_beats_a_bare_filename_quoted_in_the_prose(cfg):
    """`README.md` named in passing must not outrank the file being reported."""
    review = textwrap.dedent("""
        <details>
        <summary>🟡 Maintenance</summary>

        * As `README.md` explains, the handler in `src/api/client.py` has no
          timeout, so a slow upstream holds the worker open indefinitely.

        </details>

        **Verdict:** changes requested.
    """)
    assert parse_script(cfg, review)[0]["file"] == "src/api/client.py"


def test_a_summary_does_not_carry_stray_bold_markers(cfg):
    """An unclosed `**` is markup the reader never asked for, and `summary` prints as a row."""
    review = textwrap.dedent("""
        <details>
        <summary>🟡 Maintenance</summary>

        * **The retry loop never caps its backoff, so three callers spin.

        </details>

        **Verdict:** changes requested.
    """)
    assert parse_script(cfg, review)[0]["summary"] == (
        "The retry loop never caps its backoff, so three callers spin."
    )


def test_a_capped_summary_does_not_end_on_a_dangling_dash():
    """`_cap` strips the hyphen it cuts back to; the en and em dash are the same case."""
    for dash in DASHES:
        text = f"{'word ' * 23}{dash} and then some more text after the break"
        assert _cap(text) == f"{'word ' * 23}".strip() + "…"


def test_a_nested_details_does_not_truncate_the_section(cfg):
    """Bots fold evidence into an inner block; the lazy body used to end on its close tag."""
    findings = parse_script(cfg, body("nested-details.md"))
    assert [f["file"] for f in findings] == ["src/api/retry.py", "src/api/reject.py"]
