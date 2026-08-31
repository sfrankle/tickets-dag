import json

import pytest

from ticket.errors import StoreError
from ticket.store import pr_slug


def test_pr_slug():
    assert pr_slug("acme/api#115") == "acme-api-pr115"


def test_pr_slug_does_not_collide_with_a_repo_named_after_a_number():
    assert pr_slug("acme/api-115#7") != pr_slug("acme/api#1157")


def test_reading_an_absent_ticket_returns_none(store):
    assert store.read_ticket("ABC-123") is None


def test_ticket_round_trip(store):
    store.write_ticket({"key": "ABC-123", "repo": "acme/api", "tracked": True})
    assert store.read_ticket("ABC-123")["repo"] == "acme/api"


def test_list_tickets_is_sorted(store):
    for key in ("ABC-9", "ABC-10", "ZZZ-1"):
        store.write_ticket({"key": key})
    assert [t["key"] for t in store.list_tickets()] == ["ABC-10", "ABC-9", "ZZZ-1"]


def test_pr_round_trip(store):
    store.write_pr({"pr": "acme/api#115", "key": "ABC-123", "head": "9c1f0ab"})
    assert store.read_pr("acme/api#115")["head"] == "9c1f0ab"


def test_absent_findings_file_reads_as_an_empty_doc(store):
    doc = store.read_findings("acme/api#115")
    assert doc == {"pr": "acme/api#115", "next_id": 1, "findings": []}


def test_add_findings_assigns_sequential_ids(store):
    first = store.add_findings("acme/api#115", [{"summary": "a"}, {"summary": "b"}])
    second = store.add_findings("acme/api#115", [{"summary": "c"}])
    assert first == ["f01", "f02"]
    assert second == ["f03"]


def test_added_findings_default_to_open(store):
    store.add_findings("acme/api#115", [{"summary": "a"}])
    assert store.read_findings("acme/api#115")["findings"][0]["status"] == "open"


def test_ids_are_never_reused_after_removal(store):
    store.add_findings("acme/api#115", [{"summary": "a"}])
    doc = store.read_findings("acme/api#115")
    doc["findings"] = []
    store.write_findings(doc)
    assert store.add_findings("acme/api#115", [{"summary": "b"}]) == ["f02"]


def test_writes_are_atomic_and_leave_no_temp_files(store):
    store.write_ticket({"key": "ABC-123"})
    stray = list((store.root / "tickets").glob("*.tmp*"))
    assert stray == []


def test_write_failure_leaves_the_old_file_intact(store, monkeypatch):
    store.write_ticket({"key": "ABC-123", "repo": "acme/api"})

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        store.write_ticket({"key": "ABC-123", "repo": "acme/other"})
    assert store.read_ticket("ABC-123")["repo"] == "acme/api"


def test_stored_json_is_human_readable(store):
    store.write_ticket({"key": "ABC-123"})
    text = (store.root / "tickets" / "ABC-123.json").read_text()
    assert text.endswith("\n")
    assert json.loads(text)["key"] == "ABC-123"


def test_reading_corrupt_json_raises_store_error(store):
    for relative in (
        "tickets/ABC-123.json",
        "prs/acme-api-pr115.json",
        "findings/acme-api-pr115.json",
    ):
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")

    with pytest.raises(StoreError, match="not valid JSON"):
        store.read_ticket("ABC-123")
    with pytest.raises(StoreError, match="not valid JSON"):
        store.read_pr("acme/api#115")
    with pytest.raises(StoreError, match="not valid JSON"):
        store.read_findings("acme/api#115")


def test_lock_is_exclusive(store):
    with (
        store.lock("ABC-123"),
        pytest.raises(StoreError, match="ABC-123"),
        store.lock("ABC-123"),
    ):
        pass


def test_lock_is_released_after_an_exception(store):
    with pytest.raises(ValueError), store.lock("ABC-123"):
        raise ValueError("boom")
    with store.lock("ABC-123"):
        pass


def test_log_path_creates_its_directory(store):
    path = store.log_path("ABC-123", "evaluate")
    assert path.parent.is_dir()
    assert path.name.startswith("evaluate-")
