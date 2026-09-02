import json
import subprocess
import textwrap

import pytest

from ticket.config import load_config
from ticket.errors import TicketError
from ticket.fix import (
    decide,
    finding_env,
    finding_ref,
    fix_one,
    resolve_from_git,
    scan_trailers,
    set_effort,
    trailer,
    wait_for_head,
)

REF = finding_ref("acme/api#115", "f01")

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: draft-pr
        run: scripts/draft-pr.sh
    reviews:
      - id: docs-tests
        order: 1
        dispatch: bot
        prompt: prompts/reviews/docs-tests.md
    fix:
      easy:
        run: scripts/fix-easy.sh
""")

FIXER = """#!/bin/sh
echo "fixer saw $TICKET_FINDING_ID / $TICKET_FINDING_TRAILER"
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    prompts = tmp_path / "prompts" / "reviews"
    prompts.mkdir(parents=True)
    (prompts / "docs-tests.md").write_text("Check docs and tests.\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fixer = scripts / "fix-easy.sh"
    fixer.write_text(FIXER)
    fixer.chmod(0o755)
    return load_config(path)


@pytest.fixture
def worktree(tmp_path):
    """A real git repo — trailer scanning is the one place we need real git."""
    path = tmp_path / "checkout"
    path.mkdir()

    def run(*a):
        return subprocess.run(a, cwd=path, check=True, capture_output=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (path / "README.md").write_text("x\n")
    run("git", "add", "README.md")
    run("git", "commit", "-q", "-m", "initial")
    return path


def ticket_doc(worktree=None):
    doc = {"key": "ABC-123", "repo": "acme/api", "prs": ["acme/api#115"], "steps": {}}
    if worktree:
        doc["worktree"] = str(worktree)
    return doc


def seed(store, *findings):
    store.write_pr(
        {
            "pr": "acme/api#115",
            "key": "ABC-123",
            "head": "aaa",
            "dispatched": [],
            "collected": [],
            "skipped": [],
        }
    )
    store.add_findings("acme/api#115", list(findings))


def test_trailer_shape():
    assert trailer("9f3c1a7d2b04") == "Finding: 9f3c1a7d2b04"


def test_a_ref_is_scoped_to_its_pr():
    """Ids restart at f01 on every PR, so the id alone cannot be the trailer: a scan of the branch would match an unrelated PR's commit."""
    assert finding_ref("acme/api#115", "f01") != finding_ref("acme/api#116", "f01")
    assert finding_ref("acme/api#115", "f01") == finding_ref("acme/api#115", "f01")


def test_a_ref_does_not_carry_the_store_local_id():
    assert "f01" not in finding_ref("acme/api#115", "f01")


def test_scan_trailers_finds_a_commit(worktree):
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fix: wording",
            "-m",
            "Finding: f07",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    found = scan_trailers(worktree)
    assert "f07" in found
    assert len(found["f07"]) == 40


def test_scan_trailers_ignores_commits_without_one(worktree):
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "chore: nothing"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert scan_trailers(worktree) == {}


def test_scan_trailers_newest_commit_wins(worktree):
    """When two commits carry the same finding trailer, `git log`'s
    combined-revision ordering means the newest one wins."""
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fix: first attempt",
            "-m",
            "Finding: f01",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    older = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fix: better attempt",
            "-m",
            "Finding: f01",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    newer = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    found = scan_trailers(worktree)
    assert found["f01"] == newer
    assert found["f01"] != older


def test_resolve_from_git_marks_findings_resolved(cfg, store, worktree):
    seed(store, {"summary": "a", "effort": "easy"})
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "fix: a", "-m", trailer(REF)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    closed = resolve_from_git(cfg, store, ticket_doc(worktree), "acme/api#115")
    assert closed == ["f01"]
    finding = store.read_findings("acme/api#115")["findings"][0]
    assert finding["status"] == "resolved"
    assert len(finding["commit"]) == 40


def test_a_trailer_from_another_pr_does_not_close_a_finding(cfg, store, worktree):
    """Issue #13, run 1: ids restart at f01 on every PR, and the scan reads the whole branch.
    An older PR's `Finding: f01` closed the new PR's f01 on sight, which is why `fix` reported `f01: resolved` for work nobody had done."""
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fix: an older PR's f01",
            "-m",
            "Finding: f01",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    seed(store, {"summary": "a", "effort": "easy"})
    assert resolve_from_git(cfg, store, ticket_doc(worktree), "acme/api#115") == []
    assert store.read_findings("acme/api#115")["findings"][0]["status"] == "open"


def test_resolve_from_git_costs_no_model_call(cfg, store, worktree, fake_bin):
    seed(store, {"summary": "a", "effort": "easy"})
    resolve_from_git(cfg, store, ticket_doc(worktree), "acme/api#115")
    assert fake_bin.calls_to("claude") == []


def test_wait_for_head_returns_when_the_sha_moves(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "bbb"}))
    assert wait_for_head("acme/api#115", "aaa", poll=lambda _s: None) == "bbb"


