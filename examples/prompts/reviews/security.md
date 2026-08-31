Review this PR for security only: input handling, authorisation, secrets in logs or errors, injection through shelled-out commands, and anything that widens what an untrusted input can reach.

This is separate from the org's required security bot; overlap is fine.

Use the same output format as prompts/reviews/docs-tests.md: one <details> block per severity keyed by 🔴/🟡/🔵, findings as top-level `*` bullets with the file in backticks, `None.` for an empty block, and a final `**Verdict:**` line. Severity is importance, not effort.
