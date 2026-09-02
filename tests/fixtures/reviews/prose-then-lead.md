## Review

<details>
<summary>🟡 Maintenance</summary>

Two things in this section, both in the client:
**Foo.kt:43 - the null branch of this guard is permitted** (`src/Foo.kt`): so
the case it is meant to catch passes through with nothing in the logs.

* The retry loop never caps its backoff in `src/Bar.kt`.

</details>

**Verdict:** changes requested.
