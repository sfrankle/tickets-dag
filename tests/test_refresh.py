import json
import os
import sys
import textwrap

import pytest

from tests.conftest import dead_pid, write_lock
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


def configure(env, *, queue=(), ticket=(), tracker=()) -> None:
    lines = [BASE.rstrip("\n")]
    if tracker:
        lines.append("tracker:")
        lines.append(f"  summary: {json.dumps(list(tracker))}")
    lines.append("refresh:")
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


def test_a_relative_announced_worktree_resolves_against_the_entrys_cwd(env, store):
    (env / "clone" / "sub").mkdir()
    configure(env, ticket=[script(env, "find", "echo 'ticket-worktree: sub'")])
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    recorded = store.read_ticket("ABC-1")["worktree"]
    assert recorded == str((env / "clone" / "sub").resolve())


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


def test_a_tracker_outage_warns_instead_of_skipping_the_entries(
    env, store, fake_tracker, capsys, monkeypatch
):
    monkeypatch.setattr("ticket.gh.RETRY_BACKOFF", (0, 0))
    fake_tracker(exit_code=1)
    configure(
        env,
        tracker=["faketracker", "issue", "view", "{key}"],
        ticket=[script(env, "after", f": > {env}/after-ran")],
    )
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    assert (env / "after-ran").exists()
    assert "warning: tracker summary" in capsys.readouterr().out


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


# --- every ticket -----------------------------------------------------------


def test_queue_runs_once_before_any_ticket(env, store):
    configure(
        env,
        queue=[
            script(
                env, "bulk", f'echo "queue ${{TICKET_KEY:-none}}" >> {env}/order.txt'
            )
        ],
        ticket=[script(env, "each", f'echo "ticket $TICKET_KEY" >> {env}/order.txt')],
    )
    track("ABC-1")
    track("ABC-2")
    assert main(["refresh"]) == 0
    lines = (env / "order.txt").read_text().splitlines()
    assert lines[0] == "queue none"
    assert sorted(lines[1:]) == ["ticket ABC-1", "ticket ABC-2"]


def test_refresh_with_a_key_skips_the_queue(env, store):
    configure(env, queue=[script(env, "bulk", f"touch {env}/queue-ran")])
    track("ABC-1")
    assert main(["refresh", "ABC-1"]) == 0
    assert not (env / "queue-ran").exists()


def test_a_failing_queue_entry_is_listed_and_the_tickets_still_run(env, store, capsys):
    configure(
        env,
        queue=[script(env, "bulk", "exit 2")],
        ticket=[script(env, "each", f"touch {env}/ticket-ran")],
    )
    track("ABC-1")
    assert main(["refresh"]) == 1
    assert (env / "ticket-ran").exists()
    assert "queue bulk: exit 2" in capsys.readouterr().out


def test_one_ticket_failing_does_not_stop_the_next(env, store, capsys):
    configure(
        env,
        ticket=[
            script(
                env,
                "each",
                f'[ "$TICKET_KEY" = ABC-1 ] && exit 4\ntouch {env}/$TICKET_KEY',
            )
        ],
    )
    track("ABC-1")
    track("ABC-2")
    assert main(["refresh"]) == 1
    assert (env / "ABC-2").exists()
    out = capsys.readouterr().out
    assert "failed | ABC-1 each: exit 4" in out
    assert "refreshed every tracked row" not in out


def test_a_ticket_locked_by_a_live_run_is_skipped_and_the_run_still_succeeds(
    env, store, capsys
):
    configure(env, ticket=[script(env, "each", f"touch {env}/$TICKET_KEY")])
    track("ABC-1")
    track("ABC-2")
    write_lock(store, f"{os.getpid()}\n", key="ABC-1")
    assert main(["refresh"]) == 0
    assert not (env / "ABC-1").exists()
    assert (env / "ABC-2").exists()
    assert "skipped | ABC-1: locked by pid" in capsys.readouterr().out


