<details>
<summary>🔴 Blocking</summary>

The retry loop in the client never caps its backoff, and this pull request
adds two more callers of it.

</details>

**Verdict:** changes requested.
