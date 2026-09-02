"""Review body -> findings. Script path plus Haiku fallback.

The AI reviews we trigger emit a known shape: one `<details>` block per severity keyed by its configured marker in `<summary>`, findings inside it, and a `**Verdict:**` line at the end.
A script handles that.
Anything else — a human comment, another bot, format drift — goes to Haiku.
Cost is therefore zero on the common path and small on the uncommon one.

The built-in grammar is deliberately wide, because "our format" is a shape several bots land on rather than one bot's exact bytes: `<details open>` is still a details block, and a finding is either a `*` bullet or a paragraph that opens with a bolded lead.
A trailing summary table is not a finding.
Anything a config could express in a regex, the built-in tries to handle first — `parse.sources` in config is an escape hatch for a bot that writes something genuinely different, not the primary path.

Recognised-but-empty and not-our-format are different answers and are returned as such (see `ScriptParse`): a review that says "None." in every section is ours with zero findings, while a review whose sections we cannot split is not ours and belongs to Haiku.
Collapsing the two is what records a source with zero findings and never asks Haiku.

This module never sets `effort`. Severity says how important a finding is;
effort says how contained the fix is. See effort.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from functools import cache

from .config import Config
from .errors import TicketError

FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def loads_loose(raw: str):
    """`json.loads`, tolerating the code fence models wrap JSON in.

    Asking for "ONLY a JSON array" gets a fenced array often enough that not
    handling it means routine, avoidable failures.
    """
    match = FENCE_RE.match(raw.strip())
    return json.loads(match.group(1) if match else raw.strip())


# `<details open>`, `<details class="...">` and `<DETAILS>` are all the same block.
# Matching the literal tag lost a whole severity section in the wild.
DETAILS_RE = re.compile(
    r"<details[^>]*>\s*<summary[^>]*>(?P<summary>.*?)</summary>"
    r"(?P<body>.*?)</details>",
    re.DOTALL | re.IGNORECASE,
)
VERDICT_RE = re.compile(r"^\*\*Verdict:\*\*", re.MULTILINE)
EMPTY_RE = re.compile(r"^\s*(None\.?|No findings\.?)\s*$", re.IGNORECASE | re.MULTILINE)
# A finding starts either as a list item or as a paragraph opening with a bolded lead — `**Title - summary** (`path`): text` is one finding, and the `*` bullet rule reads its first asterisk and then fails on the second.
BULLET_RE = re.compile(r"^\s{0,3}[*+-]\s+")
LEAD_RE = re.compile(r"^\s{0,3}\*\*\S")
# A summary table at the end of a review counts findings, it does not make them.
# Its rows must never become findings of their own.
TABLE_RE = re.compile(r"^\s{0,3}\|")
# A fenced block belongs to the finding it sits in, whatever its lines start with.
# Without this, every `-` and `+` line of a suggested diff is read as a bullet.
FENCE_RE_LINE = re.compile(r"^\s{0,3}(?:```|~~~)")
# An indented line after a blank one is the rest of the list item above it, not a new paragraph.
INDENT_RE = re.compile(r"^\s{2,}\S")

MAX_SUMMARY = 120
# Below this, a bolded lead is a location (`**Foo.kt:43**`) rather than a statement of the problem, so the sentence after it is folded in.
SHORT_LEAD = 32
BOLD_LEAD_RE = re.compile(r"^\*\*(?P<lead>[^*].*?)\*\*")
# A sentence ends at .!? followed by space and something that starts a new sentence.
# Requiring the capital is what keeps `retry.py — retries ...` and `Foo.kt:43` from being read as two sentences.
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
# Hyphen, en dash, em dash.
# Spelled by codepoint: all three turn up in review prose, and two of them are invisible next to each other in source.
DASHES = "-" + chr(0x2013) + chr(0x2014)
# What sits between a bolded lead and the sentence after it: an optional parenthesised path, then punctuation.
LEAD_JUNK_RE = re.compile(r"^\s*(?:\([^)]*\))?\s*[:;," + re.escape(DASHES) + r"]*\s*")
LEAD_TRAIL = " :;," + DASHES

# A path in parentheses right after the lead is the format telling us the file outright, so it wins over anything else quoted in the prose.
PAREN_PATH_RE = re.compile(r"\(\s*(?:in\s+|at\s+|see\s+)?`([^`]+)`\s*\)")
BACKTICK_RE = re.compile(r"`([^`]+)`")
LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
# `logger.warn` looks exactly like `README.md` to a `word.word` rule, and the store filled up with the former.
# A token earns "path" by carrying a directory separator or by ending in an extension people actually name files with.
# Anything else records no file, which is better than a wrong one.
_PATH_EXTENSIONS = """
    bash bat bzl c cc cfg clj cljs cmake conf cpp cs css csv cxx dart edn ex
    exs erl fish go gradle graphql groovy h haml hbs hcl hpp hrl hs htm html
    ini ipynb java jl js json json5 jsx kt kts less lock lua m md mdx mjs mk
    mm nim nix php pl pp proto ps1 psm1 py pyi r rb re rs rst sass sbt scala
    scm scss sh sql svelte svg swift tf tfvars toml ts tsv tsx txt vue xml yml
    yaml zig zsh
