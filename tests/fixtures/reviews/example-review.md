## Docs and tests review

<details>
<summary>🔴 Blocking</summary>

* `workflows/manifest-status-update.md` — the degradation claim contradicts
  `collect.team-prs`, which returns partial results rather than failing.
* `src/api/retry.py` — retries are unbounded when the upstream returns 429.

</details>

<details>
<summary>🟡 Maintenance</summary>

* `README.md` — the install section still names the removed `--legacy` flag.

</details>

<details>
<summary>🔵 Architecture</summary>

None.

</details>

**Verdict:** changes requested.
