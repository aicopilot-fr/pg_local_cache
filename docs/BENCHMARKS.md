---
layout: doc
title: PostgreSQL cache benchmarks
description: Reproducible measurements of ordinary exact-key SELECT and RESP GET against stock PostgreSQL, Valkey and Redis.
section: Benchmarks
permalink: /docs/BENCHMARKS.html
---

# pg_local_cache benchmarks

The published tables cover the read interfaces exercised by the current
comparison runner:

- ordinary exact-primary-key `SELECT` through the PostgreSQL protocol;
- whole-row RESP2 `GET` against the same PostgreSQL-backed rows.

The two suites measure different operations and are reported separately.
Write, rollback, DDL, and invalidation semantics are verified by integration
tests instead of being inferred from read throughput.

## Current CI result (`fe2d23c`)

[CI run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805)
measured [source `fe2d23c`](https://github.com/profundium/pg_local_cache/commit/fe2d23c87ddc7e523ada2951376ebcb7d8570fb1)
and passed every independent gate. The exact
[`comparison-smoke.zip`](../assets/benchmark-evidence/fe2d23c/comparison-smoke.zip)
is preserved with raw JSON and rendered Markdown. It is Actions artifact
`8851825673` and has ZIP digest
`sha256:fc624e7ebed11b10c8470d11e7d2a91855813e04f9fb809e62e4f0852f7c8a76`.

### Ordinary SQL

Both PostgreSQL targets used the complete prepared command template shown
below; pgbench replaced `:key` with each measured key. Only the mapped server
loaded `pg_local_cache`; the stock server did not install or preload the
extension.

| Command template | Mapped cache ops/s | Stock PostgreSQL ops/s | Mapped/stock |
|---|---:|---:|---:|
| `SELECT * FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 126,710 | 65,257 | 1.94x |
| `SELECT metadata, payload, enabled, amount, note, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 123,051 | 65,236 | 1.89x |
| `SELECT payload, metadata, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE id = :key AND tenant_id = 7;` | 131,017 | 71,398 | 1.84x |

The mapped working set was filled and stabilized before measurement. Each
timed mapped operation produced one exact cache hit; misses, fills, bypasses,
and failed batches remained zero. CI gates the 10,000 mapped ops/s floor,
result integrity, and counter accounting. The mapped/stock ratios are displayed
for context and do not decide the gate.

### RESP2 GET

The same RESP2 command bytes and expected row bytes were used for all three
targets. The command below is the concrete key for row `id=1` in the measured
key stream; subsequent operations changed only the `id` value.

| Target | Command | Ops/s | p50 | p95 | p99 | Errors |
|---|---|---:|---:|---:|---:|---:|
| pg_local_cache | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 150,569 | 0.176 ms | 0.324 ms | 0.394 ms | 0 |
| Valkey 9.1.1 | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 201,248 | 0.137 ms | 0.205 ms | 0.281 ms | 0 |
| Redis 8.8.1 | `GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:{"id":1,"tenant_id":7}` | 205,430 | 0.134 ms | 0.200 ms | 0.271 ms | 0 |

Valkey and Redis persistence was disabled because this lane measures warm cache
reads, not durable writes. Every response was decoded and compared byte-for-byte
with PostgreSQL's row JSON. Timed pg_local_cache operations produced zero cache
misses and zero source-table reads.

## Recorded workload

The raw report records the runner and container identities in addition to these
effective settings:

| Setting | Value |
|---|---|
| PostgreSQL | 16.14 on both targets |
| Runner CPU | AMD EPYC 7763; 4 logical CPUs visible |
| CPU quotas | 2 client CPUs; 2 CPUs per server target |
| Memory limits | 3 GiB client; 1 GiB per server target |
| Clients | 4 |
| Pipeline depth | 8, up to 32 operations in flight |
| Keys / cache entries | 128 / 128 |
| Row text payload | 128 bytes |
| Timed repetitions | one 1-second smoke repetition |
| Timed warmup | 0 seconds after explicit full-working-set stabilization |

This short shared-runner run is useful as a correctness and regression smoke.
One repetition is not a capacity study, and the displayed relative ratios must
not be generalized to different rows, concurrency, hardware, or storage.

## Ordinary SQL methodology

`benchmarks/whole_row.py` creates the same composite-primary-key table and
deterministic rows on two PostgreSQL 16 servers. It uses the same SQL template
and key on both targets with pgbench's prepared extended protocol. Each client
batch pipelines eight executions, so
`operations_per_second` counts completed row lookups rather than batches.

Before timing, the runner:

1. verifies source row counts and key ranges on both servers;
2. compares a result sample from mapped and stock PostgreSQL;
3. fills the mapped cache and repeats a full-keyspace pass until it observes no
   miss or database read;
4. resets counters immediately before each measured lane.

During each mapped SQL lane, successful operations must equal the
`sql_cache_hits` delta exactly. Any miss, fill, safety bypass, failed batch, or
result mismatch fails the run instead of producing a publishable rate.

## RESP methodology

The whole-row RESP comparison loads PostgreSQL's exact `row_to_json` bytes into
Valkey and Redis. All three targets then receive the same client implementation,
key order, connection count, pipeline depth, CPU quota, Docker network, and
reply validation. Target order rotates when more than one repetition is used.

Connection and authentication setup happen before the timed interval. Latency
starts when a pipeline is sent and ends when every reply in that pipeline has
been decoded, so the percentiles include queueing behind earlier commands in
the pipeline. They are client-observed end-to-end values, not server execution
times.

## Run the current comparison

Run both current interfaces with:

```bash
bash benchmarks/run.sh
```

To reproduce the short CI profile exactly:

```bash
PGLC_BENCH_DURATION=1 \
PGLC_BENCH_WARMUP_SECONDS=0 \
PGLC_BENCH_REPETITIONS=1 \
PGLC_BENCH_CONCURRENCY=4 \
PGLC_BENCH_PIPELINE=8 \
PGLC_BENCH_KEYS=128 \
PGLC_BENCH_CACHE_ENTRIES=128 \
PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1 \
PGLC_BENCH_SERVER_CPUS=2 \
PGLC_BENCH_CLIENT_CPUS=2 \
PGLC_BENCH_SERVER_MEMORY=1g \
PGLC_BENCH_ROW_RESP_MIN_OPS=10000 \
PGLC_BENCH_ROW_SQL_MIN_OPS=10000 \
PGLC_BENCH_ROW_WIDTH_MIN_OPS=0 \
PGLC_BENCH_OUTPUT_DIR="$PWD/benchmark-results/comparison" \
bash benchmarks/run.sh
```

The output directory receives:

- `whole-row.json`: source revision, exact commands, environment, image
  identities, configuration, every repetition, counter deltas, and gates;
- `whole-row.md`: a rendered summary of the same run;
- a structured failure report if the runner stops before satisfying its
  correctness contract.

Use a clean Git commit: the runner records `-dirty` beside the source revision
when local changes are present.

## Publishing results

For a result intended as more than a CI smoke:

1. Pin PostgreSQL, Valkey, Redis, and client images by digest.
2. Place client and servers on separate physical CPU sets; do not rely only on
   container quotas.
3. Record CPU model, governor, memory, kernel, container runtime, storage, and
   swap policy.
4. Keep schema, payload bytes, key distribution, connection count, protocol,
   and command text identical within each comparison.
5. Use a meaningful warmup and at least three long repetitions; publish every
   repetition and its coefficient of variation.
6. Report throughput together with p50, p95, p99, sample count, and the latency
   semantics.
7. Retain raw reports, failures, counter deltas, harness checksums, and image
   identities under an immutable source revision.
8. Repeat the run on the intended HA, storage, connection-pool, row-width, and
   CPU profile before setting a service objective.

GitHub-hosted runners are useful for correctness and regression detection, but
variable CPU scheduling makes them unsuitable as the sole source of a capacity
claim.

For implementation limits that affect interpretation, see the
[technical reference]({{ '/docs/TECHNICAL.html' | relative_url }}) and the exact
[scenario definitions](https://github.com/profundium/pg_local_cache/blob/master/benchmarks/SCENARIOS.md).
