import json
import os
import textwrap

import pytest

from ticket.cli import main
from ticket.store import Store

BASE = textwrap.dedent("""\
    models: {opus: claude-opus-5}
    defaults: {model: opus}
    sync: false
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
    repos:
      acme/api:
        path: clone
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "evaluate.md").write_text("Evaluate.\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "clone").mkdir()
    (tmp_path / "config.yml").write_text(BASE)
    monkeypatch.setenv("TICKET_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setenv("TICKET_STORE", str(tmp_path / "store"))
    return tmp_path


@pytest.fixture
def store(env):
    return Store(env / "store")


def script(env, name: str, body: str) -> str:
    """A `run:` script under the config's `scripts/`, returned as the config names it."""
    path = env / "scripts" / f"{name}.sh"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return f"scripts/{name}.sh"


def configure(env, *, queue=(), ticket=()) -> None:
    lines = [BASE.rstrip("\n"), "refresh:"]
    for name, runs in (("queue", queue), ("ticket", ticket)):
        if runs:
            lines.append(f"  {name}:")
            lines += [f"    - run: {run}" for run in runs]
    (env / "config.yml").write_text("\n".join(lines) + "\n")


def track(key: str, repo: str = "acme/api") -> None:
    args = ["track", key] + (["--repo", repo] if repo else [])
    assert main(args) == 0


def log_text(store: Store) -> str:
    return "".join(
        p.read_text() for p in sorted((store.root / "refresh").glob("*.log"))
    )


# --- one ticket -------------------------------------------------------------


def test_ticket_entries_run_in_order_in_the_clone_with_the_ticket_env(
    env, store, capsys
):
    configure(
        env,
        ticket=[
            script(env, "first", f'echo "first $TICKET_KEY $(pwd)" >> {env}/order.txt'),
            script(env, "second", f'echo "second $TICKET_WORKTREE" >> {env}/order.txt'),
        ],
    )
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    clone = os.path.realpath(env / "clone")
    lines = (env / "order.txt").read_text().splitlines()
    assert lines[0].split()[:2] == ["first", "ABC-1"]
    assert os.path.realpath(lines[0].split()[2]) == clone
    assert os.path.realpath(lines[1].split()[1]) == clone
    assert "refreshed ABC-1" in capsys.readouterr().out


def test_every_line_is_prefixed_on_the_terminal_and_in_the_log(env, store, capsys):
    configure(env, ticket=[script(env, "talk", "echo hello; printf 'no newline'")])
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    out = capsys.readouterr().out
    assert "ABC-1 | hello\n" in out
    assert "ABC-1 | no newline\n" in out
    assert "ABC-1 | hello\n" in log_text(store)


def test_a_failing_entry_stops_that_ticket_and_exits_1(env, store, capsys):
    configure(
        env,
        ticket=[
            script(env, "breaks", "exit 3"),
            script(env, "after", f"touch {env}/after-ran"),
        ],
    )
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 1
    assert not (env / "after-ran").exists()
    out = capsys.readouterr().out
    assert "ABC-1 breaks: exit 3" in out
    assert "refreshed ABC-1" not in out


def test_a_script_that_cannot_run_is_exit_127_not_a_traceback(env, store, capsys):
    run = script(env, "gone", "true")
    configure(env, ticket=[run])
    (env / run).unlink()
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 1
    assert "ABC-1 gone: exit 127" in capsys.readouterr().out


def test_a_ticket_with_no_repo_runs_its_entries_in_the_config_directory(env, store):
    configure(env, ticket=[script(env, "where", f"pwd > {env}/where.txt")])
    track("ABC-1", repo="")
    assert main(["refresh", "ABC-1"]) == 0
    where = (env / "where.txt").read_text().strip()
    assert os.path.realpath(where) == os.path.realpath(env)


# --- worktree and PR announcements -----------------------------------------


def test_a_dead_worktree_is_repaired_by_an_announce(env, store):
    real = env / "wt-real"
    real.mkdir()
    configure(
        env,
        ticket=[
            script(
                env,
                "find",
                f'echo "$TICKET_RECORDED_WORKTREE" > {env}/recorded.txt\n'
                f"pwd >> {env}/recorded.txt\n"
                f"echo 'ticket-worktree: {real}'",
            )
        ],
    )
    track("ABC-1")
    ticket = store.read_ticket("ABC-1")
    ticket["worktree"] = str(env / "wt-gone")
    store.write_ticket(ticket)
    assert main(["refresh", "ABC-1"]) == 0
    recorded, ran_in = (env / "recorded.txt").read_text().splitlines()
    assert recorded == str(env / "wt-gone")
    assert os.path.realpath(ran_in) == os.path.realpath(env / "clone")
    assert store.read_ticket("ABC-1")["worktree"] == str(real)


def test_an_announced_worktree_that_does_not_exist_is_not_recorded(env, store, capsys):
    configure(
        env, ticket=[script(env, "find", f"echo 'ticket-worktree: {env}/not-made'")]
    )
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    assert not store.read_ticket("ABC-1").get("worktree")
    assert "does not exist, not recorded" in capsys.readouterr().out


def test_a_new_pr_is_registered_and_made_active(env, store):
    configure(env, ticket=[script(env, "pr", "echo 'ticket-pr: acme/api#7'")])
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    ticket = store.read_ticket("ABC-1")
    assert ticket["prs"] == ["acme/api#7"]
    assert ticket["active"] == "acme/api#7"


def test_a_pr_already_known_does_not_move_the_active_pr(env, store, fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "abc"}))
    configure(env, ticket=[script(env, "pr", "echo 'ticket-pr: acme/api#7'")])
    track("ABC-1")
    ticket = store.read_ticket("ABC-1")
    ticket["prs"] = ["acme/api#7", "acme/api#9"]
    ticket["active"] = "acme/api#9"
    store.write_ticket(ticket)
    assert main(["refresh", "ABC-1"]) == 0
    assert store.read_ticket("ABC-1")["active"] == "acme/api#9"


