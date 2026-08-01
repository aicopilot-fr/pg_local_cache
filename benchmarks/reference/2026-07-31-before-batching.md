# pg_local_cache comparative benchmark

Generated: `2026-07-31T18:10:42.126246+00:00`

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
| pg_local_cache | 1.0.0 | 64 918 | 64 751–66 038 | 0.88% | 6.082 ms | 16.388 ms | 26.943 ms | 0 |
| valkey | 9.1.1 | 225 882 | 225 855–228 689 | 0.59% | 1.985 ms | 4.721 ms | 7.237 ms | 0 |
| redis | 8.8.1 | 234 906 | 234 773–235 939 | 0.22% | 1.872 ms | 4.447 ms | 6.734 ms | 0 |

Individual RESP runs:

| Target | Run | ops/s | p50 | p95 | p99 | Client quota CPU | Cache misses | SQL reads |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pg_local_cache | 1 | 64 918 | 6.082 ms | 16.388 ms | 26.943 ms | 46.2% | 0 | 0 |
| pg_local_cache | 2 | 66 038 | 5.950 ms | 17.342 ms | 27.951 ms | 45.9% | 0 | 0 |
| pg_local_cache | 3 | 64 751 | 6.220 ms | 14.954 ms | 24.309 ms | 46.7% | 0 | 0 |
| valkey | 1 | 225 882 | 1.970 ms | 4.721 ms | 7.285 ms | 61.0% | — | — |
| valkey | 2 | 225 855 | 1.994 ms | 4.732 ms | 7.237 ms | 61.7% | — | — |
| valkey | 3 | 228 689 | 1.985 ms | 4.656 ms | 6.987 ms | 62.0% | — | — |
| redis | 1 | 234 773 | 1.905 ms | 4.447 ms | 6.734 ms | 62.8% | — | — |
| redis | 2 | 234 906 | 1.872 ms | 4.450 ms | 6.807 ms | 62.9% | — | — |
| redis | 3 | 235 939 | 1.857 ms | 4.405 ms | 6.723 ms | 62.8% | — | — |

## Direct stock PostgreSQL reference

> Invalid historical lane: the harness passed `pgbench -d`, enabling
> per-command debug logging. Do not use the PostgreSQL throughput below;
> the RESP results above are unaffected.

This section is deliberately separate: pgbench uses the PostgreSQL extended protocol against a separate stock PostgreSQL container and validates SQL errors, while the RESP harness validates every returned value. Statements in one pgbench pipeline share an implicit transaction/snapshot, so this amortizes more SQL overhead than independent transactions.

| Client | Median value lookups/s | Min–max lookups/s | CV | Operations per pipeline batch |
|---|---:|---:|---:|---:|
| pgbench prepared | 46 058 | 46 045–46 542 | 0.50% | 32 |

## Reproducibility and interpretation

- `pg_local_cache`: `pg_local_cache:benchmark-2000`, identity `sha256:ab0ba4103355ec17f7a41ce53dceb9a43dd4fed35984ff7b49813e44282b3cbf`.
- `postgres_plain`: `postgres:16.14-bookworm`, identity `postgres@sha256:92620daddcd947f8d5ab5ba66e848702fe443d87fed30c4cea8e389fd78dfc55`.
- `valkey`: `valkey/valkey:9.1.1-trixie`, identity `valkey/valkey@sha256:3acc0687f2a2e1091fae6450d7842dd658c941338cf0a873ddd9e14b9e4ea4dd`.
- `redis`: `redis:8.8.1-trixie`, identity `redis@sha256:c88d347edef6249a6d2293f926f1eeb48bd40c57cbcd02c07f52e7f1fd2cb46b`.
- `benchmark_client`: `pg_local_cache-benchmark-runner:2000`, identity `sha256:7f90c596a43c6b7febdb6f06d62816c6bd656b524c27c4fae526f8e5eeff409d`.
  Source `45c28a8944f9971fb6260e66de312fae2db20683`, harness SHA-256 `1db20b3665402561b3bb19e641f247646fc7071e2b8e9eef6d12733041876bbc`.
- Gate: **PASS** — pg_local_cache median >= 10000 ops/s, zero RESP errors, misses, and SQL reads
- Valkey and Redis persistence is disabled for this cache-only read workload.
- `pg_local_cache` uses the reported worker count; Valkey/Redis have different execution topologies. Set `PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1` for a one-worker lane.
- CPU quotas are limits, not CPU affinity. All containers share the host, so hosted-runner results are noisy; use isolated, pinned CPUs for publication-quality claims.
- RESP p50/p95/p99 measure time from sending a pipeline batch until each response completes, including queueing behind earlier responses. Deterministic per-connection Algorithm R reservoirs sample the entire measured interval; their merge is weighted by each connection's completed operations. These are not per-command server service times.
- Valkey/Redis store an application-managed copy. `pg_local_cache` serves a PostgreSQL-owned row with transactional invalidation; this semantic difference is not represented by warm GET throughput.