def test_wait_for_head_gives_up_after_its_attempts(fake_bin):
    fake_bin.respond("gh pr view", stdout=json.dumps({"headRefOid": "aaa"}))
    with pytest.raises(TicketError, match="head did not move"):
        wait_for_head("acme/api#115", "aaa", attempts=2, poll=lambda _s: None)


def easy_finding():
    return {
        "summary": "README names a removed flag",
        "body": "the install section still names --legacy",
        "effort": "easy",
        "file": "README.md",
    }


def test_an_easy_finding_goes_to_the_configured_script(
    cfg, store, worktree, fake_bin, capsys
):
    """The engine posts nothing itself: what fixes an easy finding is a script the config names, so another shop's protocol is another shop's file."""
    seed(store, easy_finding())
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    out = capsys.readouterr().out
    assert f"fixer saw f01 / {trailer(REF)}" in out
    assert [c for c in fake_bin.calls_to("gh") if c[1:3] == ["pr", "comment"]] == []
    assert fake_bin.calls_to("claude") == []


def test_an_easy_finding_with_no_script_configured_is_refused(
    tmp_path, store, worktree, fake_bin
):
    path = tmp_path / "no-fixer.yml"
    path.write_text(CONFIG.replace("  easy:\n    run: scripts/fix-easy.sh\n", ""))
    seed(store, easy_finding())
    with pytest.raises(TicketError, match="fix:"):
        fix_one(load_config(path), store, ticket_doc(worktree), "acme/api#115", "f01")
    assert fake_bin.calls == []


def test_the_script_gets_the_finding_in_its_environment(cfg, store, worktree):
    """The id travels here, privately, and not in whatever the script posts."""
    env = finding_env(
        cfg, ticket_doc(worktree), "acme/api#115", {"id": "f01", **easy_finding()}
    )
    assert env["TICKET_FINDING_ID"] == "f01"
    assert env["TICKET_FINDING_REF"] == REF
    assert env["TICKET_FINDING_TRAILER"] == trailer(REF)
    assert env["TICKET_FINDING_SUMMARY"] == "README names a removed flag"
    assert env["TICKET_FINDING_FILE"] == "README.md"
    assert env["TICKET_PR"] == "acme/api#115"
    assert env["TICKET_KEY"] == "ABC-123"


