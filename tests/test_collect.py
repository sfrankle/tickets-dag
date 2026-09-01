import json
import textwrap
from pathlib import Path

import pytest

from ticket.collect import collect, outstanding
from ticket.config import load_config

FIXTURES = Path(__file__).parent / "fixtures" / "reviews"

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
""")


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(CONFIG)
    prompts = tmp_path / "prompts" / "reviews"
    prompts.mkdir(parents=True)
    (prompts / "docs-tests.md").write_text("Check docs and tests.\n")
    return load_config(path)


def ticket_doc():
    return {"key": "ABC-123", "repo": "acme/api", "prs": ["acme/api#115"], "steps": {}}


def seeded_pr(store):
    store.write_pr(
        {
            "pr": "acme/api#115",
            "key": "ABC-123",
            "head": "9c1f0ab",
            "dispatched": [
                {
                    "review": "docs-tests",
                    "at": "t",
                    "head": "9c1f0ab",
                    "transport": "bot",
                }
            ],
            "collected": [],
            "skipped": [],
        }
    )


def review_payload(body, author="claude-review-bot", review_id="PRR_1"):
    return json.dumps(
        [
            {
                "id": review_id,
                "user": {"login": author},
                "body": body,
                "submitted_at": "t",
            }
        ]
    )


def test_collect_ingests_a_dispatched_review(cfg, store, fake_bin):
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["review"] == "docs-tests"
    assert records[0]["findings"] == ["f01", "f02", "f03"]


def test_collected_findings_get_an_effort(cfg, store, fake_bin):
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    collect(cfg, store, ticket_doc(), "acme/api#115")
    efforts = [f["effort"] for f in store.read_findings("acme/api#115")["findings"]]
    assert efforts == ["easy", "hard", "easy"]


def test_collect_records_the_source_on_each_finding(cfg, store, fake_bin):
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    collect(cfg, store, ticket_doc(), "acme/api#115")
    source = store.read_findings("acme/api#115")["findings"][0]["source"]
    assert source == {"kind": "review", "review": "docs-tests", "source_id": "PRR_1"}


def test_collect_is_idempotent(cfg, store, fake_bin):
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")
    second = collect(cfg, store, ticket, "acme/api#115")
    assert second == []
    assert len(store.read_findings("acme/api#115")["findings"]) == 3


def test_a_review_we_did_not_dispatch_is_recorded_with_a_null_review(
    cfg, store, fake_bin
):
    seeded_pr(store)
    body = (FIXTURES / "review-bot.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload(body, author="review-bot", review_id="PRR_9"),
    )
    fake_bin.respond(
        "claude",
        stdout=json.dumps(
            [
                {
                    "severity": "maintenance",
                    "summary": "headers logged",
                    "body": "...",
                    "file": "src/api/retry.py",
                }
            ]
        ),
    )
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["review"] is None
    assert records[0]["author"] == "review-bot"


def test_issue_comments_are_collected_too(cfg, store, fake_bin):
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    fake_bin.respond(
        "gh api repos/acme/api/issues/115/comments",
        stdout=json.dumps(
            [
                {
                    "id": 42,
                    "user": {"login": "sfrankle"},
                    "body": (FIXTURES / "human-comment.md").read_text(),
                    "created_at": "t",
                }
            ]
        ),
    )
    fake_bin.respond(
        "claude",
        stdout=json.dumps(
            [
                {
                    "severity": "blocking",
                    "summary": "429 spin",
                    "body": "...",
                    "file": "src/api/retry.py",
                }
            ]
        ),
    )
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["source_id"] == "42"
    assert records[0]["author"] == "sfrankle"


def test_our_own_dispatch_comment_is_not_collected_as_a_review(cfg, store, fake_bin):
    """The `/review docs-tests` comment we posted is our request, not a result."""
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    fake_bin.respond(
        "gh api repos/acme/api/issues/115/comments",
        stdout=json.dumps(
            [
                {
                    "id": 7,
                    "user": {"login": "sfrankle"},
                    "body": "/review docs-tests\n<details>\nCheck docs and tests.\n</details>\n",
                    "created_at": "t",
                }
            ]
        ),
    )
    assert collect(cfg, store, ticket_doc(), "acme/api#115") == []


def test_an_empty_review_collects_with_no_findings(cfg, store, fake_bin):
    seeded_pr(store)
    empty = textwrap.dedent("""
        <details>
        <summary>🔴 Blocking</summary>

        None.

        </details>

        **Verdict:** approved.
    """)
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(empty)
    )
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["findings"] == []
    assert fake_bin.calls_to("claude") == []


def test_dry_run_writes_nothing_and_spends_no_tokens(cfg, store, fake_bin):
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    collect(cfg, store, ticket_doc(), "acme/api#115", dry_run=True)
    assert store.read_findings("acme/api#115")["findings"] == []
    assert store.read_pr("acme/api#115")["collected"] == []
    assert fake_bin.calls_to("claude") == []


def test_the_same_finding_arriving_twice_is_minted_once(cfg, store, fake_bin):
    """review-bot posts on every push; a re-run review repeats unfixed items."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload(body, review_id="PRR_2"),
    )
    second = collect(cfg, store, ticket, "acme/api#115")
    assert second[0]["findings"] == []
    assert len(store.read_findings("acme/api#115")["findings"]) == 3