"""
PATH_EXTENSIONS = frozenset(_PATH_EXTENSIONS.split())

HAIKU_PROMPT = """Split the following code review into individual findings.

Return ONLY a JSON array. Each element must be an object with these keys:
  "severity": one of {severities}
  "summary":  one short line naming the problem
  "body":     the full text of the finding
  "file":     the file path the finding is about, or null

Do not add commentary. Do not wrap the JSON in a code fence.

REVIEW:
"""


def haiku_prompt(cfg: Config) -> str:
    """The parse prompt, naming this config's severities rather than a fixed set."""
    names = ", ".join(f'"{name}"' for name in cfg.severity_ids())
    return HAIKU_PROMPT.format(severities=names)


@dataclass(frozen=True)
class Grammar:
    """The patterns one source's findings are read with.

    The built-in is `BUILTIN`.
    A `parse.sources` profile replaces only the patterns it names, so an override of `details:` still gets the built-in bullet, lead and file rules.
    """

    details: re.Pattern
    bullet: re.Pattern
    lead: re.Pattern
    verdict: re.Pattern
    empty: re.Pattern
    file: re.Pattern | None = None


BUILTIN = Grammar(
    details=DETAILS_RE,
    bullet=BULLET_RE,
    lead=LEAD_RE,
    verdict=VERDICT_RE,
    empty=EMPTY_RE,
)

_FLAGS = {
    "details": re.DOTALL | re.IGNORECASE,
    "bullet": re.MULTILINE,
    "lead": re.MULTILINE,
    "verdict": re.MULTILINE,
    "file": 0,
}


@cache
def _compiled(pattern: str, flags: int) -> re.Pattern:
    return re.compile(pattern, flags)


def grammar_for(cfg: Config, author: str | None) -> Grammar:
    """The built-in grammar, or an author's `parse.sources` override of it."""
    profile = cfg.parse_source(author)
    if profile is None:
        return BUILTIN
    overrides = {
        name: _compiled(pattern, _FLAGS[name])
        for name in _FLAGS
        if (pattern := getattr(profile, name)) is not None
    }
    return replace(BUILTIN, **overrides)


@dataclass(frozen=True)
class ScriptParse:
    """What the script parser made of a body.

    `recognised` is the question `parse` and `collect` actually ask: an unrecognised body goes to Haiku, a recognised one is ours even when `findings` is empty because every section said "None."
    """

    recognised: bool
    findings: list[dict]


def _cap(text: str, limit: int = MAX_SUMMARY) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def _first_sentence(text: str) -> str:
    match = SENTENCE_RE.search(text)
    return text[: match.start()].strip() if match else text.strip()


def _summarise(text: str) -> str:
    """One short line naming the problem, derived from the finding.

    Slicing the first physical line made `summary` a copy of `body` for any format that writes a titled paragraph.
    These formats already put the short version in a bolded lead, so lift that; otherwise cut at the first sentence.
    Either way the result is capped — `ticket findings` prints it as a padded row, and `_fingerprint` dedupes on it.
    """
    flat = " ".join(text.split())
    if not flat:
        return ""
    match = BOLD_LEAD_RE.match(flat)
    if not match:
        return _cap(_first_sentence(flat))
    lead = match.group("lead").strip().rstrip(LEAD_TRAIL).strip()
    rest = LEAD_JUNK_RE.sub("", flat[match.end() :])
    if not lead:
        return _cap(_first_sentence(rest or flat))
    if len(lead) >= SHORT_LEAD or not rest:
        return _cap(lead)
    return _cap(f"{lead}: {_first_sentence(rest)}")


def _as_path(raw: str) -> str | None:
    """`raw` as a file path, or None if it is a symbol rather than a path."""
    token = raw.strip().strip("\"'").rstrip(".,;:")
    token = LINE_SUFFIX_RE.sub("", token)
    if not token or any(c in token for c in " \t()[]{}<>\"'`"):
        return None
    if token.endswith("/"):
        return None
    if "/" in token:
        return token
    _, dot, extension = token.rpartition(".")
    if not dot or not extension:
        return None
    return token if extension.lower() in PATH_EXTENSIONS else None