def test_announce_lines_from_a_failed_entry_are_ignored(env, store):
    configure(env, ticket=[script(env, "pr", "echo 'ticket-pr: acme/api#7'; exit 1")])
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 1
    assert not store.read_ticket("ABC-1").get("prs")


# --- built-in part and dry run ---------------------------------------------


def test_a_gh_error_in_the_builtin_part_fails_the_ticket_and_skips_its_entries(
    env, store, fake_bin, capsys, monkeypatch
):
    monkeypatch.setattr("ticket.gh.RETRY_BACKOFF", (0, 0))
    fake_bin.respond("gh pr view", exit_code=1, stderr="gh down")
    # `: >` rather than `touch`: `fake_bin` leaves only the fakes on PATH, so `touch` would never create the file and the test could not fail.
    configure(env, ticket=[script(env, "after", f": > {env}/after-ran")])
    track("ABC-1")
    ticket = store.read_ticket("ABC-1")
    ticket["prs"] = ["acme/api#7"]
    store.write_ticket(ticket)
    assert main(["refresh", "ABC-1"]) == 1
    assert not (env / "after-ran").exists()
    assert "ABC-1 built-in:" in capsys.readouterr().out


def test_a_refresh_that_changes_nothing_does_not_restamp_the_ticket(env, store):
    """The queue sorts on `updated`, so a no-op refresh must not reorder it."""
    configure(env, ticket=[script(env, "quiet", "exit 0")])
    track("ABC-1")
    ticket = store.read_ticket("ABC-1")
    ticket["updated"] = "2000-01-01T00:00:00Z"
    store._write(store._ticket_file("ABC-1"), ticket)  # bypass the stamp
    assert main(["refresh", "ABC-1"]) == 0
    assert store.read_ticket("ABC-1")["updated"] == "2000-01-01T00:00:00Z"


def test_dry_run_runs_no_entry_and_writes_no_log(env, store, capsys):
    configure(env, ticket=[script(env, "touch", f"touch {env}/ran")])
    track("ABC-1")
    assert main(["refresh", "ABC-1", "--dry-run"]) == 0
    assert not (env / "ran").exists()
    assert not (store.root / "refresh").exists()
    assert "[dry-run] would run scripts/touch.sh" in capsys.readouterr().out
