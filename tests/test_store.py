import json
from pathlib import Path

import pytest

from ticket.errors import StoreError
from ticket.store import Store, pr_slug


def test_pr_slug():
    assert pr_slug("acme/api#115") == "acme-api_115"


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
    stray = list((store.root / "tickets").rglob("*.tmp*"))
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
    text = (store.root / "tickets" / "ABC-123" / "state.json").read_text()
    assert text.endswith("\n")
    assert json.loads(text)["key"] == "ABC-123"


def test_reading_corrupt_json_raises_store_error(store):
    for relative in (
        "tickets/ABC-123/state.json",
        "tickets/ABC-123/acme-api_115.json",
        "tickets/ABC-123/acme-api_115_findings.json",
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


def test_a_finding_that_arrives_with_an_id_gets_a_minted_one(store):
    """A parsed review body can carry an `id`; ids are minted here or nowhere."""
    assigned = store.add_findings("acme/api#115", [{"id": "theirs", "summary": "x"}])
    assert assigned == ["f01"]
    assert store.read_findings("acme/api#115")["findings"][0]["id"] == "f01"


# --- layout ---------------------------------------------------------------


def test_a_ticket_is_a_directory_named_by_its_key(store):
    store.write_ticket({"key": "ABC-123"})
    assert (store.root / "tickets" / "ABC-123" / "state.json").is_file()


def test_a_pr_document_lives_beside_its_ticket(store):
    store.write_pr({"pr": "acme/api#115", "key": "ABC-123", "head": "9c1f0ab"})
    assert (store.root / "tickets" / "ABC-123" / "acme-api_115.json").is_file()


def test_findings_live_beside_the_pr_they_belong_to(store):
    store.write_pr({"pr": "acme/api#115", "key": "ABC-123"})
    store.add_findings("acme/api#115", [{"summary": "a"}])
    expected = store.root / "tickets" / "ABC-123" / "acme-api_115_findings.json"
    assert expected.is_file()
    assert store.read_findings("acme/api#115")["findings"][0]["id"] == "f01"


def test_findings_follow_the_ticket_that_registered_the_pr(store):
    """The PR document need not exist yet: the ticket already names the PR."""
    store.write_ticket({"key": "ABC-123", "prs": ["acme/api#115"]})
    store.add_findings("acme/api#115", [{"summary": "a"}])
    assert (store.root / "tickets" / "ABC-123" / "acme-api_115_findings.json").is_file()


def test_logs_live_under_the_ticket_directory(store):
    path = store.log_path("ABC-123", "evaluate")
    assert path.parent == store.root / "tickets" / "ABC-123" / "logs"
    assert path.parent.is_dir()


def test_each_run_gets_its_own_log_file(store):
    first = store.log_path("ABC-123", "evaluate")
    first.write_text("one")
    second = store.log_path("ABC-123", "evaluate")
    second.write_text("two")
    assert first != second
    assert first.read_text() == "one"


def test_a_recorded_log_path_is_relative_to_the_store_root(store):
    recorded = store.relative(store.log_path("ABC-123", "evaluate"))
    assert not Path(recorded).is_absolute()
    assert recorded.startswith("tickets/ABC-123/logs/evaluate-")


def test_a_recorded_log_path_resolves_back_to_the_file(store):
    path = store.log_path("ABC-123", "evaluate")
    path.write_text("hello")
    assert store.read_log(store.relative(path)) == "hello"


def test_an_absolute_recorded_log_path_still_resolves(store):
    path = store.log_path("ABC-123", "evaluate")
    path.write_text("hello")
    assert store.read_log(str(path)) == "hello"


def test_reading_a_missing_log_says_so_instead_of_raising(store):
    text = store.read_log("tickets/ABC-123/logs/evaluate-20260901T120416Z.log")
    assert "no log file" in text
    assert "evaluate-20260901T120416Z.log" in text


def test_reading_an_unrecorded_log_says_so(store):
    assert "no log" in store.read_log(None)


# --- migration ------------------------------------------------------------


def write_old_layout(root: Path) -> None:
    """The type-grouped layout this store used to write."""
    (root / "tickets").mkdir(parents=True)
    (root / "prs").mkdir(parents=True)
    (root / "findings").mkdir(parents=True)
    (root / "logs" / "ABC-123").mkdir(parents=True)
    (root / "logs" / "ABC-123" / "evaluate-20260901T120416Z.log").write_text("old log")
    (root / "tickets" / "ABC-123.json").write_text(
        json.dumps(
            {
                "key": "ABC-123",
                "repo": "acme/api",
                "tracked": True,
                "prs": ["acme/api#115"],
                "steps": {
                    "evaluate": {
                        "status": "done",
                        "log": str(
                            root / "logs" / "ABC-123" / "evaluate-20260901T120416Z.log"
                        ),
                    }
                },
            }
        )
    )
    (root / "prs" / "acme-api-pr115.json").write_text(
        json.dumps({"pr": "acme/api#115", "key": "ABC-123", "head": "9c1f0ab"})
    )
    (root / "findings" / "acme-api-pr115.json").write_text(
        json.dumps(
            {
                "pr": "acme/api#115",
                "next_id": 2,
                "findings": [{"id": "f01", "status": "open", "summary": "a"}],
            }
        )
    )


def test_an_old_store_is_migrated_on_load(tmp_path):
    root = tmp_path / "store"
    write_old_layout(root)
    store = Store(root)

    assert store.read_ticket("ABC-123")["repo"] == "acme/api"
    assert store.read_pr("acme/api#115")["head"] == "9c1f0ab"
    assert store.read_findings("acme/api#115")["findings"][0]["id"] == "f01"
    key_dir = root / "tickets" / "ABC-123"
    assert (key_dir / "state.json").is_file()
    assert (key_dir / "acme-api_115.json").is_file()
    assert (key_dir / "acme-api_115_findings.json").is_file()
    assert (key_dir / "logs" / "evaluate-20260901T120416Z.log").read_text() == "old log"
    assert not (root / "prs").exists()
    assert not (root / "findings").exists()
    assert not (root / "logs").exists()


def test_migration_rewrites_recorded_log_paths_to_relative_ones(tmp_path):
    root = tmp_path / "store"
    write_old_layout(root)
    store = Store(root)
    recorded = store.read_ticket("ABC-123")["steps"]["evaluate"]["log"]
    assert recorded == "tickets/ABC-123/logs/evaluate-20260901T120416Z.log"
    assert store.read_log(recorded) == "old log"


def test_migration_is_idempotent(tmp_path):
    root = tmp_path / "store"
    write_old_layout(root)
    Store(root)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    store = Store(root)
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    assert before == after
    assert store.read_ticket("ABC-123")["steps"]["evaluate"]["log"] == (
        "tickets/ABC-123/logs/evaluate-20260901T120416Z.log"
    )


def test_migration_never_overwrites_an_already_migrated_file(tmp_path):
    root = tmp_path / "store"
    write_old_layout(root)
    key_dir = root / "tickets" / "ABC-123"
    key_dir.mkdir(parents=True)
    (key_dir / "state.json").write_text(json.dumps({"key": "ABC-123", "repo": "new"}))
    Store(root)
    assert json.loads((key_dir / "state.json").read_text())["repo"] == "new"
    # The file it could not claim is left where it is rather than dropped.
    assert (root / "tickets" / "ABC-123.json").is_file()


def test_locks_stay_at_the_top_level(store):
    with store.lock("ABC-123"):
        assert (store.root / "locks" / "ABC-123.lock").is_file()
