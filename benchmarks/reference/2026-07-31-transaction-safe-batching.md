# pg_local_cache comparative benchmark

Generated: `2026-07-31T20:11:39.662805+00:00`

This is a warm positive GET comparison. The RESP table uses one byte-identical Python/multiprocess client for all three targets. It is not a durability or transactional-invalidation comparison.

## Workload

| Parameter | Value |
|---|---:|
| Measured duration per run | 120.0 s |
| Untimed warmup per run | 15.0 s |
| Repetitions | 3 |
| Persistent connections | 16 |
| Pipeline | 32 |
| Keys | 16384 |
| Value bytes | 128 |
| Whole-run latency reservoir | 200000 samples |
| Server CPU quota | 2.0 |
| Client CPU quota | 2.0 |
| Client memory limit | 3g |
| pg_local_cache workers | 4 |

## RESP warm GET

| Target | Version | Median ops/s | Min–max ops/s | CV | Pipeline-completion p50 | Pipeline-completion p95 | Pipeline-completion p99 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| pg_local_cache | 1.0.0 | 239 292 | 236 072–243 811 | 1.32% | 1.693 ms | 4.265 ms | 6.951 ms | 0 |
| valkey | 9.1.1 | 235 349 | 233 980–235 787 | 0.33% | 1.886 ms | 4.363 ms | 6.850 ms | 0 |
| redis | 8.8.1 | 238 019 | 235 799–240 762 | 0.85% | 1.849 ms | 4.358 ms | 6.694 ms | 0 |

Individual RESP runs:

| Target | Run | ops/s | p50 | p95 | p99 | Client quota CPU | Cache misses | SQL reads |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pg_local_cache | 1 | 239 292 | 1.693 ms | 4.336 ms | 7.317 ms | 61.1% | 0 | 0 |
| pg_local_cache | 2 | 236 072 | 1.741 ms | 4.265 ms | 6.951 ms | 60.9% | 0 | 0 |
| pg_local_cache | 3 | 243 811 | 1.684 ms | 4.178 ms | 6.818 ms | 62.2% | 0 | 0 |
| valkey | 1 | 233 980 | 1.873 ms | 4.407 ms | 6.850 ms | 61.7% | — | — |
| valkey | 2 | 235 787 | 1.899 ms | 4.363 ms | 6.800 ms | 61.7% | — | — |
| valkey | 3 | 235 349 | 1.886 ms | 4.360 ms | 6.902 ms | 62.0% | — | — |
| redis | 1 | 235 799 | 1.886 ms | 4.475 ms | 6.830 ms | 63.0% | — | — |
| redis | 2 | 238 019 | 1.849 ms | 4.358 ms | 6.694 ms | 62.7% | — | — |
| redis | 3 | 240 762 | 1.848 ms | 4.274 ms | 6.643 ms | 62.9% | — | — |

## Direct stock PostgreSQL reference

This section is deliberately separate: pgbench uses the PostgreSQL extended protocol against a separate stock PostgreSQL container and validates SQL errors, while the RESP harness validates every returned value. Statements in one pgbench pipeline share an implicit transaction/snapshot, so this amortizes more SQL overhead than independent transactions.

| Client | Median value lookups/s | Min–max lookups/s | CV | Operations per pipeline batch |
|---|---:|---:|---:|---:|
| pgbench prepared | 47 974 | 47 479–48 233 | 0.65% | 32 |

## Reproducibility and interpretation

- `pg_local_cache`: `pg_local_cache:benchmark-2003`, identity `sha256:1e3380b3f2ef75021e9453ed6a92dd674bc300d82e1b0f77c3fd7c7d312e2b09`.
- `postgres_plain`: `postgres:16.14-bookworm`, identity `postgres@sha256:92620daddcd947f8d5ab5ba66e848702fe443d87fed30c4cea8e389fd78dfc55`.
- `valkey`: `valkey/valkey:9.1.1-trixie`, identity `valkey/valkey@sha256:3acc0687f2a2e1091fae6450d7842dd658c941338cf0a873ddd9e14b9e4ea4dd`.
- `redis`: `redis:8.8.1-trixie`, identity `redis@sha256:c88d347edef6249a6d2293f926f1eeb48bd40c57cbcd02c07f52e7f1fd2cb46b`.
- `benchmark_client`: `pg_local_cache-benchmark-runner:2003`, identity `sha256:67ebd528396faa668e625ff97572e95cab9dcbc16335f7aa2e02a8434b169595`.
  Source `24e4d5c37ea76e63f19756ff528d89feae9d67e3`, harness SHA-256 `93befb23c30d3ec67b7eceeff24577c37c73c83df35d19a863374233963c7c96`.
- Gate: **PASS** — pg_local_cache median >= 10000 ops/s, zero RESP errors, misses, and SQL reads
- Valkey and Redis persistence is disabled for this cache-only read workload.
- `pg_local_cache` uses the reported worker count; Valkey/Redis have different execution topologies. Set `PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1` for a one-worker lane.
- CPU quotas are limits, not CPU affinity. All containers share the host, so hosted-runner results are noisy; use isolated, pinned CPUs for publication-quality claims.
- RESP p50/p95/p99 measure time from sending a pipeline batch until each response completes, including queueing behind earlier responses. Deterministic per-connection Algorithm R reservoirs sample the entire measured interval; their merge is weighted by each connection's completed operations. These are not per-command server service times.
- Valkey/Redis store an application-managed copy. `pg_local_cache` serves a PostgreSQL-owned row with transactional invalidation; this semantic difference is not represented by warm GET throughput.