def test_a_failing_fix_script_is_an_error_not_a_silent_pass(
    tmp_path, store, worktree, fake_bin
):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    (tmp_path / "prompts" / "reviews").mkdir(parents=True)
    (tmp_path / "prompts" / "reviews" / "docs-tests.md").write_text("x\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fixer = scripts / "fix-easy.sh"
    fixer.write_text("#!/bin/sh\necho 'gh: not authenticated' >&2\nexit 4\n")
    fixer.chmod(0o755)
    seed(store, easy_finding())
    with pytest.raises(TicketError, match="exited 4"):
        fix_one(load_config(path), store, ticket_doc(worktree), "acme/api#115", "f01")


def test_a_finding_already_with_the_fixer_is_not_sent_again(
    cfg, store, worktree, fake_bin, capsys
):
    """Issue #13: a second handoff while the first is in flight is how a fixer that runs one action per PR ends up dropping both."""
    seed(store, easy_finding())
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    with pytest.raises(TicketError, match="handed to the easy fixer"):
        fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    assert capsys.readouterr().out.count("fixer saw f01") == 1


def test_force_sends_a_finding_to_the_fixer_again(cfg, store, worktree, capsys):
    seed(store, easy_finding())
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01", force=True)
    assert capsys.readouterr().out.count("fixer saw f01") == 2


def test_hard_finding_runs_a_local_session_and_commits(cfg, store, worktree, fake_bin):
    seed(
        store,
        {
            "summary": "retry loop unbounded",
            "effort": "hard",
            "file": "src/api/retry.py",
        },
    )
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    assert fake_bin.calls_to("claude")
    commit = next(c for c in fake_bin.calls_to("git") if c[1] == "commit")
    assert trailer(REF) in " ".join(commit)
    assert "f01" not in " ".join(commit)


def test_a_hard_finding_uses_the_default_model(cfg, store, worktree, fake_bin):
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    assert "claude-opus-5" in fake_bin.calls_to("claude")[0]


def test_a_hard_fix_passes_the_fix_blocks_args(
    cfg, store, worktree, fake_bin, tmp_path
):
    """Without agent mode the session cannot write, so every hard fix would end
    in "changed nothing" — see `fix:` in examples/config.yml."""
    path = tmp_path / "with-fix.yml"
    path.write_text(
        CONFIG
        + "  hard:\n    model: haiku\n"
        + "    args: [--permission-mode, acceptEdits]\n"
    )
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fix_one(load_config(path), store, ticket_doc(worktree), "acme/api#115", "f01")
    argv = fake_bin.calls_to("claude")[0]
    assert "claude-haiku-4-5-20251001" in argv
    assert argv[-2:] == ["--permission-mode", "acceptEdits"]


def test_a_hard_fix_refuses_a_dirty_tree(cfg, store, worktree, fake_bin):
    """One commit per finding: `git add -A` would sweep in unrelated work."""
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fake_bin.respond("git status --porcelain", stdout=" M README.md\n")
    with pytest.raises(TicketError, match="uncommitted changes"):
        fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")


def test_a_hard_fix_that_changes_nothing_is_reported_not_crashed(
    cfg, store, worktree, fake_bin
):
    """An empty `git commit` exits non-zero. That is an answer, not a crash."""
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fake_bin.respond(
        "git commit", exit_code=1, stderr="nothing to commit, working tree clean"
    )
    assert fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01") == "open"


def test_a_hard_fix_syncs_before_running_the_session(cfg, store, worktree, fake_bin):
    """Decision #22: gh.sync runs before every command that reads a checkout.
    A stale worktree must not feed a hard-fix session outdated code."""
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    fetch_index = next(
        i for i, c in enumerate(fake_bin.calls) if c[0] == "git" and c[1:2] == ["fetch"]
    )
    claude_index = next(i for i, c in enumerate(fake_bin.calls) if c[0] == "claude")
    assert fetch_index < claude_index


def test_a_hard_fix_dry_run_still_syncs(cfg, store, worktree, fake_bin):
    """--no-sync is sync's opt-out, deliberately separate from --dry-run
    (decision #22) -- a dry run must not post/write but should still fetch."""
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01", dry_run=True)
    assert [c for c in fake_bin.calls if c[0] == "git" and c[1:2] == ["fetch"]] != []
    assert fake_bin.calls_to("claude") == []


def test_resolution_fetches_before_scanning(cfg, store, worktree, fake_bin):
    """The bot commits on the remote; a stale checkout closes nothing."""
    seed(store, {"summary": "a", "effort": "easy"})
    resolve_from_git(cfg, store, ticket_doc(worktree), "acme/api#115")
    assert any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def test_the_fix_prompt_goes_on_stdin(cfg, store, worktree, fake_bin):
    seed(store, {"summary": "retry loop unbounded", "effort": "hard"})
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    assert "retry loop unbounded" in fake_bin.stdin_to("claude")[0]


def test_a_finding_with_no_effort_is_refused(cfg, store, worktree, fake_bin):
    seed(store, {"summary": "unknown effort", "effort": None})
    with pytest.raises(TicketError, match="ticket effort"):
        fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")
    assert fake_bin.calls == []


def test_an_already_closed_finding_is_refused(cfg, store, worktree, fake_bin):
    seed(store, {"summary": "a", "effort": "easy"})
    decide(store, "acme/api#115", "f01", "covered by ABC-140")
    with pytest.raises(TicketError, match="wontfix"):
        fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01")


def test_decide_closes_a_finding_with_a_reason(cfg, store):
    seed(store, {"summary": "a", "effort": "easy"})
    decide(store, "acme/api#115", "f01", "covered by ABC-140")
    finding = store.read_findings("acme/api#115")["findings"][0]
    assert finding["status"] == "wontfix"
    assert finding["reason"] == "covered by ABC-140"


def test_set_effort_overrides(cfg, store):
    seed(store, {"summary": "a", "effort": None})
    set_effort(store, "acme/api#115", "f01", "hard")
    assert store.read_findings("acme/api#115")["findings"][0]["effort"] == "hard"


def test_set_effort_rejects_an_unknown_value(cfg, store):
    seed(store, {"summary": "a", "effort": None})
    with pytest.raises(TicketError, match="easy"):
        set_effort(store, "acme/api#115", "f01", "medium")


def test_dry_run_runs_no_fixer_and_changes_nothing(cfg, store, worktree, capsys):
    seed(store, easy_finding())
    fix_one(cfg, store, ticket_doc(worktree), "acme/api#115", "f01", dry_run=True)
    out = capsys.readouterr().out
    assert "[dry-run] would run" in out
    assert "fixer saw" not in out
    finding = store.read_findings("acme/api#115")["findings"][0]
    assert finding["status"] == "open"
    assert "sent" not in finding


def test_a_hard_fix_can_use_a_prompt_of_the_sites_own(
    store, worktree, fake_bin, tmp_path
):
    """`hard` is a local session, so the prompt is the site's to replace."""
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "fix.md").write_text(
        "House style. Fix {summary} in {file}. Nothing else.\n"
    )
    path = tmp_path / "with-prompt.yml"
    path.write_text(CONFIG + "  hard:\n    prompt: prompts/fix.md\n")
    seed(store, {"summary": "retry loop unbounded", "effort": "hard", "file": "r.py"})
    fix_one(load_config(path), store, ticket_doc(worktree), "acme/api#115", "f01")
    assert (
        "House style. Fix retry loop unbounded in r.py."
        in fake_bin.stdin_to("claude")[0]
    )
