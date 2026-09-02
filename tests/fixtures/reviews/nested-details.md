## Review

<details>
<summary>🔴 Blocking</summary>

* The retry loop never caps its backoff in `src/api/retry.py`.

<details>
<summary>What the trace looks like</summary>

Six retries inside one second, then the socket is exhausted.

</details>

* The rejection path emits a metric and no log line in `src/api/reject.py`.

</details>

**Verdict:** changes requested.