def _file_of(text: str, pattern: re.Pattern | None = None) -> str | None:
    if pattern is not None:
        match = pattern.search(text)
        return (match.group(1) if match.groups() else match.group(0)) if match else None
    for candidates in (PAREN_PATH_RE.finditer(text), BACKTICK_RE.finditer(text)):
        paths = [path for match in candidates if (path := _as_path(match.group(1)))]
        if not paths:
            continue
        # Within one tier, a token carrying a directory separator is the file being reported; a bare `README.md` is usually prose naming a file.
        return next((p for p in paths if "/" in p or "\\" in p), paths[0])
    return None


def _has_content(block: str, grammar: Grammar) -> bool:
    """Anything in this section that a finding could have been made out of.

    Table rows count here even though `_units` never makes findings out of them: a section written entirely as a table is one we have no rule for, not one that is empty.
    Only `grammar.empty` — "None." — makes a section genuinely empty.
    """
    if grammar.empty.search(block):
        return False
    return any(line.strip() for line in block.splitlines())


def _continues(lines: list[str], index: int) -> bool:
    """Whether the blank line at `index` sits inside a list item rather than after it."""
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        return bool(INDENT_RE.match(line)) and not TABLE_RE.match(line)
    return False


def _units(block: str, grammar: Grammar) -> list[str]:
    """One entry per finding: a bullet or a lead-bolded paragraph, folded.

    A paragraph that opens with neither is prose about the section rather than a finding, and is skipped along with its continuation lines — except a bolded lead, which starts a finding whatever preceded it.
    Table rows are skipped outright.
    Fenced blocks are held whole: a suggested diff is part of its finding, not a run of bullets.
    """
    if grammar.empty.search(block):
        return []
    units: list[str] = []
    current: list[str] | None = None
    fenced = False

    def flush() -> None:
        nonlocal current
        if current:
            text = "\n".join(current).strip()
            if text:
                units.append(text)
        current = None

    lines = block.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        fence = bool(FENCE_RE_LINE.match(line))
        if fence:
            fenced = not fenced
        if fence or fenced:
            if current is not None:
                # Kept as written: indentation is the point of a fenced block.
                current.append(line.rstrip())
            continue
        if not stripped or TABLE_RE.match(line):
            if not stripped and current is not None and _continues(lines, index):
                current.append("")
                continue
            flush()
            continue
        bullet = grammar.bullet.match(line)
        if bullet:
            flush()
            current = [line[bullet.end() :].strip()]
            continue
        if current is not None:
            current.append(stripped)
            continue
        if grammar.lead.match(line):
            current = [stripped]
    flush()
    return units


def script_parse(cfg: Config, body: str, author: str | None = None) -> ScriptParse:
    grammar = grammar_for(cfg, author)
    sections = [
        (match, severity)
        for match in grammar.details.finditer(body)
        if (severity := cfg.severity_for_marker(match.group("summary"))) is not None
    ]
    if not sections or not grammar.verdict.search(body):
        return ScriptParse(False, [])

    findings: list[dict] = []
    for match, severity in sections:
        section = match.group("body")
        units = _units(section, grammar)
        if not units and _has_content(section, grammar):
            # Our markers and our verdict, but a section we have no rule for.
            # Handing that to Haiku is the whole point of having a fallback; calling it ours with zero findings loses the review silently.
            return ScriptParse(False, [])
        for unit in units:
            findings.append(
                {
                    "severity": severity,
                    "summary": _summarise(unit),
                    "body": unit,
                    "file": _file_of(unit, grammar.file),
                    "parsed_by": "script",
                }
            )
    return ScriptParse(True, findings)


def parse_script(
    cfg: Config, body: str, author: str | None = None
) -> list[dict] | None:
    """The findings in `body`, or None if this is not a format we know.

    None means "ask Haiku"; `[]` means "ours, and it found nothing".
    """
    result = script_parse(cfg, body, author)
    return result.findings if result.recognised else None


def parse_haiku(cfg: Config, body: str) -> list[dict]:
    model = cfg.model_id("haiku")
    completed = subprocess.run(
        ["claude", "-p", "--model", model],
        input=haiku_prompt(cfg) + body,  # stdin, not argv — decision #21
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TicketError(f"haiku parse failed: {completed.stderr.strip()}")
    try:
        raw = loads_loose(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TicketError(f"haiku did not return JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise TicketError("haiku did not return JSON: expected an array")

    known = set(cfg.severity_ids())
    findings = []
    for item in raw:
        severity = item.get("severity")
        findings.append(
            {
                "severity": severity if severity in known else cfg.default_severity,
                "summary": (item.get("summary") or "").strip(),
                "body": item.get("body") or item.get("summary") or "",
                "file": item.get("file") or None,
                "parsed_by": "haiku",
            }
        )
    return findings


def parse(cfg: Config, body: str, author: str | None = None) -> list[dict]:
    result = script_parse(cfg, body, author)
    if result.recognised:
        return result.findings
    return parse_haiku(cfg, body)
