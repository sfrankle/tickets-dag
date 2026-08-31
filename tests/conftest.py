import json
import stat
import sys
from pathlib import Path

import pytest

from ticket.store import Store

FAKE_TOOL = Path(__file__).parent / "fakes" / "fake_tool.py"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


class FakeBin:
    """A directory of fake gh/git/claude on PATH, plus a call log."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.log = directory / "calls.log"
        self.stdin_log = directory / "stdin.log"
        self.responses_file = directory / "responses.json"
        self._responses: list[dict] = []
        self._flush()

    def _flush(self) -> None:
        self.responses_file.write_text(json.dumps(self._responses))

    def respond(
        self, prefix: str, stdout: str = "", exit_code: int = 0, stderr: str = ""
    ) -> None:
        """Make any call whose joined argv starts with `prefix` return this."""
        self._responses.insert(
            0,
            {
                "prefix": prefix,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            },
        )
        self._flush()

    @property
    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def calls_to(self, tool: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == tool]

    def stdin_to(self, tool: str) -> list[str]:
        """What was piped to each invocation of `tool`, in order.

        Prompts go on stdin (decision #21), so this is where a test looks for
        one — not in the argv.
        """
        if not self.stdin_log.exists():
            return []
        entries = [
            json.loads(line) for line in self.stdin_log.read_text().splitlines() if line
        ]
        return [e["stdin"] for e in entries if e["tool"] == tool]


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    directory = tmp_path / "fakebin"
    directory.mkdir()
    # `PATH` is about to be replaced wholesale (see below), so `/usr/bin/env
    # python3` in the fake tool's shebang would no longer resolve. Point the
    # copies at this interpreter directly instead of relying on PATH lookup.
    body = FAKE_TOOL.read_text().split("\n", 1)[1]
    for tool in ("gh", "git", "claude"):
        target = directory / tool
        target.write_text(f"#!{sys.executable}\n{body}")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    fake = FakeBin(directory)
    monkeypatch.setenv("PATH", str(directory), prepend=False)
    monkeypatch.setenv("FAKE_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_STDIN_LOG", str(fake.stdin_log))
    monkeypatch.setenv("FAKE_RESPONSES", str(fake.responses_file))
    return fake
