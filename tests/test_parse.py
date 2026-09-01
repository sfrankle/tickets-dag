import json
import textwrap
from pathlib import Path

import pytest

from ticket.config import load_config
from ticket.parse import haiku_prompt, parse, parse_haiku, parse_script

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
