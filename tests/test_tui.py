import textwrap

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


def _track(key: str) -> None:
    assert main(["track", key, "--repo", "acme/api"]) == 0


def test_tui_plain_renders_ticket_list_and_stage_pipeline(
    tmp_path, monkeypatch, capsys
):
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

    _track("ABC-123")
    capsys.readouterr()

    assert main(["tui", "--plain"]) == 0
    out = capsys.readouterr().out
    assert "ticket tui" in out
    assert "TICKETS" in out
    assert "DETAILS" in out
    assert "Issue Key: ABC-123" in out
    assert "1. evaluate (handoff)" in out
    assert "2. review-spec (gate)" in out
    assert "3. draft-pr (script)" in out
    assert "4. docs-tests (review)" in out


def test_tui_ascii_snapshot_uses_ascii_markers(tmp_path, monkeypatch, capsys):
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

    _track("ABC-123")
    capsys.readouterr()

    assert main(["tui", "--plain", "--ascii"]) == 0
    out = capsys.readouterr().out
    assert "[>]" in out
    assert "[▶]" not in out
    assert "[✓]" not in out


def test_tui_filter_ready_hides_completed_rows(tmp_path, monkeypatch, capsys):
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

    _track("ABC-123")
    _track("ABC-124")
    store = Store(tmp_path / "store")
    done = store.read_ticket("ABC-123")
    done["steps"] = {
        "evaluate": {"status": "done"},
        "review-spec": {"status": "released"},
        "draft-pr": {"status": "done"},
    }
    store.write_ticket(done)
    capsys.readouterr()

    assert main(["tui", "--plain", "--filter", "ready"]) == 0
    out = capsys.readouterr().out
    assert "ABC-124" in out
    assert "ABC-123" not in out
