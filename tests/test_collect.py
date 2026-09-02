import json
import textwrap
from pathlib import Path

import pytest

from ticket.collect import collect, outstanding
from ticket.config import load_config
from ticket.errors import TicketError

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


def test_recollect_of_a_source_that_is_not_on_the_pr_is_an_error(cfg, store, fake_bin):
    """A mistyped source id that exits 0 reads as a successful re-read to whatever ran us."""
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    with pytest.raises(TicketError) as excinfo:
        collect(cfg, store, ticket_doc(), "acme/api#115", recollect=["PRR_9"])
    assert "PRR_9" in str(excinfo.value)


def test_a_source_that_does_exist_is_still_collected_alongside_a_bad_id(
    cfg, store, fake_bin
):
    """The refusal comes after the run, so one wrong id does not throw away the sources that were there."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews", stdout=review_payload(body)
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    with pytest.raises(TicketError):
        collect(cfg, store, ticket_doc(), "acme/api#115", recollect=["PRR_9"])
    collected = store.read_pr("acme/api#115")["collected"]
    assert [c["source_id"] for c in collected] == ["PRR_1"]
    assert collected[0]["findings"] == ["f01", "f02", "f03"]


def test_recollect_recovers_findings_a_source_did_not_have_before(cfg, store, fake_bin):
    """The case --recollect exists for: a source that already produced findings, whose body now holds more of them.
    The new ones are minted and the record's history is the union, not a replacement."""
    seeded_pr(store)
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "example-review.md").read_text()),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")

    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "wide-review.md").read_text()),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard"]))
    records = collect(cfg, store, ticket, "acme/api#115", recollect=["PRR_1"])

    assert [r["findings"] for r in records] == [["f04", "f05"]]
    collected = store.read_pr("acme/api#115")["collected"]
    assert len(collected) == 1
    assert collected[0]["findings"] == ["f01", "f02", "f03", "f04", "f05"]
    assert len(store.read_findings("acme/api#115")["findings"]) == 5


def test_a_dry_run_re_read_is_reported_as_a_re_read(cfg, store, fake_bin):
    """The caller prints from the returned record, so a dry run that says `would re-read` must not hand back a record that prints as a fresh collection."""
    collected_pr(store, review="docs-tests")
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "example-review.md").read_text()),
    )
    records = collect(cfg, store, ticket_doc(), "acme/api#115", dry_run=True)
    assert [r["reread"] for r in records] == [True]


def test_collect_does_not_wait_for_an_outstanding_review(cfg, store, fake_bin):
    """`outstanding` is what the CLI says out loud: collect reads what is on the PR now and returns, it never polls."""
    seeded_pr(store)
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout="[]")
    assert collect(cfg, store, ticket_doc(), "acme/api#115") == []
    assert outstanding(store.read_pr("acme/api#115")) == "docs-tests"


def test_a_re_read_that_recovers_nothing_stays_quiet(cfg, store, fake_bin, capsys):
    """review-bot re-posts its unfixed items on every push, so the same findings arrive again under a second source id.
    Once they are open, re-reading the record that missed them recovers nothing.
    Recording that as work would leave the record empty, so it would be re-read, rewritten and printed on every run after this one."""
    seeded_pr(store)
    body = (FIXTURES / "example-review.md").read_text()
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload(body, review_id="PRR_2"),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    ticket = ticket_doc()
    collect(cfg, store, ticket, "acme/api#115")

    # The same review, posted on an earlier push and recorded with no findings by the #9 grammar bug.
    pr = store.read_pr("acme/api#115")
    pr["collected"].insert(
        0,
        {
            "source_id": "PRR_1",
            "review": None,
            "author": "claude-review-bot",
            "at": "t",
            "findings": [],
        },
    )
    store.write_pr(pr)

    both = json.dumps(
        [
            {
                "id": source_id,
                "user": {"login": "claude-review-bot"},
                "body": body,
                "submitted_at": "t",
            }
            for source_id in ("PRR_1", "PRR_2")
        ]
    )
    fake_bin.respond("gh api repos/acme/api/pulls/115/reviews", stdout=both)
    capsys.readouterr()

    for _ in range(3):
        assert collect(cfg, store, ticket, "acme/api#115") == []

    assert "re-read" not in capsys.readouterr().out
    assert len(store.read_findings("acme/api#115")["findings"]) == 3
    collected = store.read_pr("acme/api#115")["collected"]
    assert [c["findings"] for c in collected] == [[], ["f01", "f02", "f03"]]
    assert "reread_at" not in collected[0]


def two_dispatches_pr(store, collected):
    """A PR with two dispatches outstanding and one existing collection record."""
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
                },
                {"review": "perf", "at": "t", "head": "9c1f0ab", "transport": "bot"},
            ],
            "collected": [collected],
            "skipped": [],
        }
    )


def test_a_re_read_of_a_null_review_record_claims_no_slot(cfg, store, fake_bin):
    """`review: null` is a decision already made — this body answered no dispatch we know of.
    A re-read must not ask `next_uncollected` again: it would hand back whatever is outstanding *now*, which is a dispatch this body cannot have answered."""
    two_dispatches_pr(
        store,
        {
            "source_id": "PRR_1",
            "review": None,
            "author": "claude-review-bot",
            "at": "t",
            "findings": [],
        },
    )
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "example-review.md").read_text()),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["review"] is None
    assert outstanding(store.read_pr("acme/api#115")) == "docs-tests"


def test_recollecting_a_null_review_record_claims_no_slot(cfg, store, fake_bin):
    """Same contract by the other entry point: naming a source for a forced re-read does not re-attribute it."""
    two_dispatches_pr(
        store,
        {
            "source_id": "PRR_1",
            "review": None,
            "author": "claude-review-bot",
            "at": "t",
            "findings": ["f01"],
        },
    )
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "example-review.md").read_text()),
    )
    fake_bin.respond("claude", stdout=json.dumps(["easy", "hard", "easy"]))
    records = collect(cfg, store, ticket_doc(), "acme/api#115", recollect=["PRR_1"])
    assert records[0]["review"] is None
    assert outstanding(store.read_pr("acme/api#115")) == "docs-tests"


def test_an_all_clear_review_answers_its_dispatch(cfg, store, fake_bin):
    """`[]` from the script parser means ours, and it found nothing (issue #23 item 1).
    A reviewer that ran and had nothing to say has answered its dispatch, so it takes that dispatch's collected slot and stops being outstanding — otherwise `next` asks for a `collect` that can never clear."""
    seeded_pr(store)
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload((FIXTURES / "all-clear.md").read_text()),
    )
    records = collect(cfg, store, ticket_doc(), "acme/api#115")
    assert records[0]["review"] == "docs-tests"
    assert records[0]["findings"] == []
    assert outstanding(store.read_pr("acme/api#115")) is None
    assert fake_bin.calls_to("claude") == []


def test_a_body_the_parser_cannot_read_answers_no_dispatch(cfg, store, fake_bin):
    """The other half of the same contract: `None` is a body we could not read as a review at all, so it claims no slot and the dispatch stays outstanding."""
    seeded_pr(store)
    fake_bin.respond(
        "gh api repos/acme/api/pulls/115/reviews",
        stdout=review_payload(
            (FIXTURES / "human-comment.md").read_text(), author="sfrankle"
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
    assert records[0]["review"] is None
    assert outstanding(store.read_pr("acme/api#115")) == "docs-tests"
