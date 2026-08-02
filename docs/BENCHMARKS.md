---
layout: doc
title: PostgreSQL cache benchmarks
description: Reproducible pg_local_cache throughput, latency and correctness measurements against PostgreSQL, Valkey and Redis.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
---

# pg_local_cache benchmarks

The benchmark suite separates ordinary SQL, whole-row RESP reads, and
payload-width sweeps. Write, rollback, DDL, and invalidation semantics are
verified by integration tests because they have different durability and
transaction contracts and do not belong in a cache-read ranking.

## Results

The latest pinned result is [CI run 30729192604](https://github.com/aicopilot-fr/pg_local_cache/actions/runs/30729192604)
for [source `9cf12e3`](https://github.com/aicopilot-fr/pg_local_cache/commit/9cf12e34bee512e4d453e117c39ca8eb140afd4d).
It used three repetitions, 4,096 keys, 128-byte payloads, c16/p32 throughput,
and a separate c16/p1 latency pass.

| Protocol | Path | Median ops/s | Mean | p50 | p95 | p99 |
|---|---|---:|---:|---:|---:|---:|
| Prepared | Stock PostgreSQL 16 | 37,054 | 1.200 ms | 1.070 ms | 2.501 ms | 3.982 ms |
| Prepared | Mapped, cache off | 37,310 | 1.157 ms | 1.038 ms | 2.486 ms | 4.239 ms |
| Prepared | Cache on | 34,662 | 1.160 ms | 1.040 ms | 2.459 ms | 3.989 ms |
| Extended | Stock PostgreSQL 16 | 16,595 | 2.183 ms | 1.954 ms | 4.501 ms | 6.162 ms |
| Extended | Mapped, cache off | 16,704 | 2.193 ms | 1.945 ms | 4.501 ms | 6.145 ms |
| Extended | Cache on | 14,941 | 2.214 ms | 2.016 ms | 4.585 ms | 6.366 ms |

Prepared cache-on was 0.94x stock and 0.93x mapped cache-off. Extended
cache-on was 0.90x stock and 0.89x mapped cache-off. These short GitHub-hosted
measurements detect regressions; shared-runner scheduling makes them unsuitable
for capacity claims.

The same artifact contains a non-gating c4/p8 throughput snapshot with a
separate c4/p1 latency pass:

| Protocol | Path | Median ops/s | Mean | p50 | p95 | p99 |
|---|---|---:|---:|---:|---:|---:|
| Prepared | Stock PostgreSQL 16 | 32,716 | 0.320 ms | 0.263 ms | 0.742 ms | 1.365 ms |
| Prepared | Mapped, cache off | 32,834 | 0.317 ms | 0.239 ms | 0.786 ms | 1.669 ms |
| Prepared | Cache on | 31,071 | 0.331 ms | 0.240 ms | 0.894 ms | 1.668 ms |
| Extended | Stock PostgreSQL 16 | 15,065 | 0.548 ms | 0.482 ms | 1.039 ms | 1.767 ms |
| Extended | Mapped, cache off | 14,906 | 0.499 ms | 0.461 ms | 0.923 ms | 1.115 ms |
| Extended | Cache on | 14,012 | 0.534 ms | 0.490 ms | 0.999 ms | 1.272 ms |

The same run recorded a separate one-second, one-repetition c4/p8 RESP smoke
test. Each target returned byte-identical whole-row JSON:

| Target | Median ops/s | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| pg_local_cache 1.0.0 | 165,495 | 0.157 ms | 0.290 ms | 0.507 ms |
| Valkey 9.1.1 | 214,310 | 0.118 ms | 0.232 ms | 0.329 ms |
| Redis 8.8.1 | 201,990 | 0.107 ms | 0.265 ms | 0.420 ms |

The RESP and SQL tables measure different protocols and must not be combined
into one ranking.

The comparison job also records three prepared SQL shapes. This is a
one-second, one-repetition smoke test, separate from the primary SQL-only suite:

| SQL shape | Mapped cache ops/s | Stock PostgreSQL ops/s | Mapped/stock |
|---|---:|---:|---:|
| `SELECT *` | 115,344 | 65,062 | 1.77x |
| Reordered projection | 113,799 | 62,254 | 1.83x |
| Reordered composite predicates | 124,305 | 67,099 | 1.85x |

The short comparison job and repeated SQL-only suite use different table
shapes, durations, server allocations, and run ordering. Their rates are not
pooled or used to claim one universal speedup.

A publishable result retains raw JSON and rendered Markdown with the source
revision, harness checksum, server and client configuration, all repetitions,
throughput distribution, and latency distribution. It does not select a single
best repetition.

## Comparison matrix

| Suite | Baseline | Extension control | Cached path | Primary measurements |
|---|---|---|---|---|
| SQL-only ordinary `SELECT` | Stock PostgreSQL without `pg_local_cache` | Same mapped server with `pg_local_cache.sql_cache=off` | Same mapped server with `sql_cache=on` | Median ops/s; per-operation mean, p50, p95, p99; cache counters |
| Whole-row RESP `GET` | Valkey and Redis with persistence disabled | Not applicable | `pg_local_cache` KVik-inspired whole-row key | Median ops/s; client-observed p50, p95, p99; reply validation; database-read deltas |
| RESP payload width | Same key and client settings at each row width | Not applicable | Complete cached row | Median ops/s; encoded row bytes; cache counters |

The SQL-only table is the primary comparison for applications that keep using
normal PostgreSQL drivers. The RESP table answers a different question: the
cost of reading an identical JSON row through the optional cache endpoint.

## Dedicated SQL-only benchmark

`benchmarks/sql_only.py`, launched by `tests/docker_sql_only_smoke.sh`, creates
two PostgreSQL 16 servers:

- a stock server that neither installs nor preloads the extension;
- a mapped server with `pg_local_cache.port=0`, so it has no RESP secret,
  listener, worker, or client buffers.

The harness creates the same schema and rows on both servers and verifies that
their relevant PostgreSQL settings match. A `LOGIN NOSUPERUSER` role executes
the same complete-primary-key `SELECT *` in three modes:

1. stock PostgreSQL;
2. the mapped server with SQL caching disabled;
3. the mapped server with SQL caching enabled.

Each mode is measured independently for both `pgbench -M prepared` and
`pgbench -M extended`. Prepared mode reuses a server-side prepared statement.
Extended mode sends unnamed Parse/Bind/Execute messages for every batch. Driver
auto-prepare policies differ, so neither lane is a universal proxy for every
ORM.

### Correctness contract

Before timing, the harness verifies:

- the stock server does not contain or preload `pg_local_cache`;
- stock and mapped servers use the same PostgreSQL version and comparison
  settings;
- source row counts and key ranges match;
- stock, mapped-direct, and cached sentinel rows are byte-identical;
- the mapped plan includes `Custom Scan (pg_local_cache_sql)` when enabled;
- a cold SQL lookup produces one miss and fill, followed by a hit;
- the SQL-only server reports zero configured/running RESP workers and zero
  RESP memory.

During every cached throughput and latency window, successful statements must
equal the `sql_cache_hits` delta exactly. Timed misses, fills, and safety
bypasses must remain zero. Direct runs must not change any `sql_cache_*`
counter. Any mismatch fails the run rather than reporting a misleading rate.

### Throughput and latency

Throughput runs use persistent connections and a configurable number of
validated lookups per transaction batch. `operations_per_second` is batch TPS
multiplied by that exact lookup count; failed batches remain visible in the raw
report.

Latency runs are separate. They use one `SELECT` per transaction, persistent
connections, and pgbench latency logs with deterministic sampling. The report
contains mean, p50, p95, p99, maximum, and sample count for each protocol and
each of the three modes. These are client-observed end-to-end operation
latencies, including PostgreSQL protocol and transaction overhead after the
connection is established. They are not inferred by dividing pipelined batch
latency. This is a closed-loop saturation measurement: each client submits its
next transaction after the previous one completes. It does not hold the offered
request rate constant across modes, so it should not be read as a fixed-rate
service-time comparison.

The default latency gate is `MEASURED`: no p99 limit is assumed across unknown
hardware. Set `PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS` to enforce a deployment-
specific ceiling. `PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES` prevents a sparse
sample from passing.

Relative throughput gates are also optional. Set
`PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO` and
`PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO` to reject a cached result below
the required fraction of the mapped-direct and stock medians. CI currently
sets both to `0.80`; an unset gate is reported as `MEASURED` rather than passed.

### c4/p8 and c16/p32 snapshot

CI also records a short scaling snapshot on the same runner and the same two
PostgreSQL containers. The existing c16/p32 result remains the strict primary
profile with three repetitions and all configured gates. The harness then runs
one non-gating c4/p8 repetition for both prepared and unnamed-extended SQL.

The profile pipeline applies only to the throughput pass. Latency is always
measured with one `SELECT` per transaction, so the corresponding latency
profiles are c4/p1 and c16/p1. The generated table reports stock PostgreSQL,
the mapped table with caching off, and caching on with median throughput plus
latency mean, p50, p95, and p99.

The c4/p8 performance numbers do not decide the build result. Incorrect cache
counters, failed batches, missing raw latency samples, or a wrong pipeline do
fail the benchmark. The c16/p32 entry reuses the strict primary result.

This snapshot changes connection count and throughput pipeline together. It
compares two operating profiles; it is not a causal concurrency curve. Use a
long, repeated dedicated run before publishing capacity claims.

### Run it

The command below runs integration checks followed by three 30-second
throughput repetitions and per-mode latency passes:

```bash
PGLC_SQL_ONLY_BENCH_DURATION=30 \
PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS=5 \
PGLC_SQL_ONLY_BENCH_LATENCY_DURATION=15 \
PGLC_SQL_ONLY_BENCH_LATENCY_SAMPLE_RATE=0.05 \
PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES=200 \
PGLC_SQL_ONLY_BENCH_REPETITIONS=3 \
PGLC_SQL_ONLY_BENCH_CONCURRENCY=16 \
PGLC_SQL_ONLY_BENCH_PIPELINE=32 \
PGLC_SQL_ONLY_BENCH_KEYS=16384 \
PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES=128 \
PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_SCALING_SNAPSHOT=true \
PGLC_SQL_ONLY_BENCH_SCALING_DURATION=3 \
PGLC_SQL_ONLY_BENCH_SCALING_WARMUP_SECONDS=1 \
PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_DURATION=3 \
PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_SAMPLE_RATE=0.10 \
PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_MIN_SAMPLES=500 \
PGLC_SQL_ONLY_BENCH_SCALING_REPETITIONS=1 \
PGLC_SQL_ONLY_BENCH_OUTPUT_DIR="$PWD/benchmark-results/sql-only" \
bash tests/docker_sql_only_smoke.sh
```

The output directory receives:

- `sql-only.json`: raw configuration, correctness checks, repetitions,
  counters, raw latency samples, distributions, and gates;
- `sql-only.md`: rendered strict and scaling tables for stock, mapped-direct,
  and cached paths;
- a failure report if the harness stops before satisfying its contract.

The SQL-only client has no Docker CPU quota by default. `pgbench` uses one job
per detected CPU, capped by the configured connection count. Set
`PGLC_SQL_ONLY_BENCH_CLIENT_CPUS` and `PGLC_SQL_ONLY_BENCH_JOBS` when a repeatable
client allocation is required. The report records the effective job count,
logical CPUs, and cgroup CPU and memory limits either way.

The 10,000 ops/s defaults are regression floors for the cached prepared and
extended lanes. They are not published capacity claims. Set them to zero only
for diagnosis, and set a higher target for a deployment-specific release gate.

## Whole-row RESP comparison

Run the whole-row comparison with:

```bash
bash benchmarks/run.sh
```

The whole-row report is written to `whole-row.json` and `whole-row.md`. It
creates a table with a composite primary key and reads keys in this form:

```text
CRUD:database.schema.table:{"pk_a":1,"pk_b":"value"}
```

For every source row, PostgreSQL's exact row JSON is loaded byte-for-byte into
Valkey and Redis. The three targets receive the same client implementation,
key order, connections, pipeline depth, CPU quota, Docker network, and reply
validation. Target order rotates between repetitions. Valkey and Redis
persistence is disabled because this lane measures cache reads, not durable
writes.

Before a timed `pg_local_cache` pass, the harness warms and validates the full
working set. A timed pass fails if it records a cache miss, source-table read,
protocol error, or wrong value. Payload-width sweeps remain separate because
row size changes serialization, copying, network, and client-decoding costs.

On success, the comparison runner writes `whole-row.json` and `whole-row.md`.
SQL-only evidence is produced separately by `tests/docker_sql_only_smoke.sh` as
`sql-only.json` and `sql-only.md`. The exact workloads and controls are listed
in the
[benchmark scenarios](https://github.com/aicopilot-fr/pg_local_cache/blob/main/benchmarks/SCENARIOS.md).

## Fairness rules

### Ordinary SQL

- Stock, mapped-direct, and cached runs use the same PostgreSQL major and
  checked comparison settings.
- Schema, generated data, query text, key sequence, role capabilities,
  connection count, client process, protocol, pipeline, and random seed match.
- The mapped direct/cache pair differs only by
  `SET pg_local_cache.sql_cache=off|on`.
- Stock and cached sentinel rows are compared before timing.
- Prepared and unnamed-extended results are never averaged together.
- Throughput and single-operation latency are measured in separate passes.

The stock server shows end-to-end behavior without the extension. The mapped
cache-off lane isolates the overhead of the preloaded extension, mapping, and
invalidation hooks on the same server used for the cache-on lane. Both
baselines belong in a published table.

### RESP

- Payload bytes and encoded request streams match across targets.
- Connection and authentication setup is outside the timed interval.
- Every response is decoded and checked, not merely counted.
- Warm-read runs require no timed PostgreSQL source read or cache miss.
- RESP writes are not presented as durability-equivalent to Valkey or Redis
  writes with persistence disabled.

### Latency semantics

SQL-only latency uses one `SELECT` per transaction. RESP latency starts when a
pipeline is sent and ends after its replies are decoded, so it includes queueing
behind earlier commands in that pipeline. Do not compare their percentiles
directly. Record protocol, pipeline depth, client count, and sampling method
with every latency table.

## Correctness outside timed read lanes

Cold-fill checks account for the expected source reads and fills, while warm
read measurements require zero source reads and misses. Rollback, primary-key
changes, `TRUNCATE`, DDL, RESP writes, and reconciliation are covered by Docker
integration tests rather than inferred from throughput counters.

See
[SCENARIOS.md](https://github.com/aicopilot-fr/pg_local_cache/blob/main/benchmarks/SCENARIOS.md)
for every timed operation and its required counter evidence.

## Publishing results

For results intended for a public comparison:

1. Use a clean commit and record its full SHA; do not publish a dirty-tree run.
2. Pin PostgreSQL, Valkey, Redis, and benchmark-client images by digest.
3. Pin server and client to separate physical CPU sets rather than relying only
   on Docker quotas.
4. Disable swap and unrelated workloads; record CPU model, frequency governor,
   memory, kernel, container runtime, and storage.
5. Keep schemas, payload bytes, key distribution, connection counts, and
   protocols identical within each comparison.
6. Use a meaningful warmup and at least three long repetitions. Inspect
   min/median/max and coefficient of variation.
7. Report throughput and latency together. Include p50, p95, p99, sample count,
   and the latency semantics above.
8. Retain all raw repetitions, failures, counter deltas, rendered tables,
   harness checksums, and image identities.
9. Commit the complete report and link it from the README by immutable source
   revision. Do not copy a number from an expiring CI artifact without its
   evidence bundle.
10. Repeat the run on the intended HA, storage, connection-pool, row-width, and
    CPU profile before setting a service objective.

GitHub-hosted runners remain useful for correctness and regression detection,
but variable CPU scheduling makes them unsuitable as the sole source of a
capacity comparison.

For implementation limits that affect interpretation, see the
[technical reference]({{ '/docs/TECHNICAL.html' | relative_url }}).
