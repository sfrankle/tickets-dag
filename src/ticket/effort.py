"""Estimate a finding's effort with Haiku, at ingestion (decision #16).

`effort` answers "who can fix this": `easy` means the gh Claude bot can do it
from an /edit comment; `hard` means it needs a local Claude session. It is a
question about effort and containment.

Severity is deliberately NOT an input. Severity answers "how important is this",
and the two are orthogonal: a blocking finding can be a one-line fix and an
architecture note can be a rewrite. Do not add severity to the prompt.

Failure is soft: a finding we cannot estimate gets effort None, the resolver
offers it last, and `fix` refuses to route it until `ticket effort` sets one
by hand.
"""

from __future__ import annotations

import json
import subprocess

from .config import Config
from .parse import loads_loose

EFFORTS = ("easy", "hard")

PROMPT = """For each numbered item below, answer with how contained the fix is.

"easy"  - a small, mechanical, self-contained edit an automated agent can make
          without judgement: a wording fix, a missing test case, a renamed
          symbol, a docs correction.
"hard"  - anything needing judgement, spanning several files, changing
          behaviour, or requiring a design decision.

Judge only the size and containment of the fix. Do not judge how important it
is; that is tracked separately.

Return ONLY a JSON array of exactly {count} strings, each "easy" or "hard", in
the same order as the items. No commentary, no code fence.

ITEMS:
{items}
"""


def _items(findings: list[dict]) -> str:
    lines = []
    for index, finding in enumerate(findings, start=1):
        summary = (finding.get("summary") or "").strip()
        body = (finding.get("body") or "").strip()
        # Known limitation: a finding with no `file` (e.g. from a source
        # FILE_RE could not match) silently gets this placeholder rather than
        # being flagged, so the effort prompt sees "unknown file" verbatim.
        path = finding.get("file") or "unknown file"
        lines.append(f"{index}. [{path}] {summary}\n{body}\n")
    return "\n".join(lines)


def assign_effort(cfg: Config, findings: list[dict]) -> list[dict]:
    if not findings:
        return findings

    prompt = PROMPT.format(count=len(findings), items=_items(findings))
    try:
        completed = subprocess.run(
            ["claude", "-p", "--model", cfg.model_id("haiku")],
            input=prompt,  # stdin, not argv — decision #21
            capture_output=True,
            text=True,
            check=False,
        )
        answers = loads_loose(completed.stdout) if completed.returncode == 0 else None
    except (OSError, json.JSONDecodeError):
        answers = None

    if not isinstance(answers, list) or len(answers) != len(findings):
        answers = [None] * len(findings)

    for finding, answer in zip(findings, answers, strict=True):
        finding["effort"] = answer if answer in EFFORTS else None
    return findings
