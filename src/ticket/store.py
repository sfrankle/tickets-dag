"""Plain JSON, one file per concern, under the configured store path.

Concurrency is handled by writing to a temp file and renaming, plus an advisory
lock file per ticket. The CLI is single-user and single-machine; this is enough.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import StoreError


def pr_slug(pr_ref: str) -> str:
    """`acme/api#115` -> `acme-api-pr115`, the on-disk name for a PR.

    The `pr` marker matters: without it `acme/api-115#7` and `acme/api#1157`
    would land in the same file.
    """
    repo, _, number = pr_ref.partition("#")
    return f"{repo.replace('/', '-')}-pr{number}"


def now() -> str:
    """UTC timestamp in the form the store records everywhere."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    # --- paths ---------------------------------------------------------

    def _ticket_file(self, key: str) -> Path:
        return self.root / "tickets" / f"{key}.json"

    def _pr_file(self, pr_ref: str) -> Path:
        return self.root / "prs" / f"{pr_slug(pr_ref)}.json"

    def _findings_file(self, pr_ref: str) -> Path:
        return self.root / "findings" / f"{pr_slug(pr_ref)}.json"

    def log_path(self, key: str, step: str) -> Path:
        directory = self.root / "logs" / key
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return directory / f"{step}-{stamp}.log"

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
        tickets = [self._read(p) for p in directory.glob("*.json")]
        return sorted((t for t in tickets if t), key=lambda t: t["key"])

    # --- prs -----------------------------------------------------------

    def read_pr(self, pr_ref: str) -> dict | None:
        return self._read(self._pr_file(pr_ref))

    def write_pr(self, data: dict) -> None:
        self._write(self._pr_file(data["pr"]), data)

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
            record = {"id": finding_id, "status": "open", **finding}
            record["id"] = finding_id
            record.setdefault("status", "open")
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
