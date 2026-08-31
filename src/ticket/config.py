"""Load and validate config. Resolve repo overrides and model aliases.

The config file is central and serves every repo; per-repo differences are
overrides under `repos:`, never separate files. Overrides are shallow:
`reviews:` replaces the list, `steps.skip:` subtracts from it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from .errors import ConfigError

DEFAULT_CONFIG = Path("~/.ticket/config.yml")
DEFAULT_WORKTREE_ROOT = Path("~/worktrees")


def _anchor(raw: str, root: Path) -> Path:
    """Resolve a path out of the config: `~` expands, relative anchors to `root`.

    `root` is the directory the config file was loaded from, so every path in
    the file means the same thing no matter which directory `ticket` was run
    in. Used for `store:`, `worktrees.root:`, `prompt:`, and `run:`.
    """
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


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
    args: tuple[str, ...] = ()   # extra argv for a handoff, e.g. agent mode


@dataclass(frozen=True)
class Review:
    id: str
    order: int
    dispatch: str
    prompt: str
    model: str | None = None
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    store: Path   # where state lives; defaults to `root` (decision #24)
    root: Path    # the config file's directory; every relative path anchors here
    models: dict[str, str]
    default_model: str
    steps: tuple[Step, ...]
    reviews: tuple[Review, ...]
    repos: dict[str, dict]
    worktrees: Worktrees
    sync: bool

    def model_id(self, alias: str) -> str:
        try:
            return self.models[alias]
        except KeyError:
            raise ConfigError(
                f"unknown model alias: {alias}. Known: {', '.join(sorted(self.models))}"
            ) from None

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


def _load_reviews(raw_reviews: list) -> tuple[Review, ...]:
    reviews: list[Review] = []
    seen: set[str] = set()
    for raw in raw_reviews:
        review_id = raw.get("id")
        if not review_id:
            raise ConfigError("every review needs an id")
        if review_id in seen:
            raise ConfigError(f"duplicate review id: {review_id}")
        seen.add(review_id)
        dispatch = raw.get("dispatch")
        if dispatch not in ("bot", "local"):
            raise ConfigError(
                f"review {review_id} has dispatch {dispatch!r}: expected bot or local"
            )
        if not raw.get("prompt"):
            raise ConfigError(f"review {review_id} has no prompt: (both transports need one)")
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


def load_config(path: Path | None = None) -> Config:
    path = (path or config_path()).expanduser()
    if not path.is_file():
        raise ConfigError(f"no config at {path}. Copy examples/config.yml to get started.")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a YAML mapping")

    models = dict(raw.get("models") or {})
    default_model = (raw.get("defaults") or {}).get("model")
    if default_model and default_model not in models:
        raise ConfigError(f"defaults.model is {default_model!r}, which is not in models:")

    # The store defaults to the directory the config was found in, not to a
    # fixed `~/.ticket`, so pointing `$TICKET_CONFIG` somewhere else moves the
    # state with it (decision #24). The documented default is unchanged: a
    # config at `~/.ticket/config.yml` still stores state in `~/.ticket`.
    root = path.parent
    store = os.environ.get("TICKET_STORE") or raw.get("store")
    wt = raw.get("worktrees") or {}
    cfg = Config(
        store=_anchor(store, root) if store else root,
        root=root,
        models=models,
        default_model=default_model,
        steps=_load_steps(raw.get("steps") or [], default_model),
        reviews=_load_reviews(raw.get("reviews") or []),
        repos=dict(raw.get("repos") or {}),
        worktrees=Worktrees(
            enabled=bool(wt.get("enabled", True)),
            root=_anchor(wt["root"], root) if wt.get("root") else DEFAULT_WORKTREE_ROOT.expanduser(),
            branch=wt.get("branch") or "{key}",
        ),
        sync=bool(raw.get("sync", True)),
    )
    # Validate every repo override eagerly, not just the ones a given run
    # happens to call `for_repo` on: a bad repos: entry should fail at load
    # time, before the DAG is ever built for that repo.
    for repo in cfg.repos:
        cfg.for_repo(repo)
    return cfg
