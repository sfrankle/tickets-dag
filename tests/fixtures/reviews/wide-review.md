## Review

<details open>
<summary>🟡 Maintenance (2)</summary>

**Foo.kt:43 - the null branch of this guard is permitted** (`src/Foo.kt`): so
the case it is meant to catch passes through with nothing in the logs.

**Bar.kt / Baz.kt - the rejection path emits a metric and no log line**
(`src/Bar.kt`): there is no way to tell which caller triggered it.

</details>

| Severity | Count |
|---|---|
| Maintenance | 2 |

**Verdict:** changes requested.
