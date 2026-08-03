---
layout: doc
title: PostgreSQL cache benchmarks
description: Reproducible pg_local_cache throughput, latency and correctness measurements against PostgreSQL, Valkey and Redis.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
---

# pg_local_cache benchmarks

The release gate is SQL-only: `local_cache.mget()` is compared with a stock
PostgreSQL primary-key batch query intended to return the same ordered whole-row
JSON. Transparent SQL and RESP measurements are separate compatibility
profiles. Write, rollback, DDL, and invalidation semantics are verified by
integration tests because they have different contracts and do not belong in a
cache-read ranking.

## Reference SQL-only CI snapshot (`ee221410`)

[CI run 30796269395](https://github.com/profundium/pg_local_cache/actions/runs/30796269395)
measured [source `ee221410`](https://github.com/profundium/pg_local_cache/commit/ee221410da59a8d5a3adb2068160d441b75e05f2).
The `sql-only-benchmark-smoke` job and its internal gate passed. Its exact
[`sql-only-benchmark-smoke.zip`](../assets/benchmark-evidence/ee221410/sql-only-benchmark-smoke.zip)
is preserved with the raw JSON, retained latency samples, and rendered
Markdown. It is Actions artifact `8849113380` (expiry 2026-09-02) and has ZIP
digest
`sha256:da4d7cad085e21ed636ee8ea54ab6bc30ec24a482282b15378a037f6ad3e1220`.
Each cached c16/k32 lane had to sustain at least 10,000 key ops/s and at least
`1.50x` both the stock and mapped-cache-off medians.

| Protocol | Path | c16/k32 key ops/s | vs stock | c16/k1 mean | p50 | p95 | p99 | Samples |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Prepared | Stock PostgreSQL 16.14 | 7,992 | 1.00x | 0.409 ms | 0.323 ms | 0.991 ms | 2.019 ms | 48,091 |
| Prepared | Mapped, cache off | 8,049 | 1.01x | 0.402 ms | 0.328 ms | 0.919 ms | 1.857 ms | 48,495 |
| Prepared | `local_cache.mget`, cache on | 111,103 | 13.90x | 0.778 ms | 0.516 ms | 1.196 ms | 3.150 ms | 30,091 |
| Unnamed extended | Stock PostgreSQL 16.14 | 7,992 | 1.00x | 0.601 ms | 0.568 ms | 1.150 ms | 1.649 ms | 38,201 |
| Unnamed extended | Mapped, cache off | 7,976 | 1.00x | 0.599 ms | 0.561 ms | 1.132 ms | 1.717 ms | 38,274 |
| Unnamed extended | `local_cache.mget`, cache on | 104,956 | 13.13x | 0.846 ms | 0.601 ms | 1.356 ms | 2.787 ms | 28,689 |

The c16/k32 pass uses 16 connections and batches of 32 key positions; the
cached batch calls MGET. Its unit is resolved key positions per second
(`batch TPS × 32`), not SQL statements per second. Latency comes from separate
scalar-key c16/k1 passes. It is a
closed-loop saturation measurement with no configured p99 objective. Cache-on
scalar p99 was higher than stock in both protocols in this run; the throughput
ratio must not be presented as a latency improvement.

The runner exposed four logical Intel Xeon Platinum 8573C CPUs and a 1 GiB
client cgroup without physical CPU pinning. PostgreSQL 16.14 used 4,096
deterministic, incompressible 3,000-byte values, two seconds of warmup, three
rotated five-second repetitions, four pgbench jobs, and a LOGIN NOSUPERUSER
role. The harness validates source count and key range, compares the first,
middle, and last scalar stock/mapped/cache row byte-for-byte, and requires every timed
cached key to produce one hit with zero misses, fills, or bypasses. It does not
byte-compare every timed batch or all 4,096 rows.

The archived report's human-readable `methodology` labels mistakenly say
"composite primary key" and "identical query". Its recorded schema and query
evidence is authoritative: the key is `id`, and stock PostgreSQL necessarily
uses different SQL because it has no `local_cache.mget`. The generator labels
are corrected after `ee221410`; the original evidence bytes remain unchanged.

The same servers produced a one-repetition c4/k8 scaling snapshot. Prepared
cache/stock was 1.15x (73,302 vs 63,568 key ops/s); unnamed extended was 1.61x
(68,439 vs 42,491). That sensitivity is why the c16/k32 13x result is a
workload-profile regression signal, not a general PostgreSQL speedup or a
capacity claim.

## Transparent SELECT and RESP smoke

The same workflow's
[`comparison-smoke.zip`](../assets/benchmark-evidence/ee221410/comparison-smoke.zip)
is Actions artifact `8848997316` (expiry 2026-08-10; ZIP digest
`sha256:9facd988ca29b671fc51f3df471bdd013458e29e691cf81d9917979d1781e458`).
It ran in a separate GitHub Actions job on an AMD EPYC 7763 runner, with a
2-CPU client quota and a 2-CPU quota per server target. It used four clients at
pipeline depth 8 (up to 32 in-flight operations), 128 keys, and 128-byte text
values. The configured timed warmup was zero, but the working set was prefilled
and stabilized before measurement; timed cache lanes required zero misses and
fills. Absolute 10,000 ops/s floors plus integrity and counter checks gate the
job; the displayed relative ratios are non-gating.

| Prepared SQL shape | Mapped cache ops/s | Stock PostgreSQL ops/s | Mapped/stock |
|---|---:|---:|---:|
| `SELECT *` | 126,169 | 67,017 | 1.88x |
| Reordered projection | 120,354 | 62,733 | 1.92x |
| Reordered composite predicates | 130,133 | 71,284 | 1.83x |

| RESP target | Median ops/s | p99 | Errors |
|---|---:|---:|---:|
| pg_local_cache | 149,703 | 0.374 ms | 0 |
| Valkey | 199,345 | 0.265 ms | 0 |
| Redis | 200,137 | 0.269 ms | 0 |

The SQL, SQL KV, and RESP tables measure different operations and must not be
combined into one ranking. A publishable result retains raw JSON and rendered
Markdown with the source revision, harness checksum, server and client
configuration, every repetition, and the latency distribution; it never
selects only the best repetition.

## Comparison matrix

| Suite | Baseline | Extension control | Cached path | Primary measurements |
|---|---|---|---|---|
| SQL-only KV batch | Stock PostgreSQL without `pg_local_cache`, using a PK batch query | Same mapped server using the stock batch query | `local_cache.mget()` on the mapped server | Median resolved key positions/s; per-operation mean, p50, p95, p99; exact cache counters |
| Transparent exact-PK `SELECT` | Stock PostgreSQL without `pg_local_cache` | Not recorded by the current comparison smoke | Mapped server with `sql_cache=on` | One-repetition prepared/pipelined compatibility smoke; exact hit counters; relative ratio non-gating |
| Whole-row RESP `GET` | Valkey and Redis with persistence disabled | Not applicable | `pg_local_cache` KVik-inspired whole-row key | Median ops/s; client-observed p50, p95, p99; reply validation; database-read deltas |
| RESP payload width | Same key and client settings at each row width | Not applicable | Complete cached row | Median ops/s; encoded row bytes; cache counters |

The SQL KV table is the release comparison for applications that keep using
normal PostgreSQL drivers. The transparent and RESP tables answer different
compatibility questions and do not decide the SQL KV performance gate.

## Dedicated SQL-only benchmark

`benchmarks/sql_only.py`, launched by `tests/docker_sql_only_smoke.sh`, creates
two PostgreSQL 16 servers:

- a stock server that neither installs nor preloads the extension;
- a mapped server with `pg_local_cache.port=0`, so it has no RESP secret,
  listener, worker, or client buffers.

The harness creates the same schema and rows on both servers and verifies that
their relevant PostgreSQL settings match. Values are deterministic,
incompressible 3,000-byte text so PostgreSQL cannot turn the stock comparison
into a small compressed-row lookup. A `LOGIN NOSUPERUSER` role executes one
batch of primary-key reads in three modes:

1. stock PostgreSQL using `unnest(bigint[])`, a PK join, and ordered whole-row
   JSON aggregation;
2. the mapped server using the same stock query;
3. the mapped server using `local_cache.mget(regclass, bigint[])`.

All three queries are designed to return the same ordered JSON values. Before
timing, the harness byte-compares the first, middle, and last scalar rows and
checks exact cache accounting; it does not byte-compare every timed batch. SQL
text necessarily differs because stock PostgreSQL has no `mget()` function.

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
- the application role can use the `local_cache` schema and the canonical SQL
  KV function is not a transparent `Custom Scan`;
- a cold SQL lookup produces one miss and fill, followed by a hit;
- the SQL-only server reports zero configured/running RESP workers and zero
  RESP memory.

During every cached throughput and latency window, successful statements must
equal the `sql_cache_hits` delta exactly. Timed misses, fills, and safety
bypasses must remain zero. Direct runs must not change any `sql_cache_*`
counter. Any mismatch fails the run rather than reporting a misleading rate.

### Throughput and latency

Throughput runs use persistent connections and a configurable key-array width.
`operations_per_second` is successful batch TPS multiplied by that exact number
of key positions for every mode; the report also retains batch TPS, key width,
and failed batches. This is KV key throughput, not SQL statement throughput.

Latency runs are separate. They use one scalar `get()` or stock row lookup
designed to return the same row JSON per transaction, persistent connections,
and pgbench latency logs
with deterministic sampling. The report
contains mean, p50, p95, p99, maximum, and sample count for each protocol and
each of the three modes. These are client-observed end-to-end operation
latencies, including PostgreSQL protocol and transaction overhead after the
connection is established. They are not inferred by dividing batch
latency. This is a closed-loop saturation measurement: each client submits its
next transaction after the previous one completes. It does not hold the offered
request rate constant across modes, so it should not be read as a fixed-rate
service-time comparison.

The default latency gate is `MEASURED`: no p99 limit is assumed across unknown
hardware. Set `PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS` to enforce a deployment-
specific ceiling. `PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES` prevents a sparse
sample from passing.

Relative throughput gates default to `1.50` in CI and in the standalone harness. Set
`PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO` and
`PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO` to reject a cached result below
the required fraction of the mapped-direct and stock medians. A lower numeric
value changes a local run's threshold; release evidence validation still
rejects any ratio below `1.50`.

### c4/k8 and c16/k32 snapshot

CI also records a short scaling snapshot on the same runner and the same two
PostgreSQL containers. The c16/k32 result is the strict primary profile: 16
connections and 32 key positions per batch, with three repetitions and all
configured gates. The harness then runs one non-gating c4/k8 repetition for
both prepared and unnamed-extended SQL.

The key-array width applies only to the throughput pass. Latency is always
measured with one scalar key per transaction, so the corresponding latency
profiles are c4/k1 and c16/k1. The generated table reports stock PostgreSQL,
the mapped table with caching off, and caching on with median throughput plus
latency mean, p50, p95, and p99. The environment variable retains its historical
name `PGLC_SQL_ONLY_BENCH_PIPELINE`; for this SQL KV workload it is the keys per
MGET, not a count of SQL statements queued on the wire.

The c4/k8 performance numbers do not decide the build result. Incorrect cache
counters, failed batches, missing raw latency samples, or a wrong key width do
fail the benchmark. The c16/k32 entry reuses the strict primary result.

This snapshot changes connection count and MGET width together. It compares two
operating profiles; it is not a causal concurrency curve. Use a long, repeated
dedicated run before publishing capacity claims.

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
PGLC_SQL_ONLY_BENCH_KEYS=4096 \
PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES=3000 \
PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO=1.50 \
PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO=1.50 \
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
[benchmark scenarios](https://github.com/profundium/pg_local_cache/blob/master/benchmarks/SCENARIOS.md).

## Fairness rules

### SQL KV

- Stock, mapped-direct, and cached runs use the same PostgreSQL major and
  checked comparison settings.
- Schema, generated data, key sequence, role capabilities, connection count,
  client process, protocol, batch width, random seed, and intended ordered JSON
  shape match. Query text differs because stock PostgreSQL has no `mget()`.
- The stock and mapped-direct lanes use the same ordered PK batch query.
- The first, middle, and last stock, mapped-direct, and cached scalar rows are
  byte-compared before timing; every timed batch is not byte-compared.
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

SQL-only latency uses one scalar key read per transaction. RESP latency starts when a
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
[SCENARIOS.md](https://github.com/profundium/pg_local_cache/blob/master/benchmarks/SCENARIOS.md)
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
