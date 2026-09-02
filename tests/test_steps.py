import os
import textwrap

import pytest

from ticket.config import load_config
from ticket.steps import release_gate, run_step, tee

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
      - id: describe
        model: haiku
        prompt: prompts/describe.md
        needs: [draft-pr]
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate the ticket.\n")
    (tmp_path / "prompts" / "describe.md").write_text("Describe the PR.\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    return load_config(path)


def ticket_doc():
    return {
        "key": "ABC-123",
        "repo": "acme/api",
        "prs": [],
        "steps": {},
        "tracked": True,
    }


def write_script(cfg, name, body):
    path = cfg.root / "scripts" / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_script_step_success_is_recorded_done(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo working\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "done"
    assert ticket["steps"]["draft-pr"]["status"] == "done"
    assert store.read_ticket("ABC-123")["steps"]["draft-pr"]["status"] == "done"


def test_script_step_failure_records_exit_code_and_log(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo nope >&2\nexit 3\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "failed"
    assert ticket["steps"]["draft-pr"]["exit_code"] == 3
    assert "nope" in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_script_step_receives_ticket_env(cfg, store):
    write_script(cfg, "draft-pr.sh", 'echo "$TICKET_KEY $TICKET_REPO"\n')
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert "ABC-123 acme/api" in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_a_printed_pr_ref_is_registered_on_the_ticket(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.pr == "acme/api#115"
    assert ticket["prs"] == ["acme/api#115"]


def test_a_repeated_pr_ref_is_not_added_twice(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["prs"] == ["acme/api#115"]


def test_handoff_step_invokes_claude_with_the_resolved_model(cfg, store, fake_bin):
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("describe"))
    assert "claude-haiku-4-5-20251001" in fake_bin.calls_to("claude")[0]


def test_the_prompt_goes_on_stdin_not_in_argv(cfg, store, fake_bin):
    """Decision #21: a long review body would blow the argv size limit."""
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("describe"))
    assert "Describe the PR." in fake_bin.stdin_to("claude")[0]
    assert "Describe the PR." not in " ".join(fake_bin.calls_to("claude")[0])


def test_handoff_args_are_passed_through(cfg, store, fake_bin, tmp_path):
    """`args:` is where agent mode lives, so `implement` can actually write."""
    config = tmp_path / "config.yml"
    config.write_text(
        CONFIG.replace(
            "  - id: describe\n    model: haiku\n",
            "  - id: describe\n    model: haiku\n    args: [--permission-mode, acceptEdits]\n",
        )
    )
    from ticket.config import load_config as reload

    scoped = reload(config)
    run_step(scoped, store, ticket_doc(), scoped.step("describe"))
    assert "--permission-mode" in fake_bin.calls_to("claude")[0]


def test_a_step_runs_in_the_worktree_once_one_is_registered(cfg, store):
    write_script(cfg, "draft-pr.sh", "pwd\n")
    checkout = cfg.root / "checkout"
    checkout.mkdir()
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert str(checkout) in store.read_log(ticket["steps"]["draft-pr"]["log"])


def test_a_step_gets_the_worktree_and_branch_in_its_env(cfg, store):
    write_script(
        cfg,
        "draft-pr.sh",
        'echo "$TICKET_WORKTREE|$TICKET_BRANCH|$TICKET_USE_WORKTREES"\n',
    )
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    logged = store.read_log(ticket["steps"]["draft-pr"]["log"])
    assert "ABC-123|1" in logged


def test_an_announced_worktree_is_recorded_on_the_ticket(cfg, store):
    write_script(cfg, "draft-pr.sh", "echo 'ticket-worktree: /tmp/checkout-abc'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["worktree"] == "/tmp/checkout-abc"
    assert ticket["steps"]["draft-pr"]["registered_worktree"] == "/tmp/checkout-abc"


def test_a_registered_pr_is_recorded_against_the_step_that_made_it(cfg, store):
    """`ticket reset` undoes registrations without knowing any step id."""
    write_script(cfg, "draft-pr.sh", "echo 'ticket-pr: acme/api#115'\n")
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert ticket["steps"]["draft-pr"]["registered_pr"] == "acme/api#115"


def test_a_step_fetches_before_it_runs(cfg, store, fake_bin):
    write_script(cfg, "draft-pr.sh", "echo hi\n")
    checkout = cfg.root / "checkout"
    (checkout / ".git").mkdir(parents=True)
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_handoff_step_defaults_to_the_default_model(cfg, store, fake_bin):
    ticket = ticket_doc()
    run_step(cfg, store, ticket, cfg.step("evaluate"))
    assert "claude-opus-5" in fake_bin.calls_to("claude")[0]


def test_handoff_failure_is_recorded(cfg, store, fake_bin):
    fake_bin.respond("claude", exit_code=1, stderr="model unavailable")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("evaluate"))
    assert result.status == "failed"


def test_gate_step_parks_without_running_anything(cfg, store, fake_bin):
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("review-spec"))
    assert result.status == "parked"
    assert "review-spec" not in ticket["steps"]
    assert fake_bin.calls == []


def test_release_gate_records_released(cfg, store):
    ticket = ticket_doc()
    release_gate(store, ticket, "review-spec")
    assert ticket["steps"]["review-spec"]["status"] == "released"
    assert store.read_ticket("ABC-123")["steps"]["review-spec"]["status"] == "released"


def test_dry_run_executes_nothing_and_records_nothing(cfg, store, fake_bin):
    write_script(cfg, "draft-pr.sh", "echo working\n")
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"), dry_run=True)
    assert result.status == "dry-run"
    assert ticket["steps"] == {}
    assert store.read_ticket("ABC-123") is None


def test_a_missing_script_is_a_failure_not_a_crash(cfg, store):
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "failed"


def test_tee_writes_each_line_as_it_arrives(cfg, tmp_path):
    """A step that is killed mid-run must still leave what it printed on disk.

    The script blocks until its own first line shows up in the log file, so it
    can only finish if `tee` wrote that line before the process exited.
    """
    log = tmp_path / "run.log"
    script = write_script(
        cfg,
        "streaming.sh",
        "echo first\n"
        "i=0\n"
        f"while [ $i -lt 100 ]; do\n"
        f'  if grep -q first "{log}" 2>/dev/null; then echo second; exit 0; fi\n'
        "  sleep 0.05\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 1\n",
    )
    output, exit_code = tee(
        [str(script)], cwd=tmp_path, env=dict(os.environ), stdin_text=None, log=log
    )
    assert exit_code == 0, "tee held the first line until the process exited"
    assert output == "first\nsecond\n"
    assert log.read_text() == "first\nsecond\n"


def test_a_steps_log_exists_before_the_step_finishes(cfg, store, tmp_path):
    """Same guarantee through `run_step`: the file it names is being written
    while the step runs, not once it is over."""
    logs = store.ticket_dir("ABC-123") / "logs"
    write_script(
        cfg,
        "draft-pr.sh",
        "echo first\n"
        "i=0\n"
        f"while [ $i -lt 100 ]; do\n"
        f'  if grep -rq first "{logs}" 2>/dev/null; then echo second; exit 0; fi\n'
        "  sleep 0.05\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 1\n",
    )
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "done"
    assert (store.root / ticket["steps"]["draft-pr"]["log"]).read_text() == (
        "first\nsecond\n"
    )


def test_a_step_that_cannot_be_executed_still_writes_its_log(cfg, store):
    """The OSError path never reaches `tee`, so it has to write the log itself."""
    ticket = ticket_doc()
    result = run_step(cfg, store, ticket, cfg.step("draft-pr"))
    assert result.status == "failed"
    log = store.root / ticket["steps"]["draft-pr"]["log"]
    assert "could not execute" in log.read_text()
