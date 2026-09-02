Review this PR for security only: input handling, authorisation, secrets in logs or errors, injection through shelled-out commands, and anything that widens what an untrusted input can reach.

Ignore anything another review in this config covers.
Overlap with a review your repo already requires is fine — say it anyway.

Report in exactly this format:

```md
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
```

Severity is importance, not effort: it says how much the finding matters, not how hard it is to fix. 🔴 blocking, 🟡 maintenance, 🔵 architecture.
That legend is the one `severities:` declares in the config this prompt ships with; a config that declares another set wants this line and the block headings above changed to match.
A one-line fix can be blocking.
Write `None.` inside a block with no findings.
One finding per top-level bullet, and name the file in backticks at the start of the bullet.