def test_a_stale_lock_fails_that_ticket_and_names_the_fix(env, store, capsys):
    configure(env, ticket=[script(env, "each", f"touch {env}/$TICKET_KEY")])
    track("ABC-1")
    track("ABC-2")
    write_lock(store, f"{dead_pid()}\n", key="ABC-1")
    assert main(["refresh"]) == 1
    assert (env / "ABC-2").exists()
    assert "ticket unlock ABC-1" in capsys.readouterr().out


def test_each_ticket_is_locked_as_refresh_while_its_entries_run(env, store):
    configure(
        env,
        ticket=[
            script(
                env, "peek", f"cat {env}/store/locks/$TICKET_KEY.lock > {env}/lock.txt"
            )
        ],
    )
    track("ABC-1")
    assert main(["refresh"]) == 0
    assert (env / "lock.txt").read_text().splitlines()[1] == "refresh"
    assert not store.lock_path("ABC-1").exists()


def test_a_step_written_during_the_run_is_not_overwritten(env, store):
    """Each ticket's entry writes the *other* ticket's state after the run listed them.

    Whichever ticket refreshes second must keep the edit, so this holds whatever order `list_tickets` returns — it sorts by `updated`, which depends on whether the two `track` calls land in the same second.
    """
    edit = (
        'if [ "$TICKET_KEY" = ABC-1 ]; then other=ABC-2; else other=ABC-1; fi\n'
        f'{sys.executable} -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); '
        f"d['steps']={{'evaluate':{{'status':'done'}}}}; json.dump(d,open(p,'w'))\" "
        f"{env}/store/tickets/$other/state.json\n"
        # A change of its own, so the ticket is written back: refresh writes only a ticket that changed, and a stale copy that is never written cannot clobber anything.
        'echo "ticket-pr: acme/api#${TICKET_KEY#ABC-}"'
    )
    configure(env, ticket=[script(env, "edit", edit)])
    track("ABC-1")
    track("ABC-2")
    assert main(["refresh"]) == 0
    # The ticket refreshed second is the one a stale copy would clobber; asserting both covers either order.
    for key in ("ABC-1", "ABC-2"):
        ticket = store.read_ticket(key)
        assert ticket["steps"]["evaluate"]["status"] == "done", key
        assert ticket["prs"], key


def test_ctrl_c_mid_run_releases_the_lock_and_logs_what_already_failed(
    env, store, monkeypatch
):
    from ticket import refresh as refresh_module

    configure(env, ticket=[script(env, "each", "exit 5")])
    track("ABC-1")
    track("ABC-2")
    real = refresh_module.refresh_ticket
    calls = []

    def interrupt_second(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(refresh_module, "refresh_ticket", interrupt_second)
    assert main(["refresh"]) == 130
    assert f"{calls[0]} each: exit 5" in log_text(store)
    assert not store.lock_path(calls[1]).exists()


def test_dry_run_with_no_key_runs_nothing_and_takes_no_lock(env, store, capsys):
    configure(
        env,
        queue=[script(env, "bulk", f"touch {env}/ran")],
        ticket=[script(env, "each", f"touch {env}/ran")],
    )
    track("ABC-1")
    assert main(["refresh", "--dry-run"]) == 0
    assert not (env / "ran").exists()
    out = capsys.readouterr().out
    assert "queue | [dry-run] would run scripts/bulk.sh" in out
    assert "ABC-1 | [dry-run] would run scripts/each.sh" in out


def test_a_repo_the_config_does_not_know_fails_that_ticket_not_the_run(
    env, store, capsys
):
    """#42 lands as that ticket's failure, per the #8 spec, and the next ticket still refreshes."""
    configure(env, ticket=[script(env, "touch", f'touch "{env}/ran-$TICKET_KEY"')])
    track("ABC-1", repo="someone/else")
    track("ABC-2")
    assert main(["refresh"]) == 1
    out = capsys.readouterr().out
    assert "ABC-1 touch: " in out
    assert "'someone/else'" in out
    assert not (env / "ran-ABC-1").exists()
    assert (env / "ran-ABC-2").exists()
