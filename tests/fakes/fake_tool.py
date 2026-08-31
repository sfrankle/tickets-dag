#!/usr/bin/env python3
"""One executable standing in for gh, git, or claude.

Copied to tests/fakes/bin/{gh,git,claude} by the `fake_bin` fixture. Every
invocation appends its argv to $FAKE_LOG and whatever it was given on stdin to
$FAKE_STDIN_LOG. Responses are looked up in $FAKE_RESPONSES, a JSON list of
{"prefix", "stdout", "exit_code"} matched against the joined argv; the first
matching prefix wins. No match means exit 0 with empty stdout, so a test only
scripts what it cares about.

Prompts reach `claude` on stdin, not in argv (decision #21), so a test that
asserts on a prompt reads `fake_bin.stdin_to("claude")`.
"""

import json
import os
import sys


def main() -> int:
    argv = [os.path.basename(sys.argv[0]), *sys.argv[1:]]

    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(argv) + "\n")

    stdin_log = os.environ.get("FAKE_STDIN_LOG")
    if stdin_log:
        try:
            piped = "" if sys.stdin.isatty() else sys.stdin.read()
        except (OSError, ValueError):
            piped = ""
        with open(stdin_log, "a") as fh:
            fh.write(json.dumps({"tool": argv[0], "stdin": piped}) + "\n")

    responses_path = os.environ.get("FAKE_RESPONSES")
    responses = []
    if responses_path and os.path.exists(responses_path):
        with open(responses_path) as fh:
            responses = json.load(fh)

    joined = " ".join(argv)
    for response in responses:
        if joined.startswith(response["prefix"]):
            sys.stdout.write(response.get("stdout", ""))
            sys.stderr.write(response.get("stderr", ""))
            return int(response.get("exit_code", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
