import textwrap

import pytest

from ticket.config import load_config
from ticket.resolve import next_action

CONFIG = textwrap.dedent("""
    models: {opus: claude-opus-5, haiku: claude-haiku-4-5-20251001}
    defaults: {model: opus}
    steps:
      - id: evaluate
        prompt: prompts/evaluate.md
      - id: spec
        prompt: prompts/spec.md
        needs: [evaluate]
      - id: review-spec
        gate: true
        needs: [spec]
      - id: draft-pr
        run: scripts/draft-pr.sh
        needs: [review-spec]
      - id: describe
        prompt: prompts/describe.md
        model: haiku
        needs: [draft-pr]
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
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    return load_config(path)


def ticket_doc(steps=None, prs=None):
    return {
        "key": "ABC-123",
        "repo": "acme/api",
        "prs": prs if prs is not None else [],
        "steps": steps or {},
        "tracked": True,
    }


def done(*step_ids):
    return {step_id: {"status": "done", "at": "2026-08-31T09:00:00Z"} for step_id in step_ids}


def post_pr():
    """Every step satisfied, so what is left to resolve is review work.

    Decision #20 puts a runnable step ahead of dispatching a new review, so a
    fixture that leaves a step runnable is testing the step rule, not the
    review rules.
    """
    steps = done("evaluate", "spec", "draft-pr", "describe")
    steps["review-spec"] = {"status": "released", "at": "2026-08-31T10:15:00Z"}
    return steps


def pr_doc(dispatched=(), collected=(), skipped=()):
    return {
        "pr": "acme/api#115",
        "key": "ABC-123",
        "head": "9c1f0ab",
        "dispatched": list(dispatched),
        "collected": list(collected),
        "skipped": list(skipped),
    }


def findings_doc(*findings):
    return {"pr": "acme/api#115", "next_id": len(findings) + 1, "findings": list(findings)}


# --- rule 4: the pre-PR walk ----------------------------------------------

def test_empty_ticket_starts_at_the_first_step(cfg):
    action = next_action(cfg, ticket_doc(), None, None)
    assert (action.kind, action.target) == ("step", "evaluate")


def test_walks_to_the_next_step_when_needs_are_met(cfg):
    action = next_action(cfg, ticket_doc(done("evaluate")), None, None)
    assert (action.kind, action.target) == ("step", "spec")


def test_an_unreleased_gate_parks(cfg):
    action = next_action(cfg, ticket_doc(done("evaluate", "spec")), None, None)
    assert (action.kind, action.target) == ("gate", "review-spec")
    assert "release" in action.reason


def test_a_released_gate_satisfies_the_next_step(cfg):
    steps = done("evaluate", "spec")
    steps["review-spec"] = {"status": "released", "at": "2026-08-31T10:15:00Z"}
    action = next_action(cfg, ticket_doc(steps), None, None)
    assert (action.kind, action.target) == ("step", "draft-pr")


def test_a_skipped_step_is_walked_past(cfg):
    steps = done("evaluate")
    steps["spec"] = {"status": "skipped", "reason": "trivial"}
    steps["review-spec"] = {"status": "skipped", "reason": "trivial"}
    action = next_action(cfg, ticket_doc(steps), None, None)
    assert (action.kind, action.target) == ("step", "draft-pr")


def test_a_failed_step_is_offered_again(cfg):
    steps = done("evaluate")
    steps["spec"] = {"status": "failed", "exit_code": 2, "log": "/tmp/x.log"}
    action = next_action(cfg, ticket_doc(steps), None, None)
    assert (action.kind, action.target) == ("step", "spec")
    assert "failed" in action.reason


def test_review_rules_do_not_apply_before_the_ticket_has_a_pr(cfg):
    """Rules 1-3 are gated on a PR existing, so the pre-PR walk stays a plain sequence."""
    action = next_action(cfg, ticket_doc(done("evaluate")), None, None)
    assert action.kind == "step"


# --- rule 1: collect before anything else ---------------------------------

def test_a_dispatched_uncollected_review_wins(cfg):
    ticket = ticket_doc(done("evaluate", "spec", "draft-pr"), prs=["acme/api#115"])
    pr = pr_doc(dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}])
    action = next_action(cfg, ticket, pr, None)
    assert (action.kind, action.target) == ("collect", "docs-tests")


def test_collect_beats_an_open_finding(cfg):
    ticket = ticket_doc(done("evaluate", "spec", "draft-pr"), prs=["acme/api#115"])
    pr = pr_doc(dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}])
    findings = findings_doc({"id": "f01", "status": "open", "effort": "easy"})
    action = next_action(cfg, ticket, pr, findings)
    assert action.kind == "collect"


# --- rule 2: open findings ------------------------------------------------

def test_an_open_finding_beats_the_next_review(cfg):
    ticket = ticket_doc(done("evaluate", "spec", "draft-pr"), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": ["f01"]}],
    )
    findings = findings_doc({"id": "f01", "status": "open", "effort": "hard"})
    action = next_action(cfg, ticket, pr, findings)
    assert (action.kind, action.target) == ("fix", "f01")


def test_easy_findings_are_offered_before_hard_ones(cfg):
    """Remote work runs before local work so a local commit never lands on a
    head the bot then moves past."""
    ticket = ticket_doc(done("evaluate", "spec", "draft-pr"), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": ["f01", "f02"]}],
    )
    findings = findings_doc(
        {"id": "f01", "status": "open", "effort": "hard"},
        {"id": "f02", "status": "open", "effort": "easy"},
    )
    assert next_action(cfg, ticket, pr, findings).target == "f02"


def test_findings_with_no_effort_are_offered_last(cfg):
    """effort is never inferred from severity, so a finding without one waits."""
    ticket = ticket_doc(done("evaluate", "spec", "draft-pr"), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": ["f01", "f02"]}],
    )
    findings = findings_doc(
        {"id": "f01", "status": "open", "effort": None, "severity": "blocking"},
        {"id": "f02", "status": "open", "effort": "hard", "severity": "architecture"},
    )
    assert next_action(cfg, ticket, pr, findings).target == "f02"


def test_resolved_and_wontfix_findings_are_ignored(cfg):
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": ["f01", "f02"]}],
    )
    findings = findings_doc(
        {"id": "f01", "status": "resolved", "commit": "4e91c02"},
        {"id": "f02", "status": "wontfix", "reason": "covered by ABC-140"},
    )
    action = next_action(cfg, ticket, pr, findings)
    assert (action.kind, action.target) == ("review", "architecture")


# --- rule 3: the next review ----------------------------------------------

def test_the_first_review_is_offered_once_a_pr_exists(cfg):
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    action = next_action(cfg, ticket, pr_doc(), None)
    assert (action.kind, action.target) == ("review", "docs-tests")


def test_reviews_are_offered_in_order(cfg):
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": []}],
    )
    action = next_action(cfg, ticket, pr, None)
    assert (action.kind, action.target) == ("review", "architecture")


def test_a_skipped_review_is_walked_past(cfg):
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    action = next_action(cfg, ticket, pr_doc(skipped=["docs-tests"]), None)
    assert (action.kind, action.target) == ("review", "architecture")


def test_a_collected_review_we_never_dispatched_does_not_count_as_ours(cfg):
    """oplane-bot is a source of findings, not one of our reviews."""
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    pr = pr_doc(collected=[{"source_id": "IC_1", "review": None, "author": "oplane-bot", "findings": []}])
    action = next_action(cfg, ticket, pr, None)
    assert (action.kind, action.target) == ("review", "docs-tests")


# --- rule 5: at rest ------------------------------------------------------

def test_everything_done_is_at_rest_with_a_reason(cfg):
    steps = done("evaluate", "spec", "draft-pr", "describe")
    steps["review-spec"] = {"status": "released"}
    ticket = ticket_doc(steps, prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[
            {"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"},
            {"review": "architecture", "head": "9c1f0ab", "transport": "local"},
        ],
        collected=[
            {"source_id": "PRR_1", "review": "docs-tests", "findings": []},
            {"source_id": "PRR_2", "review": "architecture", "findings": []},
        ],
    )
    action = next_action(cfg, ticket, pr, findings_doc())
    assert action.kind == "rest"
    assert action.reason


def test_a_runnable_step_outranks_dispatching_a_new_review(cfg):
    """Decision #20. `describe` is declared after `draft-pr`, so it runs there —
    not after every review has been collected and every finding closed."""
    steps = done("evaluate", "spec", "draft-pr")
    steps["review-spec"] = {"status": "released"}
    ticket = ticket_doc(steps, prs=["acme/api#115"])
    action = next_action(cfg, ticket, pr_doc(), None)
    assert (action.kind, action.target) == ("step", "describe")


def test_collect_still_outranks_a_runnable_step(cfg):
    """Collect and fix are about the diff already on the PR, so they stay first."""
    steps = done("evaluate", "spec", "draft-pr")
    steps["review-spec"] = {"status": "released"}
    ticket = ticket_doc(steps, prs=["acme/api#115"])
    pr = pr_doc(dispatched=[{"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"}])
    assert next_action(cfg, ticket, pr, None).kind == "collect"


def test_a_missing_pr_document_reads_as_an_empty_one(cfg):
    """The PR doc is written on the first dispatch, so the review rules cannot
    be gated on its existence — the first review would never be offered."""
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    action = next_action(cfg, ticket, None, None)
    assert (action.kind, action.target) == ("review", "docs-tests")


def test_a_review_redispatched_after_the_head_moved_is_collected_again(cfg):
    """A review can be re-fired once the diff moves, so dispatch and collection
    are counted, not set-compared."""
    ticket = ticket_doc(post_pr(), prs=["acme/api#115"])
    pr = pr_doc(
        dispatched=[
            {"review": "docs-tests", "head": "9c1f0ab", "transport": "bot"},
            {"review": "docs-tests", "head": "deadbee", "transport": "bot"},
        ],
        collected=[{"source_id": "PRR_1", "review": "docs-tests", "findings": []}],
    )
    assert next_action(cfg, ticket, pr, None) == next_action(cfg, ticket, pr, None)
    assert (next_action(cfg, ticket, pr, None).kind,
            next_action(cfg, ticket, pr, None).target) == ("collect", "docs-tests")


def test_repo_overrides_change_the_walk(cfg, tmp_path):
    """A repo that skips `describe` must reach rest instead of offering it."""
    path = tmp_path / "override.yml"
    path.write_text(CONFIG + "\nrepos:\n  acme/api:\n    steps:\n      skip: [describe]\n")
    from ticket.config import load_config as reload

    scoped = reload(path).for_repo("acme/api")
    steps = done("evaluate", "spec", "draft-pr")
    steps["review-spec"] = {"status": "released"}
    ticket = ticket_doc(steps, prs=["acme/api#115"])
    pr = pr_doc(skipped=["docs-tests", "architecture"])
    assert next_action(scoped, ticket, pr, None).kind == "rest"
