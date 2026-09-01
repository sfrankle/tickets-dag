"""Thin wrappers around `gh` and `git`.

Network failures are retried three times with backoff, then reported. This is
the only module that knows how to split a `owner/repo#number` reference.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .errors import GhError

RETRY_BACKOFF = (1, 2)


def split_ref(pr_ref: str) -> tuple[str, str]:
    """`acme/api#115` -> (`acme/api`, `115`)."""
    try:
        repo, number = pr_ref.split("#", 1)
    except ValueError:
        raise GhError(f"{pr_ref!r} is not owner/repo#number") from None
    return repo, number


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    retries: int = 3,
    sleep=time.sleep,
) -> str:
    last = ""
    for attempt in range(retries):
        result = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        last = result.stderr.strip() or f"exit {result.returncode}"
        if attempt < retries - 1:
            sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    raise GhError(f"{' '.join(argv)} failed after {retries} attempts: {last}")


def gh_json(argv: list[str], **kwargs):
    text = run(argv, **kwargs)
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise GhError(f"{' '.join(argv)} did not return JSON: {exc}") from exc


def pr_head(pr_ref: str) -> str:
    repo, number = split_ref(pr_ref)
    data = gh_json(["gh", "pr", "view", number, "--repo", repo, "--json", "headRefOid"])
    if not isinstance(data, dict) or not data.get("headRefOid"):
        raise GhError(f"gh pr view {pr_ref} returned no headRefOid")
    return data["headRefOid"]


def pr_comment(pr_ref: str, body: str, *, dry_run: bool) -> None:
    repo, number = split_ref(pr_ref)
    if dry_run:
        print(f"[dry-run] would comment on {pr_ref}:\n{body}")
        return
    # retries=1: posting a comment is not idempotent. A `gh` that fails after
    # the comment landed would otherwise post it three times.
    run(["gh", "pr", "comment", number, "--repo", repo, "--body", body], retries=1)


def is_dirty(worktree: Path) -> bool:
    return bool(run(["git", "status", "--porcelain"], cwd=worktree, retries=1).strip())


def sync(worktree: Path | None) -> str | None:
    """Fetch, then fast-forward the checkout. Cheap, so it runs often.

    Returns None when the checkout is in sync, or a one-line reason it is not.
    Never raises: the bot commits on the remote and the local checkout drifting
    behind is the normal case, not an error to abort a command on. A dirty tree
    or a diverged branch is reported and left alone — this never rebases,
    merges non-fast-forward, or touches uncommitted work.
    """
    if not worktree:
        return None
    path = Path(worktree)
    if not (path / ".git").exists() or not path.is_dir():
        return f"{path} is not a checkout"
    try:
        run(["git", "fetch", "--prune", "--quiet"], cwd=path, retries=3)
    except GhError as exc:
        return f"fetch failed: {exc}"
    try:
        if is_dirty(path):
            return "uncommitted changes; fetched but did not fast-forward"
        run(
            ["git", "merge", "--ff-only", "--quiet", "@{upstream}"], cwd=path, retries=1
        )
    except GhError:
        return "fetched, but could not fast-forward (no upstream, or diverged)"
    return None


def pr_reviews(pr_ref: str) -> list[dict]:
    repo, number = split_ref(pr_ref)
    raw = (
        gh_json(["gh", "api", f"repos/{repo}/pulls/{number}/reviews", "--paginate"])
        or []
    )
    return [
        {
            "id": str(item["id"]),
            "author": (item.get("user") or {}).get("login", ""),
            "body": item.get("body") or "",
            "submitted_at": item.get("submitted_at") or item.get("created_at") or "",
        }
        for item in raw
    ]


def pr_comments(pr_ref: str) -> list[dict]:
    repo, number = split_ref(pr_ref)
    raw = (
        gh_json(["gh", "api", f"repos/{repo}/issues/{number}/comments", "--paginate"])
        or []
    )
    return [
        {
            "id": str(item["id"]),
            "author": (item.get("user") or {}).get("login", ""),
            "body": item.get("body") or "",
            "submitted_at": item.get("created_at") or "",
        }
        for item in raw
    ]
