"""Plain JSON, grouped by ticket key, under the configured store path.

    <store>/
      tickets/
        KEY-123/
          state.json                     the ticket
          acme-api_115.json              one PR
          acme-api_115_findings.json     that PR's findings
          logs/                          one file per run
      locks/

Everything one ticket knows is in one directory, so a ticket can be read, archived or deleted by looking at a single place.
A store written by an older version is type-grouped (`tickets/KEY.json`, `prs/`, `findings/`, `logs/KEY/`); it is migrated in place the first time a `Store` is opened on it.

Concurrency is handled by writing to a temp file and renaming, plus an advisory
lock file per ticket. The CLI is single-user and single-machine; this is enough.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .errors import StoreError

UNKEYED = "_unkeyed"
"""Where a PR whose ticket cannot be named lands.
Nothing writes a `state.json` there, so it never shows up as a ticket; a later read still finds the file."""


def pr_slug(pr_ref: str) -> str:
    """`acme/api#115` -> `acme-api_115`, the on-disk name for a PR.

    The `_` before the number matters: without it `acme/api-115#7` and `acme/api#1157` would land in the same file.
    """
    repo, _, number = pr_ref.partition("#")
    return f"{repo.replace('/', '-')}_{number}"


def now() -> str:
    """UTC timestamp in the form the store records everywhere."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        slug = pr_slug(pr_ref)
        tickets = self.root / "tickets"
        for name in (f"{slug}.json", f"{slug}_findings.json"):
            existing = next(iter(sorted(tickets.glob(f"*/{name}"))), None)
            if existing:
                return existing.parent.name
        return _key_of_pr(tickets, pr_ref) or UNKEYED

    def _pr_file(self, pr_ref: str, key: str | None = None) -> Path:
        key = key or self._key_for_pr(pr_ref)
        return self.ticket_dir(key) / f"{pr_slug(pr_ref)}.json"

    def _findings_file(self, pr_ref: str, key: str | None = None) -> Path:
        key = key or self._key_for_pr(pr_ref)
        return self.ticket_dir(key) / f"{pr_slug(pr_ref)}_findings.json"

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
        """
        path = Path(path)
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def log_file(self, recorded: str | None) -> Path | None:
        """The file a recorded log path names, new-style or old-style."""
        if not recorded:
            return None
        path = Path(recorded)
        return path if path.is_absolute() else self.root / path

    def read_log(self, recorded: str | None) -> str:
        """A step's log, or a sentence saying why there isn't one.

        A recorded path can outlive its file — a moved store, a hand-rename, a cleaned-out directory.
        That is worth saying plainly, not raising over.
        """
        path = self.log_file(recorded)
        if path is None:
            return "no log recorded for this step"
        if not path.is_file():
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
        self._write(self._ticket_file(data["key"]), data)

    def list_tickets(self) -> list[dict]:
        directory = self.root / "tickets"
        if not directory.is_dir():
            return []
        tickets = [self._read(p) for p in directory.glob("*/state.json")]
        return sorted((t for t in tickets if t), key=lambda t: t["key"])

    # --- prs -----------------------------------------------------------

    def read_pr(self, pr_ref: str) -> dict | None:
        return self._read(self._pr_file(pr_ref))

    def write_pr(self, data: dict) -> None:
        # The document names its ticket, so a first write does not have to guess where it goes.
        self._write(self._pr_file(data["pr"], data.get("key")), data)

    # --- findings ------------------------------------------------------

    def read_findings(self, pr_ref: str) -> dict:
        return self._read(self._findings_file(pr_ref)) or {
            "pr": pr_ref,
            "next_id": 1,
            "findings": [],
        }

    def write_findings(self, data: dict) -> None:
        self._write(self._findings_file(data["pr"]), data)

    def add_findings(self, pr_ref: str, findings: list[dict]) -> list[str]:
        """Append findings, minting `fNN` ids. The only place an id is assigned."""
        doc = self.read_findings(pr_ref)
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

    @contextmanager
    def lock(self, key: str):
        directory = self.root / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{key}.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise StoreError(
                f"{key} is locked by another run ({path}). Remove it if that run died."
            ) from None
        try:
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)


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
    os.replace(source, destination)
    return True


def _needs_migration(root: Path) -> bool:
    if any((root / name).is_dir() for name in ("prs", "findings", "logs")):
        return True
    return any((root / "tickets").glob("*.json"))


def migrate(root: Path) -> None:
    """Move a type-grouped store into the by-key layout, in place.

    Idempotent: it does nothing at all once there is no `prs/`, `findings/` or `logs/` directory and no loose `tickets/*.json`.
    Nothing is deleted — files move, empty directories go, and a file whose destination is taken stays put.
    """
    if not root.is_dir() or not _needs_migration(root):
        return

    tickets = root / "tickets"

    # Tickets first: the PR and findings documents are placed by the key their ticket carries, so the ticket directories have to exist to be found.
    for path in sorted(tickets.glob("*.json")):
        _claim(path, tickets / path.stem / "state.json")

    for path in sorted((root / "prs").glob("*.json")):
        doc = _load(path) or {}
        key = doc.get("key") or UNKEYED
        name = f"{pr_slug(doc['pr'])}.json" if doc.get("pr") else path.name
        _claim(path, tickets / key / name)

    for path in sorted((root / "findings").glob("*.json")):
        doc = _load(path) or {}
        key = _key_of_pr(tickets, doc.get("pr")) or UNKEYED
        name = f"{pr_slug(doc['pr'])}_findings.json" if doc.get("pr") else path.name
        _claim(path, tickets / key / name)

    for directory in sorted((root / "logs").glob("*")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file():
                _claim(path, tickets / directory.name / "logs" / path.name)

    _rewrite_log_paths(tickets)

    for name in ("prs", "findings", "logs"):
        _prune(root / name)


def _key_of_pr(tickets: Path, pr_ref: str | None) -> str | None:
    """The key whose directory already holds this PR, or which registered it."""
    if not pr_ref:
        return None
    placed = next(iter(sorted(tickets.glob(f"*/{pr_slug(pr_ref)}.json"))), None)
    if placed:
        return placed.parent.name
    for path in sorted(tickets.glob("*/state.json")):
        doc = _load(path)
        if doc and pr_ref in (doc.get("prs") or []):
            return path.parent.name
    return None


def _rewrite_log_paths(tickets: Path) -> None:
    """Point every recorded log at its new home, relative to the store root.

    Only the file name survives the move, which is all that identifies a log: they were always `<store>/logs/<KEY>/<name>` and are now `tickets/<KEY>/logs/<name>`.
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
            wanted = f"tickets/{key}/logs/{PurePosixPath(str(recorded).replace(os.sep, '/')).name}"
            if recorded != wanted:
                record["log"] = wanted
                changed = True
        if changed:
            Store._write(path, doc)


def _prune(directory: Path) -> None:
    """Remove the old directory once everything in it has been placed."""
    if not directory.is_dir():
        return
    for child in sorted(directory.iterdir(), reverse=True):
        if child.is_dir():
            _prune(child)
    if not any(directory.iterdir()):
        directory.rmdir()
