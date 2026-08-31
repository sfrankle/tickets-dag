"""Review body -> findings. Script path plus Haiku fallback.

The AI reviews we trigger emit a known format: one `<details>` block per
severity keyed by the emoji in `<summary>`, top-level `*` bullets as findings,
and a `**Verdict:**` line at the end. A script handles that. Anything else — a
human comment, another bot, format drift — goes to Haiku. Cost is therefore
zero on the common path and small on the uncommon one.

This module never sets `effort`. Severity says how important a finding is;
effort says how contained the fix is. See effort.py.
"""

from __future__ import annotations

import json
import re
import subprocess

from .config import Config
from .errors import TicketError

SEVERITIES = {"🔴": "blocking", "🟡": "maintenance", "🔵": "architecture"}
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def loads_loose(raw: str):
    """`json.loads`, tolerating the code fence models wrap JSON in.

    Asking for "ONLY a JSON array" gets a fenced array often enough that not
    handling it means routine, avoidable failures.
    """
    match = FENCE_RE.match(raw.strip())
    return json.loads(match.group(1) if match else raw.strip())


DEFAULT_SEVERITY = "maintenance"

DETAILS_RE = re.compile(
    r"<details>\s*<summary>(?P<summary>.*?)</summary>(?P<body>.*?)</details>",
    re.DOTALL,
)
VERDICT_RE = re.compile(r"^\*\*Verdict:\*\*", re.MULTILINE)
EMPTY_RE = re.compile(r"^\s*(None\.?|No findings\.?)\s*$", re.IGNORECASE | re.MULTILINE)
# Known limitation: this can match a non-file backtick-quoted token that
# happens to look like `word.ext` (e.g. a version string like `v1.2`) in
# unusual review text. Low consequence — a wrong `file` value only degrades
# the effort prompt/edit hint, nothing structural.
FILE_RE = re.compile(r"`([^`\s]+\.[A-Za-z0-9]+)`")

HAIKU_PROMPT = """Split the following code review into individual findings.

Return ONLY a JSON array. Each element must be an object with these keys:
  "severity": one of "blocking", "maintenance", "architecture"
  "summary":  one short line naming the problem
  "body":     the full text of the finding
  "file":     the file path the finding is about, or null

Do not add commentary. Do not wrap the JSON in a code fence.

REVIEW:
"""


def _bullets(block: str) -> list[str]:
    """Top-level `*` bullets, with their continuation lines folded in."""
    if EMPTY_RE.search(block):
        return []
    bullets: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if re.match(r"^\s{0,3}\*\s+", line):
            if current:
                bullets.append("\n".join(current).strip())
            current = [re.sub(r"^\s{0,3}\*\s+", "", line)]
        elif current is not None and line.strip():
            current.append(line.strip())
        elif current is not None and not line.strip():
            bullets.append("\n".join(current).strip())
            current = None
    if current:
        bullets.append("\n".join(current).strip())
    return [b for b in bullets if b]


def _file_of(text: str) -> str | None:
    match = FILE_RE.search(text)
    return match.group(1) if match else None


def parse_script(body: str) -> list[dict] | None:
    blocks = list(DETAILS_RE.finditer(body))
    recognised = [
        m for m in blocks if any(emoji in m.group("summary") for emoji in SEVERITIES)
    ]
    if not recognised or not VERDICT_RE.search(body):
        return None

    findings: list[dict] = []
    for match in recognised:
        severity = next(
            name for emoji, name in SEVERITIES.items() if emoji in match.group("summary")
        )
        for bullet in _bullets(match.group("body")):
            findings.append(
                {
                    "severity": severity,
                    "summary": bullet.splitlines()[0].strip(),
                    "body": bullet,
                    "file": _file_of(bullet),
                    "parsed_by": "script",
                }
            )
    return findings


def parse_haiku(cfg: Config, body: str) -> list[dict]:
    model = cfg.model_id("haiku")
    completed = subprocess.run(
        ["claude", "-p", "--model", model],
        input=HAIKU_PROMPT + body,     # stdin, not argv — decision #21
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise TicketError(f"haiku parse failed: {completed.stderr.strip()}")
    try:
        raw = loads_loose(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TicketError(f"haiku did not return JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise TicketError("haiku did not return JSON: expected an array")

    findings = []
    for item in raw:
        severity = item.get("severity")
        findings.append(
            {
                "severity": severity if severity in SEVERITIES.values() else DEFAULT_SEVERITY,
                "summary": (item.get("summary") or "").strip(),
                "body": item.get("body") or item.get("summary") or "",
                "file": item.get("file") or None,
                "parsed_by": "haiku",
            }
        )
    return findings


def parse(cfg: Config, body: str) -> list[dict]:
    findings = parse_script(body)
    if findings is not None:
        return findings
    return parse_haiku(cfg, body)
