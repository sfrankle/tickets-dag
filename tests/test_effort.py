import json
import textwrap

import pytest

from ticket.effort import assign_effort
from ticket.config import load_config

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


def findings():
    return [
        {"summary": "README names a removed flag", "body": "...", "severity": "maintenance"},
        {"summary": "retry loop is unbounded", "body": "...", "severity": "blocking"},
    ]


def test_effort_is_set_from_the_model(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard"]))
    result = assign_effort(cfg, findings())
    assert [f["effort"] for f in result] == ["easy", "hard"]


def test_effort_uses_haiku(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard"]))
    assign_effort(cfg, findings())
    assert "claude-haiku-4-5-20251001" in fake_bin.calls_to("claude")[0]


def test_one_call_for_the_batch(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard"]))
    assign_effort(cfg, findings())
    assert len(fake_bin.calls_to("claude")) == 1


def test_severity_is_not_sent_to_the_model(cfg, fake_bin):
    """Severity is importance, not effort. It must not influence the estimate."""
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard"]))
    assign_effort(cfg, findings())
    prompt = fake_bin.stdin_to("claude")[0]
    assert "blocking" not in prompt
    assert "maintenance" not in prompt


def test_a_fenced_answer_is_accepted(cfg, fake_bin):
    fake_bin.respond("claude", stdout='```json\n["easy", "hard"]\n```')
    assert [f["effort"] for f in assign_effort(cfg, findings())] == ["easy", "hard"]


def test_an_empty_batch_makes_no_call(cfg, fake_bin):
    assert assign_effort(cfg, []) == []
    assert fake_bin.calls_to("claude") == []


def test_a_model_failure_leaves_effort_null(cfg, fake_bin):
    fake_bin.respond("claude", exit_code=1, stderr="unavailable")
    result = assign_effort(cfg, findings())
    assert [f["effort"] for f in result] == [None, None]


def test_junk_output_leaves_effort_null(cfg, fake_bin):
    fake_bin.respond("claude", stdout="not json")
    assert [f["effort"] for f in assign_effort(cfg, findings())] == [None, None]


def test_a_wrong_length_answer_leaves_effort_null(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps(["easy"]))
    assert [f["effort"] for f in assign_effort(cfg, findings())] == [None, None]


def test_an_unrecognised_answer_becomes_null(cfg, fake_bin):
    fake_bin.respond("claude", stdout=json.dumps(["easy", "medium"]))
    assert [f["effort"] for f in assign_effort(cfg, findings())] == ["easy", None]
