# Tests

## `examples/` is documentation, and tests must not pin it

The example config and its `input/` tree exist to be read, copied and edited.
They are not a fixture.
A test that asserts what the example *contains* makes the two files a matched pair, so editing the docs breaks the suite and the example stops being free to teach whatever it needs to teach.

Behaviour is tested against a config written for the test.
Every test module that needs one already has its own — `CONFIG` in `test_cli_surface.py`, `SAMPLE` in `test_config.py`, the fixtures in `conftest.py`.
When a new engine behaviour needs proving, it goes there, against a config that exists only to prove it.

`test_examples.py` is the only module that may read `examples/`, and it may only ask three questions of it.

**Is it self-consistent?**
Nothing it declares is dangling: every `prompt:`, `run:` and fix path resolves to a file that is there and is readable, and a `run:` is executable.
This is a link check.

**Does it agree with itself across files?**
The config declares a severity vocabulary and the review prompts spell out the same legend; a review prompt is self-contained because a `bot` review is posted as a PR comment and cannot read a sibling file.
These derive what they check *from* the config rather than restating it, so renaming a review or adding a severity keeps them passing.

**Does it teach something the engine would break on?**
A handoff that edits code without `--permission-mode` in `args:`, or a fix script that leaks a store-local finding id into a public comment, is an example that works badly for everyone who copies it.

Anything else is off limits.
No counts, no exact id lists, no repo names, no values the reader is expected to change.
`assert [r.id for r in cfg.reviews] == ["correctness", "security"]` and `assert len(warnings) == 2` are the shape to avoid: both fail when the documentation is edited, and neither tells you anything about the engine.

Two consequences worth stating, because both have already bitten:

- A test that reads a path under `~` is asserting a fact about whoever ran it. The example's `repos.<repo>.path` values are placeholders under `~`, so anything counting them passes for the author and fails in CI.
- The validator's warning tier (`config_warnings`, issue #23.3) exists so a placeholder repo path does not fail `--validate`. What that tier *does* belongs in `test_cli_surface.py`. All `test_examples.py` says about it is that the example reports no problems.
