# Benchmark evidence for `fe2d23c`

These are the exact current-API benchmark artifacts downloaded from
[CI run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805)
for commit `fe2d23c87ddc7e523ada2951376ebcb7d8570fb1`. The run and every
independent benchmark gate passed.

| File | Actions artifact ID | Actions expiry | SHA-256 |
|---|---:|---|---|
| [`comparison-smoke.zip`](comparison-smoke.zip) | `8851825673` | 2026-08-10 | `fc624e7ebed11b10c8470d11e7d2a91855813e04f9fb809e62e4f0852f7c8a76` |
| [`sql-only-benchmark-smoke.zip`](sql-only-benchmark-smoke.zip) | `8851940541` | 2026-09-02 | `22be445d210138be086da186bdbe4c7fb1e3543b4a26b3f98b90c8099e929d02` |

The comparison ZIP contains `whole-row.json` and `whole-row.md` for ordinary
exact-key SELECT and RESP2 GET. The SQL-only ZIP contains `sql-only.json` and
`sql-only.md` for SQL GET/MGET, including raw latency samples. Both retain the
workload, environment, every repetition, counter deltas, gates, and results.
The original artifact bytes are preserved unchanged.
