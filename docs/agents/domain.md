# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## This repo keeps its decisions in the GitHub wiki

Not in `docs/adr/`, which does not exist here and should not be created.
The decision log is a numbered table on the **`v1: release notes`** wiki page, with a `firm` / `provisional` / `superseded by #N` status per row.
`firm` means settled and would need a real reason to reopen; `provisional` means it works and is expected to be revisited.

Read it before proposing anything architectural:

```bash
git clone git@github.com:sfrankle/tickets-dag.wiki.git
```

A wiki is an ordinary git repo and `gh` has no API for one, so clone it to a scratch directory and read the Markdown there.
Push back the same way, committing with the email `git config user.email` reports in the main clone: the account rejects a push authored from a private address.

**Cite a decision by its number**, the way an ADR would be cited: _"Contradicts decision 20 (resolver order), but worth reopening because…"_.
When work settles a new decision, append a row rather than editing an old one, and mark the old one `superseded by #N`.
The log is one sequence across releases, not one table per release.

## Before exploring, read these

- The wiki decision log above, which is where this repo's ADRs actually live.
- **`CONTEXT.md`** at the repo root, if it exists.

Neither `docs/adr/` nor `CONTEXT-MAP.md` is used here, and the decision log is the wiki page rather than a directory of files.
If `CONTEXT.md` is not there, **proceed silently**.
Don't flag its absence; don't suggest creating it upfront.
The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates it lazily when terms actually get resolved.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag decision conflicts

If your output contradicts a decision in the wiki log, surface it explicitly rather than silently overriding:

> _Contradicts decision 20 (resolver order), but worth reopening because…_

A `provisional` row is an invitation to reopen; a `firm` one is not.
