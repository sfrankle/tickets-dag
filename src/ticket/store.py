"""Plain JSON, grouped by ticket key, under the configured store path.

    <store>/
      tickets/
        KEY-123/
          state.json                     the ticket
          api_115.json                   one PR
          api_115_findings.json          that PR's findings
          logs/                          one file per run
      locks/

Everything one ticket knows is in one directory, so a ticket can be read, archived or deleted by looking at a single place.
A store written by an older version is type-grouped (`tickets/KEY.json`, `prs/`, `findings/`, `logs/KEY/`); it is migrated in place the first time a `Store` is opened on it.
PR documents written before #27 carry the repo owner in their name (`acme-api_115.json`); they are read where they lie rather than renamed.

Concurrency is handled by writing to a temp file and renaming, plus an advisory
lock file per ticket. The CLI is single-user and single-machine; this is enough.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .errors import StoreError

UNKEYED = "_unkeyed"
"""Where a PR whose ticket cannot be named lands.
Nothing writes a `state.json` there, so it never shows up as a ticket; a later read still finds the file."""


def pr_slug(pr_ref: str) -> str:
    """`acme/api#115` -> `api_115`, the on-disk name for a PR.

    The owner is dropped (issue #27): it is the same for every PR a ticket has, and the name is read far more often than it disambiguates.
    Two owners of a same-named repo collide, but only within one ticket's directory, and a ticket works one repo.

    The `_` before the number matters: without it `acme/api-115#7` and `acme/api#1157` would land in the same file.
    """
    repo, _, number = pr_ref.partition("#")
    return f"{repo.rpartition('/')[2]}_{number}"


def legacy_pr_slug(pr_ref: str) -> str:
    """The pre-#27 on-disk name, `acme/api#115` -> `acme-api_115`.

    Only ever looked for, never written: a store written before the owner was dropped keeps the filenames it has, so both forms have to resolve.
    """
    repo, _, number = pr_ref.partition("#")
    return f"{repo.replace('/', '-')}_{number}"


def now() -> str:
    """UTC timestamp in the form the store records everywhere."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(root: Path, path: Path) -> str:
    """How the store names a path: relative to its root, absolute if it falls outside.

    A free function, not a method: the migration names paths before there is a `Store` to ask.
    """
    path = Path(path)
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        migrate(self.root)

    # --- paths ---------------------------------------------------------

    def ticket_dir(self, key: str) -> Path:
        return self.root / "tickets" / key

    def _ticket_file(self, key: str) -> Path:
        return self.ticket_dir(key) / "state.json"

    def _key_for_pr(self, pr_ref: str) -> str:
        """The ticket directory a PR's documents belong in.

        A PR is addressed by its ref alone, so the key is recovered: from a file already on disk, else from the ticket that registered the PR, else `_unkeyed` — a PR document is never dropped for want of a key.
        """
        return _key_of_pr(self.root / "tickets", pr_ref) or UNKEYED

    def _document(self, pr_ref: str, key: str | None, suffix: str) -> Path:
        """Where one of a PR's documents is, or is to be written.

        A file already on disk under the pre-#27 name wins: a store keeps the names it has, and writing the short name beside the long one would split one PR across two documents.
        """
        directory = self.ticket_dir(key or self._key_for_pr(pr_ref))
        legacy = directory / f"{legacy_pr_slug(pr_ref)}{suffix}.json"
        if legacy.is_file():
            return legacy
        return directory / f"{pr_slug(pr_ref)}{suffix}.json"

    def _pr_file(self, pr_ref: str, key: str | None = None) -> Path:
        return self._document(pr_ref, key, "")

    def _findings_file(self, pr_ref: str, key: str | None = None) -> Path:
        return self._document(pr_ref, key, "_findings")

    # --- logs ----------------------------------------------------------

    def log_path(self, key: str, step: str) -> Path:
        """A fresh file for this run, under the ticket's own `logs/`.

        One file per run, not per day: a step that is re-run after a failure must not append to the log of the run that failed.
        """
        directory = self.ticket_dir(key) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{step}-{stamp}.log"
        attempt = 1
        while path.exists():
            attempt += 1
            path = directory / f"{step}-{stamp}-{attempt}.log"
        return path

    def relative(self, path: Path) -> str:
        """How a path is recorded in state: relative to the store root.

        Recorded absolute, a log path stops resolving as soon as the store moves, and nothing re-checks it (issue #7).
        A path outside the root is returned absolute, since there is nothing to record it relative to; every log the store itself writes is inside.
        """
        return _relative(self.root, path)

    def log_file(self, recorded: str | None) -> Path | None:
        """The file a recorded log path names, new-style or old-style."""
        if not recorded:
            return None
        path = Path(recorded)
        return path if path.is_absolute() else self.root / path

    def log_missing(self, recorded: str | None) -> bool:
        """Whether a step names a log that is not there.

        A recorded path can outlive its file — a moved store, a hand-rename, a cleaned-out directory.
        Recording nothing is not missing: the step simply never wrote one.
        """
        path = self.log_file(recorded)
        return path is not None and not path.is_file()

    def read_log(self, recorded: str | None) -> str:
        """A step's log, or a sentence saying why there isn't one.

        A path that has outlived its file is worth saying plainly, not raising over.
        """
        path = self.log_file(recorded)
        if path is None:
            return "no log recorded for this step"
        if self.log_missing(recorded):
            return f"no log file at {path}: it was moved, renamed or deleted"
        return path.read_text()

    # --- io ------------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path} is not valid JSON: {exc}") from exc

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # --- tickets -------------------------------------------------------

    def read_ticket(self, key: str) -> dict | None:
        return self._read(self._ticket_file(key))

    def write_ticket(self, data: dict) -> None:
        # Every write is what "last updated" means, so it is stamped in the one place every write goes through rather than at each call site.
        data["updated"] = now()
        self._write(self._ticket_file(data["key"]), data)

    def list_tickets(self) -> list[dict]:
        """Every ticket, most recently updated first (issue #27).

        Timestamps are to the second, so writes within one second tie; the key breaks the tie, which keeps the order stable rather than arbitrary.
        A ticket written before `updated` existed sorts last, where a ticket nothing has touched belongs.
        """
        directory = self.root / "tickets"
        if not directory.is_dir():
            return []
        tickets = sorted(
            (t for t in (self._read(p) for p in directory.glob("*/state.json")) if t),
            key=lambda t: t["key"],
        )
        # Sorted by key first and stably by time second, so tickets sharing a timestamp keep a fixed order instead of the directory's.
        return sorted(tickets, key=lambda t: t.get("updated", ""), reverse=True)

    # --- prs -----------------------------------------------------------

    def read_pr(self, pr_ref: str, key: str | None = None) -> dict | None:
        # `key` when the caller holds the ticket: it saves the walk over `tickets/` that placing the PR otherwise needs.
        return self._read(self._pr_file(pr_ref, key))

    def write_pr(self, data: dict) -> None:
        # The document names its ticket, so a first write does not have to guess where it goes.
        self._write(self._pr_file(data["pr"], data.get("key")), data)

    # --- findings ------------------------------------------------------

    def read_findings(self, pr_ref: str, key: str | None = None) -> dict:
        doc = self._read(self._findings_file(pr_ref, key))
        if doc is not None:
            return doc
        fresh = {"pr": pr_ref, "next_id": 1, "findings": []}
        # The document records its ticket, so every later read of it agrees on where it lives without having to find the PR document first.
        if key:
            fresh["key"] = key
        return fresh

    def write_findings(self, data: dict) -> None:
        self._write(self._findings_file(data["pr"], data.get("key")), data)

    def add_findings(
        self, pr_ref: str, findings: list[dict], key: str | None = None
    ) -> list[str]:
        """Append findings, minting `fNN` ids. The only place an id is assigned.

        `key` names the ticket the PR belongs to.
        Without it the document's place is a lookup over what is on disk, and findings written before the PR document lands in `_unkeyed`, where the next read does not look — which restarts `next_id` and re-uses ids that are already out.
        """
        doc = self.read_findings(pr_ref, key)
        assigned: list[str] = []
        for finding in findings:
            finding_id = f"f{doc['next_id']:02d}"
            doc["next_id"] += 1
            # id last: a parsed finding carrying its own id must not keep it.
            record = {"status": "open", **finding, "id": finding_id}
            doc["findings"].append(record)
            assigned.append(finding_id)
        self.write_findings(doc)
        return assigned

    # --- locking -------------------------------------------------------

    def lock_path(self, key: str) -> Path:
        return self.root / "locks" / f"{key}.lock"

    def lock_status(self, key: str) -> LockStatus | None:
        """Who holds this key's lock, and whether that run is still running.

        `None` means nothing holds it. Otherwise the pid the lock file records,
        and whether a process with that pid exists — which is what separates a
        run still working from one that died without releasing (issue #27).
        """
        path = self.lock_path(key)
        if not path.exists():
            return None
        pid = _lock_pid(path)
        return LockStatus(pid=pid)

    def clear_lock(self, key: str) -> None:
        self.lock_path(key).unlink(missing_ok=True)

    @contextmanager
    def lock(self, key: str):
        path = self.lock_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise StoreError(
                f"{key} is locked by another run ({path}). "
                f"Run `ticket unlock {key}` to clear it if that run died."
            ) from None
        try:
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)


