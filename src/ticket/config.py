"""Load and validate config. Resolve repo overrides and model aliases.

The config file is central and serves every repo; per-repo differences are
overrides under `repos:`, never separate files. Overrides are shallow:
`reviews:` replaces the list, `steps.skip:` subtracts from it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from .errors import ConfigError

DEFAULT_CONFIG = Path("~/.ticket/config.yml")
DEFAULT_WORKTREE_ROOT = Path("~/worktrees")

# The house style this tool grew up with, kept as the default so a config
# that says nothing keeps working. It is an example, not the engine's
# opinion: `severities:` replaces it wholesale.
# Every key each block understands.
# A key outside its set is a typo, and a typo under a known block used to load clean and do nothing (issues #6, #8).
TOP_LEVEL_KEYS = {
    "store",
    "models",
    "defaults",
    "sync",
    "tracker",
    "key_pattern",
    "severities",
    "worktrees",
    "steps",
    "reviews",
    "fix",
    "parse",
    "repos",
}
DEFAULTS_KEYS = {"model"}
WORKTREES_KEYS = {"enabled", "root", "branch"}
TRACKER_KEYS = {"summary"}
FIX_KEYS = {"model", "args"}
STEP_KEYS = {"id", "run", "gate", "prompt", "model", "needs", "args"}
REVIEW_KEYS = {"id", "order", "dispatch", "prompt", "model", "args"}
SEVERITY_KEYS = {"id", "marker", "default"}
REPO_KEYS = {"path", "reviews", "steps"}
REPO_STEPS_KEYS = {"skip"}

DEFAULT_SEVERITIES = (
    {"id": "blocking", "marker": "\U0001f534"},
    {"id": "maintenance", "marker": "\U0001f7e1", "default": True},
    {"id": "architecture", "marker": "\U0001f535"},
)


def _anchor(raw: str, root: Path) -> Path:
    """Resolve a path out of the config: `~` expands, relative anchors to `root`.

    `root` is the directory the config file was loaded from, so every path in
    the file means the same thing no matter which directory `ticket` was run
    in. Used for `store:`, `worktrees.root:`, `prompt:`, and `run:`.
    """
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _reject_unknown(where: str, raw: dict, allowed: set[str]) -> None:
    """Fail on a key this loader does not know (issues #6, #8).

    A dropped key is worse than a rejected one: `tracker: {sumary: ...}` looks configured, loads clean, and does nothing.
    Every block validates its own keys, so the message names the block and what it could have meant.
    """
    # str() first: YAML 1.1 reads a bare `no:` as the boolean False, and a
    # mixed str/bool key list is unsortable — a traceback out of the one verb
    # whose job is to report bad config instead of raising.
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown key{'s' if len(unknown) > 1 else ''} under {where}: "
            f"{', '.join(str(k) for k in unknown)}. Known: {', '.join(sorted(allowed))}"
        )


@dataclass(frozen=True)
class Severity:
    """One name a finding can carry, and the marker a review writes it as.

    `marker` is what the script parser looks for in a review's section
    heading; a severity without one is still reachable from a model-parsed
    review, just not from the script path. Config order is the order
    findings are listed in, so the most important severity goes first.
    """

    id: str
    marker: str | None = None
    default: bool = False


@dataclass(frozen=True)
class Worktrees:
    """Whether a ticket gets its own worktree, and where it goes.

    `enabled: false` means work happens in the repo clone itself on a branch,
    which is what a repo that cannot support worktrees needs. Either way the
    checkout the engine works in is recorded as `ticket["worktree"]`.
    """

    enabled: bool = True
    root: Path = DEFAULT_WORKTREE_ROOT
    branch: str = "{key}"

    def branch_for(self, key: str, repo: str) -> str:
        owner, _, name = repo.partition("/")
        return self.branch.format(key=key, repo=repo, owner=owner, name=name)

    def path_for(self, key: str) -> Path:
        # `root` is already expanded and anchored by `load_config`.
        return self.root / key


@dataclass(frozen=True)
class Step:
    id: str
    kind: str
    run: str | None = None
    model: str | None = None
    prompt: str | None = None
    needs: tuple[str, ...] = ()
    args: tuple[str, ...] = ()  # extra argv for a handoff, e.g. agent mode


@dataclass(frozen=True)
class Review:
    id: str
    order: int
    dispatch: str
    prompt: str
    model: str | None = None
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fix:
    """How a `hard` fix runs its local session.

    Separate from any step: a fix is not in the DAG, but it is a handoff that
    has to write files, so it needs the same model and argv knobs — agent mode
    above all. Keying it off a step id would put a step name back in the
    engine.
    """

    model: str | None = None
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParseSource:
    """One author's override of the built-in finding grammar.

    The escape hatch, not the road: `parse.py` ships a grammar wide enough for the shapes review bots actually write, and a profile exists for the bot that writes something genuinely different — so a new one is onboarded without waiting for a release.
    Every pattern is optional and replaces only itself; regexes in YAML rot, so the fewer of these a config carries, the better.
    Patterns are compiled here, so a typo fails at load and `parse.py` is handed the compiled form rather than compiling it again per review.
    """

    author: str
    details: re.Pattern | None = None
    bullet: re.Pattern | None = None
    lead: re.Pattern | None = None
    file: re.Pattern | None = None
    verdict: re.Pattern | None = None


@dataclass(frozen=True)
class Tracker:
    """How to ask an external tracker for a ticket's summary, if at all.

    One command, not an integration. The tool never learns what a tracker is:
    it runs the argv it is given, substitutes `{key}`, and keeps the first line
    of stdout. Unset means the summary is whatever `ticket track` recorded.
    """

    summary: tuple[str, ...] = ()

    def summary_argv(self, key: str) -> list[str]:
        return [part.format(key=key) for part in self.summary]


@dataclass(frozen=True)
class Config:
    store: Path  # where state lives; defaults to `root` (decision #24)
    root: Path  # the config file's directory; every relative path anchors here
    models: dict[str, str]
    default_model: str
    steps: tuple[Step, ...]
    reviews: tuple[Review, ...]
    repos: dict[str, dict]
    worktrees: Worktrees
    sync: bool
    severities: tuple[Severity, ...]
    fix: Fix = Fix()
    tracker: Tracker = Tracker()
    key_pattern: str | None = None
    parse_sources: tuple[ParseSource, ...] = ()

    def model_id(self, alias: str) -> str:
        try:
            return self.models[alias]
        except KeyError:
            raise ConfigError(
                f"unknown model alias: {alias}. Known: {', '.join(sorted(self.models))}"
            ) from None

    @property
    def default_severity(self) -> str:
        return next(s.id for s in self.severities if s.default)

    def severity_ids(self) -> list[str]:
        return [s.id for s in self.severities]

    def severity_for_marker(self, text: str) -> str | None:
        """The severity whose marker appears in `text`, if any."""
        for severity in self.severities:
            if severity.marker and severity.marker in text:
                return severity.id
        return None

    def parse_source(self, author: str | None) -> ParseSource | None:
        """This author's grammar override, if the config declares one."""
        if not author:
            return None
        for profile in self.parse_sources:
            if profile.author.lower() == author.lower():
                return profile
        return None

    def severity_rank(self, severity_id: str | None) -> int:
        """Sort key. Config order is the order; anything unknown sorts last."""
        for index, severity in enumerate(self.severities):
            if severity.id == severity_id:
                return index
        return len(self.severities)

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ConfigError(f"unknown step: {step_id}")

    def review(self, review_id: str) -> Review:
        for review in self.reviews:
            if review.id == review_id:
                return review
        raise ConfigError(f"unknown review: {review_id}")

    def path_to(self, relative: str) -> Path:
        """Resolve a `run:` or `prompt:` path against the config's directory."""
        return _anchor(relative, self.root)

    def repo_path(self, repo: str) -> Path | None:
        """The clone a worktree is added from, or worked in directly."""
        raw = (self.repos.get(repo) or {}).get("path")
        return _anchor(raw, self.root) if raw else None

    def for_repo(self, repo: str) -> Config:
        override = self.repos.get(repo)
        if not override:
            return self
        reviews = self.reviews
        if "reviews" in override:
            wanted = list(override["reviews"] or [])
            known = {r.id for r in self.reviews}
            for name in wanted:
                if name not in known:
                    raise ConfigError(
                        f"repo {repo} names review {name}, which is not in reviews:"
                    )
            reviews = tuple(r for r in self.reviews if r.id in wanted)
        steps = self.steps
        skip = list((override.get("steps") or {}).get("skip") or [])
        if skip:
            known = {s.id for s in self.steps}
            for name in skip:
                if name not in known:
                    raise ConfigError(
                        f"repo {repo} skips step {name}, which is not in steps:"
                    )
            steps = tuple(s for s in self.steps if s.id not in skip)
            # A config-level skip removes the step outright, so a dependent
            # would wait on a step that can never be satisfied and the row
            # would go silently to rest. `ticket skip` is the other thing:
            # it marks a step satisfied. Refuse the stranding case here.
            remaining = {s.id for s in steps}
            for step in steps:
                for need in step.needs:
                    if need not in remaining:
                        raise ConfigError(
                            f"repo {repo} skips {need}, but {step.id} needs it. "
                            f"Skip {step.id} too, or use `ticket skip` per ticket."
                        )
        return replace(self, steps=steps, reviews=reviews)


def config_path() -> Path:
    override = os.environ.get("TICKET_CONFIG")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG.expanduser()


def _step_kind(raw: dict) -> str:
    if raw.get("gate"):
        return "gate"
    if raw.get("run"):
        return "script"
    if raw.get("prompt"):
        return "handoff"
    raise ConfigError(
        f"step {raw.get('id')!r} has no kind: expected one of run:, gate:, prompt:"
    )


def _load_steps(raw_steps: list, default_model: str) -> tuple[Step, ...]:
    steps: list[Step] = []
    seen: set[str] = set()
    for raw in raw_steps:
        step_id = raw.get("id")
        if not step_id:
            raise ConfigError("every step needs an id")
        if step_id in seen:
            raise ConfigError(f"duplicate step id: {step_id}")
        _reject_unknown(f"step {step_id}", raw, STEP_KEYS)
        seen.add(step_id)
        kind = _step_kind(raw)
        model = raw.get("model") or (default_model if kind == "handoff" else None)
        if kind == "handoff" and not model:
            raise ConfigError(
                f"step {step_id} is a handoff with no model: and no defaults.model"
            )
        steps.append(
            Step(
                id=step_id,
                kind=kind,
                run=raw.get("run"),
                prompt=raw.get("prompt"),
                model=model,
                needs=tuple(raw.get("needs") or ()),
                args=tuple(raw.get("args") or ()),
            )
        )
    for step in steps:
        for need in step.needs:
            if need not in seen:
                raise ConfigError(f"step {step.id} needs {need}, which is not a step")
    _check_acyclic(steps)
    return tuple(steps)


def _check_acyclic(steps: list[Step]) -> None:
    """`needs` describes a DAG. A cycle would otherwise show up as `at rest`."""
    needs = {s.id: set(s.needs) for s in steps}
    resolved: set[str] = set()
    while True:
        ready = {sid for sid, deps in needs.items() if deps <= resolved}
        if ready == resolved:
            break
        resolved = ready
    stuck = sorted(set(needs) - resolved)
    if stuck:
        raise ConfigError(f"steps form a cycle in needs: {', '.join(stuck)}")


def _load_reviews(raw_reviews: list, default_model: str | None) -> tuple[Review, ...]:
    reviews: list[Review] = []
    seen: set[str] = set()
    for raw in raw_reviews:
        review_id = raw.get("id")
        if not review_id:
            raise ConfigError("every review needs an id")
        if review_id in seen:
            raise ConfigError(f"duplicate review id: {review_id}")
        _reject_unknown(f"review {review_id}", raw, REVIEW_KEYS)
        seen.add(review_id)
        dispatch = raw.get("dispatch")
        if dispatch not in ("bot", "local"):
            raise ConfigError(
                f"review {review_id} has dispatch {dispatch!r}: expected bot or local"
            )
        if not raw.get("prompt"):
            raise ConfigError(
                f"review {review_id} has no prompt: (both transports need one)"
            )
        if dispatch == "local" and not (raw.get("model") or default_model):
            raise ConfigError(
                f"review {review_id} is dispatch: local with no model: and no defaults.model"
            )
        reviews.append(
            Review(
                id=review_id,
                order=int(raw.get("order", 0)),
                dispatch=dispatch,
                prompt=raw["prompt"],
                model=raw.get("model"),
                args=tuple(raw.get("args") or ()),
            )
        )
    orders = [r.order for r in reviews]
    if len(set(orders)) != len(orders):
        # The resolver offers a review only once every earlier one is done.
        # Equal orders make "earlier" vacuous, so the sequence stops meaning
        # anything. Cheaper to reject than to explain later.
        raise ConfigError(f"reviews have duplicate order: values: {sorted(orders)}")
    return tuple(sorted(reviews, key=lambda r: (r.order, r.id)))


def _load_fix(raw: dict, default_model: str, models: dict) -> Fix:
    if not isinstance(raw, dict):
        raise ConfigError("fix: must be a mapping with model: and/or args:")
    _reject_unknown("fix:", raw, FIX_KEYS)
    model = raw.get("model") or default_model
    if not model:
        raise ConfigError("fix: has no model: and there is no defaults.model")
    # Eagerly, like defaults.model: a typo here should fail at load, not
    # halfway through the one command that spends an Opus session.
    if model not in models:
        raise ConfigError(f"fix.model is {model!r}, which is not in models:")
    return Fix(model=model, args=tuple(raw.get("args") or ()))


def _load_key_pattern(raw) -> str | None:
    """Optional house style for ticket keys, e.g. `^[A-Z]+-[0-9]+$`.

    The engine's own check is a path-safety one (see `cli.is_safe_key`); this
    is the stricter, entirely local opinion a shop with one tracker can add.
    """
    if raw is None:
        return None
    _compile_pattern(raw, 0, "key_pattern")
    return raw


def _compile_pattern(raw, flags: int, label: str) -> re.Pattern:
    """A config value as a compiled regex, or a `ConfigError` naming where it came from."""
    if not isinstance(raw, str):
        raise ConfigError(f"{label} must be a regex string")
    try:
        return re.compile(raw, flags)
    except re.error as exc:
        raise ConfigError(f"{label} is not a valid regex: {exc}") from exc


def _load_severities(raw) -> tuple[Severity, ...]:
    """The severity vocabulary. Omitted means `DEFAULT_SEVERITIES`.

    Order is meaningful and markers must be distinct, because both the script
    parser and `ticket findings` key off them. Exactly one severity carries
    `default: true`: it is what a model-parsed finding falls back to when it
    names a severity this config has never heard of.
    """
    if raw is None:
        raw = list(DEFAULT_SEVERITIES)
    if not isinstance(raw, list) or not raw:
        raise ConfigError("severities: must be a non-empty list")
    severities: list[Severity] = []
    seen: set[str] = set()
    markers: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("every severity must be a mapping with an id:")
        severity_id = item.get("id")
        if not severity_id:
            raise ConfigError("every severity needs an id")
        if severity_id in seen:
            raise ConfigError(f"duplicate severity id: {severity_id}")
        _reject_unknown(f"severity {severity_id}", item, SEVERITY_KEYS)
        seen.add(severity_id)
        marker = item.get("marker")
        if marker is not None:
            marker = str(marker)
            if marker in markers:
                raise ConfigError(f"duplicate severity marker: {marker}")
            markers.add(marker)
        severities.append(
            Severity(
                id=str(severity_id),
                marker=marker,
                default=bool(item.get("default", False)),
            )
        )
    defaults = [s.id for s in severities if s.default]
    if len(defaults) != 1:
        raise ConfigError(
            "severities: needs exactly one entry with default: true "
            f"(found {len(defaults)}). It is the severity a model-parsed "
            "finding falls back to."
        )
    return tuple(severities)


# The overridable patterns, and the flags each is compiled with.
# Compiling here rather than in `parse.py` is what lets a profile be validated at load under exactly the flags it will run under.
# `bullet` and `lead` are matched against one line at a time, so `^` anchors the line with no flag needed.
PARSE_FLAGS = {
    "details": re.DOTALL | re.IGNORECASE,
    "bullet": 0,
    "lead": 0,
    "file": 0,
    "verdict": re.MULTILINE,
}
PARSE_PATTERNS = tuple(PARSE_FLAGS)


def _load_parse_sources(raw) -> tuple[ParseSource, ...]:
    """`parse.sources:`, the optional per-author grammar override.

    Omitted — the normal case — means every source is read with the built-in grammar.
    A profile is only consulted for the author it names, so adding one can never change how anything else parses.
    """
    if not raw:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError("parse: must be a mapping with sources:")
    unknown = sorted(set(raw) - {"sources"})
    if unknown:
        raise ConfigError(f"parse: has unknown key(s): {', '.join(unknown)}")
    items = raw.get("sources")
    if items is None:
        return ()
    if not isinstance(items, list) or not items:
        raise ConfigError("parse.sources must be a non-empty list of source profiles")

    profiles: list[ParseSource] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("every parse source must be a mapping with an author:")
        author = item.get("author")
        if not author:
            raise ConfigError("every parse source needs an author")
        author = str(author)
        if author.lower() in seen:
            raise ConfigError(f"duplicate parse source author: {author}")
        seen.add(author.lower())
        stray = sorted(set(item) - {"author", *PARSE_PATTERNS})
        if stray:
            raise ConfigError(
                f"parse source {author} has unknown key(s): {', '.join(stray)}. "
                f"Known: {', '.join(PARSE_PATTERNS)}"
            )
        patterns: dict[str, re.Pattern] = {}
        for name in PARSE_PATTERNS:
            pattern = item.get(name)
            if pattern is None:
                continue
            compiled = _compile_pattern(
                pattern, PARSE_FLAGS[name], f"parse source {author}: {name}"
            )
            if name == "details" and not {"summary", "body"} <= set(
                compiled.groupindex
            ):
                raise ConfigError(
                    f"parse source {author}: details needs the named groups "
                    "(?P<summary>...) and (?P<body>...)"
                )
            if name == "file" and compiled.groups < 1:
                raise ConfigError(
                    f"parse source {author}: file needs one capturing group "
                    "for the path"
                )
            patterns[name] = compiled
        if not patterns:
            raise ConfigError(
                f"parse source {author} overrides nothing. Give it at least one "
                f"of: {', '.join(PARSE_PATTERNS)}"
            )
        profiles.append(ParseSource(author=author, **patterns))
    return tuple(profiles)


def _load_tracker(raw) -> Tracker:
    if not raw:
        return Tracker()
    if not isinstance(raw, dict):
        raise ConfigError("tracker: must be a mapping with summary:")
    _reject_unknown("tracker:", raw, TRACKER_KEYS)
    summary = raw.get("summary")
    if summary is None:
        return Tracker()
    if not isinstance(summary, list) or not summary:
        raise ConfigError(
            "tracker.summary must be a non-empty list of argv parts, "
            'e.g. [jira, issue, view, "{key}", --plain]'
        )
    return Tracker(summary=tuple(str(part) for part in summary))


def load_config(path: Path | None = None) -> Config:
    path = (path or config_path()).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"no config at {path}. Copy examples/config.yml to get started."
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a YAML mapping")

    _reject_unknown(f"{path}", raw, TOP_LEVEL_KEYS)
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults: must be a mapping with model:")
    _reject_unknown("defaults:", defaults, DEFAULTS_KEYS)

    models = dict(raw.get("models") or {})
    default_model = defaults.get("model")
    if default_model and default_model not in models:
        raise ConfigError(
            f"defaults.model is {default_model!r}, which is not in models:"
        )

    # The store defaults to the directory the config was found in, not to a
    # fixed `~/.ticket`, so pointing `$TICKET_CONFIG` somewhere else moves the
    # state with it (decision #24). The documented default is unchanged: a
    # config at `~/.ticket/config.yml` still stores state in `~/.ticket`.
    root = path.parent
    store = os.environ.get("TICKET_STORE") or raw.get("store")
    wt = raw.get("worktrees") or {}
    if not isinstance(wt, dict):
        raise ConfigError("worktrees: must be a mapping")
    _reject_unknown("worktrees:", wt, WORKTREES_KEYS)
    cfg = Config(
        store=_anchor(store, root) if store else root,
        root=root,
        models=models,
        default_model=default_model,
        steps=_load_steps(raw.get("steps") or [], default_model),
        reviews=_load_reviews(raw.get("reviews") or [], default_model),
        repos=dict(raw.get("repos") or {}),
        worktrees=Worktrees(
            enabled=bool(wt.get("enabled", True)),
            root=_anchor(wt["root"], root)
            if wt.get("root")
            else DEFAULT_WORKTREE_ROOT.expanduser(),
            branch=wt.get("branch") or "{key}",
        ),
        sync=bool(raw.get("sync", True)),
        severities=_load_severities(raw.get("severities")),
        fix=_load_fix(raw.get("fix") or {}, default_model, models),
        tracker=_load_tracker(raw.get("tracker")),
        key_pattern=_load_key_pattern(raw.get("key_pattern")),
        parse_sources=_load_parse_sources(raw.get("parse")),
    )
    # Validate every repo override eagerly, not just the ones a given run
    # happens to call `for_repo` on: a bad repos: entry should fail at load
    # time, before the DAG is ever built for that repo.
    for repo, override in cfg.repos.items():
        if not isinstance(override, dict):
            raise ConfigError(f"repos.{repo} must be a mapping")
        _reject_unknown(f"repos.{repo}", override, REPO_KEYS)
        repo_steps = override.get("steps") or {}
        if not isinstance(repo_steps, dict):
            raise ConfigError(f"repos.{repo}.steps must be a mapping with skip:")
        _reject_unknown(f"repos.{repo}.steps", repo_steps, REPO_STEPS_KEYS)
        cfg.for_repo(repo)
    return cfg
