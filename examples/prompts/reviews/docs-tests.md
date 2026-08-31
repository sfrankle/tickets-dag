Review this PR for documentation and test coverage only.

Look for: claims in docs the code contradicts, documented flags or options that
no longer exist, behaviour changed without a test, tests that assert the
implementation rather than the behaviour.

Report in exactly this format:

<details>
<summary>🔴 Blocking</summary>

* `path/to/file` — what is wrong, and why it matters.

</details>

<details>
<summary>🟡 Maintenance</summary>

None.

</details>

<details>
<summary>🔵 Architecture</summary>

None.

</details>

**Verdict:** approved | changes requested

Severity is importance, not effort: it says how much the finding matters, not
how hard it is to fix. 🔴 blocking, 🟡 maintenance, 🔵 architecture. A one-line
fix can be blocking.
Write `None.` inside a block with no findings. One finding per top-level bullet,
and name the file in backticks at the start of the bullet.
