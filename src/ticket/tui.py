from __future__ import annotations

from collections import Counter

from .config import Config
from .resolve import active_pr, next_action
from .store import Store

FILTERS = ("all", "ready", "blocked", "completed")


def _inner_cfg(cfg: Config, ticket: dict) -> Config:
    return cfg.for_repo(ticket.get("repo", ""))


def _row(cfg: Config, store: Store, ticket: dict) -> dict:
    inner = _inner_cfg(cfg, ticket)
    pr_ref = active_pr(ticket)
    pr = (store.read_pr(pr_ref, ticket["key"]) or {}) if pr_ref else None
    findings = store.read_findings(pr_ref, ticket["key"]) if pr_ref else None
    action = next_action(inner, ticket, pr, findings)
    open_count = len(
        [f for f in (findings or {}).get("findings") or [] if f.get("status") == "open"]
    )
    active_target = action.target if action.kind in {"step", "gate", "review"} else None

    dispatched = Counter(d["review"] for d in (pr or {}).get("dispatched") or [])
    collected = Counter(c["review"] for c in (pr or {}).get("collected") or [])
    skipped_reviews = set((pr or {}).get("skipped") or [])

    stages: list[dict] = []
    for step in inner.steps:
        raw = ((ticket.get("steps") or {}).get(step.id) or {}).get("status")
        if raw in {"done", "released"}:
            status = "done"
        elif raw == "skipped":
            status = "skipped"
        elif raw == "failed":
            status = "failed"
        elif active_target == step.id:
            status = "active"
        else:
            status = "pending"
        stages.append({"id": step.id, "kind": step.kind, "status": status})

    for review in inner.reviews:
        if review.id in skipped_reviews:
            status = "skipped"
        elif collected[review.id]:
            status = "done"
        elif dispatched[review.id] > collected[review.id] or active_target == review.id:
            status = "active"
        else:
            status = "pending"
        stages.append({"id": review.id, "kind": "review", "status": status})

    if action.kind == "rest":
        category = "completed"
    elif action.kind == "collect":
        category = "blocked"
    else:
        category = "ready"

    return {
        "key": ticket["key"],
        "repo": ticket.get("repo", ""),
        "summary": ticket.get("summary", ""),
        "pr": pr_ref,
        "next": {"kind": action.kind, "target": action.target, "reason": action.reason},
        "open_findings": open_count,
        "category": category,
        "stages": stages,
    }


def rows(cfg: Config, store: Store) -> list[dict]:
    tickets = [t for t in store.list_tickets() if t.get("tracked")]
    return [_row(cfg, store, ticket) for ticket in tickets]


def filtered(
    rows: list[dict], filter_name: str = "all", search: str = ""
) -> list[dict]:
    search_text = search.strip().lower()
    kept = rows
    if filter_name != "all":
        kept = [row for row in kept if row["category"] == filter_name]
    if not search_text:
        return kept
    return [
        row
        for row in kept
        if search_text in row["key"].lower()
        or search_text in row["repo"].lower()
        or search_text in row["summary"].lower()
        or any(search_text in stage["id"].lower() for stage in row["stages"])
    ]


def render_snapshot(
    cfg: Config,
    store: Store,
    *,
    filter_name: str = "all",
    search: str = "",
    selected: int = 0,
    ascii_only: bool = False,
) -> str:
    all_rows = filtered(rows(cfg, store), filter_name=filter_name, search=search)
    if selected >= len(all_rows):
        selected = max(0, len(all_rows) - 1)

    stage_markers = {
        "done": "[x]" if ascii_only else "[✓]",
        "active": "[>]" if ascii_only else "[▶]",
        "pending": "[ ]",
        "skipped": "[-]",
        "failed": "[!]",
    }
    category_text = {
        "ready": "READY",
        "blocked": "BLOCKED",
        "completed": "DONE",
    }

    lines = [
        f"ticket tui  Filter: {filter_name}  Search: {search or '-'}",
        "TICKETS",
    ]
    for index, row in enumerate(all_rows):
        prefix = ">" if index == selected else " "
        target = f" {row['next']['target']}" if row["next"]["target"] else ""
        findings = f"  {row['open_findings']} open" if row["open_findings"] else ""
        lines.append(
            f"{prefix} {row['key']} [{category_text[row['category']]}] "
            f"{row['next']['kind']}{target}{findings}"
        )
    if not all_rows:
        lines.append("  No tracked tickets match the current filter.")

    lines.append("DETAILS")
    if not all_rows:
        lines.extend(
            [
                "Issue Key: -",
                "Status: -",
                "Stages:",
                "  [ ] Track a ticket with: ticket track <KEY> --repo <owner/repo>",
            ]
        )
    else:
        row = all_rows[selected]
        lines.append(f"Issue Key: {row['key']}")
        lines.append(f"Repo: {row['repo'] or '-'}")
        if row["summary"]:
            lines.append(f"Summary: {row['summary']}")
        if row["pr"]:
            lines.append(f"PR: {row['pr']}")
        lines.append(f"Status: {category_text[row['category']]}")
        lines.append(
            f"Next: {row['next']['kind']} {row['next']['target'] or ''}".rstrip()
        )
        lines.append(f"Reason: {row['next']['reason']}")
        lines.append("Stages:")
        for number, stage in enumerate(row["stages"], start=1):
            lines.append(
                f"  {stage_markers[stage['status']]} {number}. {stage['id']} ({stage['kind']})"
            )
    lines.append(
        "Keys: [j/k] move  [s] next stage  [1-9] run stage  [/] search  [b] ready  [t] track  [o] open  [R] refresh  [q] quit"
    )
    return "\n".join(lines)
