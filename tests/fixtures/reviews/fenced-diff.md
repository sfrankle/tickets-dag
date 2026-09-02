## Review

<details>
<summary>🔴 Blocking</summary>

* The guard is inverted in `src/Foo.kt`, so the null case falls through:

  ```diff
  - if (x == null) return;
  + if (x != null) return;
  ```

  Swap the branches and the logged case is the one that happens.

</details>

**Verdict:** changes requested.