# --- locking helpers ------------------------------------------------------


@dataclass(frozen=True)
class LockStatus:
    pid: int | None

    @property
    def alive(self) -> bool:
        return self.pid is not None and _alive(self.pid)


def _lock_pid(path: Path) -> int | None:
    """The pid a lock file records, or `None` if it does not record a usable one.

    A run killed between creating the file and writing its pid leaves it empty.
    Unknown is not the same as alive: the file is stale either way, and reading
    it as a live holder would make the one verb that clears it refuse forever.
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """Does a process with this pid exist?

    Signal 0 checks for the process without sending anything. `PermissionError`
    means it exists and belongs to someone else, which is still alive.
    Pids are reused, so this can in principle name a different process than the
    one that took the lock; nothing cheap distinguishes them, and the alternative
    is the hand-deleted lock file this replaces.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


# --- migration ------------------------------------------------------------


def _load(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return doc if isinstance(doc, dict) else None


def _claim(source: Path, destination: Path) -> bool:
    """Move `source` onto `destination` unless something is already there.

    Never clobbers: a store that has been half-migrated by hand keeps the file it already has, and the one that could not be placed is left where it is for a human to look at rather than deleted.
    """
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except FileNotFoundError:
        # Another process opened the same old store and moved it first.
        return False
    return True


def _needs_migration(root: Path) -> bool:
    if any((root / name).is_dir() for name in ("prs", "findings", "logs")):
        return True
    return any((root / "tickets").glob("*.json"))


def migrate(root: Path) -> None:
    """Move a type-grouped store into the by-key layout, in place.

    Nothing is deleted — files move, empty directories go, and a file whose destination is taken stays put.
    Anything left behind is named on stderr: it is invisible to every later read, so silence would be the store quietly losing it.

    Idempotent: it does nothing at all once there is no `prs/`, `findings/` or `logs/` directory and no loose `tickets/*.json`.
    A store it could not place everything in keeps one of those, so it runs again on every open — and says the same thing again, which is the point.

    `TICKET_NO_MIGRATE` turns it off, for looking at an old store without rewriting it.
    Every read then goes to the by-key layout and finds nothing, so the store reads as empty rather than as it was.
    """
    if os.environ.get("TICKET_NO_MIGRATE", "") not in ("", "0"):
        return
    if not root.is_dir() or not _needs_migration(root):
        return

    tickets = root / "tickets"
    moved = 0
    left: list[str] = []

    def claim(source: Path, destination: Path) -> bool:
        nonlocal moved
        if _claim(source, destination):
            moved += 1
            return True
        left.append(
            f"{_relative(root, source)} not moved: {_relative(root, destination)} already exists"
        )
        return False

    # Tickets first: the PR and findings documents are placed by the key their ticket carries, so the ticket directories have to exist to be found.
    for path in sorted(tickets.glob("*.json")):
        claim(path, tickets / path.stem / "state.json")

    def place(directory: str, suffix: str, key_of) -> None:
        """File one old directory of PR-addressed documents under the tickets that own them.

        A document that names no PR keeps its old filename: it is not one of ours to rename, and the point is to lose nothing.
        """
        for path in sorted((root / directory).glob("*.json")):
            doc = _load(path)
            if doc is None:
                left.append(
                    f"{_relative(root, path)} left in place: not a readable JSON object"
                )
                continue
            key = key_of(doc) or UNKEYED
            name = f"{pr_slug(doc['pr'])}{suffix}.json" if doc.get("pr") else path.name
            if claim(path, tickets / key / name) and key == UNKEYED:
                left.append(
                    f"{_relative(root, path)} names no ticket: filed under tickets/{UNKEYED}/"
                )

    # The PR document carries its key; a findings document only names its PR, so its ticket is the one that already holds that PR.
    place("prs", "", lambda doc: doc.get("key"))
    place("findings", "_findings", lambda doc: _key_of_pr(tickets, doc.get("pr")))

    placed_logs: set[tuple[str, str]] = set()
    for entry in sorted((root / "logs").glob("*")):
        if not entry.is_dir():
            left.append(
                f"{_relative(root, entry)} left in place: not a ticket's log directory"
            )
            continue
        for path in sorted(entry.iterdir()):
            if not path.is_file():
                left.append(f"{_relative(root, path)} left in place: not a log file")
                continue
            if claim(path, tickets / entry.name / "logs" / path.name):
                placed_logs.add((entry.name, path.name))

    _rewrite_log_paths(tickets, placed_logs)

    for name in ("prs", "findings", "logs"):
        _prune(root / name)

    if moved or left:
        print(
            f"ticket: migrated {moved} file(s) under {root} to the by-key layout",
            file=sys.stderr,
        )
        for note in left:
            print(f"ticket:   {note}", file=sys.stderr)


def _key_of_pr(tickets: Path, pr_ref: str | None) -> str | None:
    """The key whose directory already holds this PR, or which registered it.

    Either of the PR's two documents answers it: a store half-migrated by hand can have the findings without the PR.
    """
    if not pr_ref:
        return None
    names = [
        f"{slug}{suffix}.json"
        for slug in (pr_slug(pr_ref), legacy_pr_slug(pr_ref))
        for suffix in ("", "_findings")
    ]
    for name in dict.fromkeys(names):
        placed = next(iter(sorted(tickets.glob(f"*/{name}"))), None)
        if placed:
            return placed.parent.name
    for path in sorted(tickets.glob("*/state.json")):
        doc = _load(path)
        if doc and pr_ref in (doc.get("prs") or []):
            return path.parent.name
    return None


def _rewrite_log_paths(tickets: Path, placed: set[tuple[str, str]]) -> None:
    """Point every log that actually moved at its new home, relative to the store root.

    Only the file name survives the move, which is all that identifies a log: they were always `<store>/logs/<KEY>/<name>` and are now `tickets/<KEY>/logs/<name>`.
    Only `placed` is rewritten. A log the migration could not move is still at the path its step records, and rewriting it would name the file that won the destination instead — a step pointing at another run's output, which nothing downstream could tell from the real thing.
    """
    for path in sorted(tickets.glob("*/state.json")):
        doc = _load(path)
        if not doc:
            continue
        key = path.parent.name
        changed = False
        for record in (doc.get("steps") or {}).values():
            recorded = isinstance(record, dict) and record.get("log")
            if not recorded:
                continue
            name = PurePosixPath(str(recorded).replace(os.sep, "/")).name
            if (key, name) not in placed:
                continue
            wanted = f"tickets/{key}/logs/{name}"
            if recorded != wanted:
                record["log"] = wanted
                changed = True
        if changed:
            Store._write(path, doc)


def _prune(directory: Path) -> None:
    """Remove the old directory once everything in it has been placed."""
    if not directory.is_dir():
        return
    for child in directory.iterdir():
        if child.is_dir():
            _prune(child)
    if not any(directory.iterdir()):
        directory.rmdir()