def test_a_resolved_finding_reopens_if_re_raised(cfg, store, fake_bin):
    """A finding that was resolved (or marked wontfix) and is then re-raised
    by a later review round — because the fix was actually wrong — must get
    a fresh entry, not be silently dropped as a duplicate. Dedup only counts
    against still-`open` findings."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")
    doc = store.read_findings("acme/api#115")
    doc["findings"][0]["status"] = "resolved"
    store.write_findings(doc)

    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload(body, review_id="PRR_2"),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    second = collect(cfg, store, ticket, "acme/api#115")

    assert second[0]["findings"] == ["f04"]
    findings = store.read_findings("acme/api#115")["findings"]
    assert len(findings) == 4
    assert findings[3]["status"] == "open"
    assert (findings[3]["file"], findings[3]["summary"]) == (
        doc["findings"][0]["file"],
        doc["findings"][0]["summary"],
    )


def test_collect_fetches_first(cfg, store, fake_bin, tmp_path):
    seeded_pr(store)
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    ticket = ticket_doc()
    ticket["worktree"] = str(checkout)
    collect(cfg, store, ticket, "acme/api#115")
    assert any("fetch" in " ".join(c) for c in fake_bin.calls_to("git"))


def collected_pr(store, source_id="PRR_1", findings=None, review=None, author="claude"):
    """A PR whose `collected` already holds a record for `source_id`."""
    store.write_pr(
        {
            "pr": "acme/api#115",
            "key": "ABC-123",
            "head": "9c1f0ab",
            "dispatched": [
                {
                    "review": "docs-tests",
                    "at": "t",
                    "head": "9c1f0ab",
                    "transport": "bot",
                }
            ],
            "collected": [
                {
                    "source_id": source_id,
                    "review": review,
                    "author": author,
                    "at": "t",
                    "findings": findings or [],
                }
            ],
            "skipped": [],
        }
    )


def test_a_source_recorded_with_zero_findings_is_re_parsed(cfg, store, fake_bin):
    """The #9 grammar bug recorded readable reviews as 0 findings.
    Once the parser can read the body, a re-run must pick those findings up rather than skipping the source id forever."""
    collected_pr(store, review="docs-tests")
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert [r["findings"] for r in records] == [["f01", "f02", "f03"]]
    collected = store.read_pr("acme/api#115")["collected"]
    assert len(collected) == 1
    assert collected[0]["findings"] == ["f01", "f02", "f03"]


def test_a_re_read_source_does_not_claim_a_second_review(cfg, store, fake_bin):
    """The record already occupies its review's collected slot; re-reading it must not consume another one."""
    collected_pr(store, review="docs-tests")
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    collect(cfg, store, ticket_doc(), "acme/api#115")
    collected = store.read_pr("acme/api#115")["collected"]
    assert [c["review"] for c in collected] == ["docs-tests"]


def test_an_empty_source_the_parser_still_cannot_read_spends_no_tokens(
    cfg, store, fake_bin
):
    """Automatic re-reading is free: it re-runs the script parser only.
    A body that is still unreadable is left alone rather than paying for a Haiku call on every run."""
    collected_pr(store, author="someone")
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "human-comment.md").read_text()),
    )
    assert collect(cfg, store, ticket_doc(), "acme/api#115") == []
    assert fake_bin.calls_to("claude") == []


def test_recollect_re_reads_a_source_that_already_had_findings(cfg, store, fake_bin):
    """--recollect is the case automatic re-reading does not cover: a source that produced findings and still needs reading again."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")
    assert collect(cfg, store, ticket, "acme/api#115", recollect=["PRR_1"]) != []


def test_recollecting_does_not_duplicate_findings_already_minted(cfg, store, fake_bin):
    """Dedupe is on the finding fingerprint, so a re-read of a source whose findings are still open mints nothing new."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")
    records = collect(cfg, store, ticket, "acme/api#115", recollect=["PRR_1"])
    assert records[0]["findings"] == []
    assert len(store.read_findings("acme/api#115")["findings"]) == 3
    collected = store.read_pr("acme/api#115")["collected"]
    assert len(collected) == 1
    assert collected[0]["findings"] == ["f01", "f02", "f03"]


def test_recollect_of_a_source_that_is_not_on_the_pr_says_so(
    cfg, store, fake_bin, capsys
):
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    assert collect(cfg, store, ticket_doc(), "acme/api#115", recollect=["PRR_9"]) == []
    assert "PRR_9" in capsys.readouterr().out


def test_collect_does_not_wait_for_an_outstanding_review(cfg, store, fake_bin):
    """`outstanding` is what the CLI says out loud: collect reads what is on the PR now and returns, it never polls."""
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    assert collect(cfg, store, ticket_doc(), "acme/api#115") == []
    assert outstanding(store.read_pr("acme/api#115")) == "docs-tests"
