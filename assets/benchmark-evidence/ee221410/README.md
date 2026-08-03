# Benchmark evidence for `ee221410`

These are the exact GitHub Actions artifact ZIPs downloaded from
[CI run 30796269395](https://github.com/profundium/pg_local_cache/actions/runs/30796269395)
for commit `ee221410da59a8d5a3adb2068160d441b75e05f2`. The SQL-only and
comparison-smoke jobs passed; the overall run was red because two later Docker
integration jobs failed on leaked test-role ACL state.

| File | Actions artifact | Artifact ID | Actions expiry | SHA-256 |
|---|---|---:|---|---|
| [`sql-only-benchmark-smoke.zip`](sql-only-benchmark-smoke.zip) | `sql-only-benchmark-smoke` | `8849113380` | 2026-09-02 | `da4d7cad085e21ed636ee8ea54ab6bc30ec24a482282b15378a037f6ad3e1220` |
| [`comparison-smoke.zip`](comparison-smoke.zip) | `comparison-smoke` | `8848997316` | 2026-08-10 | `9facd988ca29b671fc51f3df471bdd013458e29e691cf81d9917979d1781e458` |

The SQL-only ZIP contains `sql-only.json` with every repetition, counter delta,
gate, environment field, and raw sampled latency value, plus the generator's
`sql-only.md`. The comparison ZIP contains `whole-row.json` and `whole-row.md`.

The archived `sql-only.json` has two stale human-readable `methodology` labels:
it calls the primary key composite and says the query is identical. Its recorded
schema and query evidence is authoritative: the benchmark primary key is `id`,
and stock PostgreSQL uses different SQL because it does not provide
`local_cache.mget`. The generator and public interpretation were corrected after
this commit; the original evidence bytes are intentionally preserved unchanged.
