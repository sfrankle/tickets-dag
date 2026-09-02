<details>
<summary>🟡 Maintenance</summary>

* The rejection path calls `logger.warn` and never records which caller
  triggered it.

</details>

**Verdict:** changes requested.
