# Benchmark workloads

`benchmarks/run.sh` produces `whole-row.json` and `whole-row.md`. The report
contains four independent measurements:

- full-row RESP `GET` against pg_local_cache, Valkey, and Redis;
- ordinary exact-primary-key SQL against mapped and stock PostgreSQL;
- ordinary single-column-primary-key `IN` SQL against mapped and stock
  PostgreSQL;
- a pg_local_cache RESP payload-width sweep.

The separate SQL-only release runner compares `local_cache.mget()` with an
ordered, byte-identical stock PostgreSQL PK batch for prepared and unnamed
extended queries. It also records p50, p95, p99, and mean scalar-key latency.
See
[`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md).

## Run the comparison

```bash
bash benchmarks/run.sh
```

Useful controls:

```bash
PGLC_BENCH_DURATION=120 \
PGLC_BENCH_WARMUP_SECONDS=15 \
PGLC_BENCH_REPETITIONS=3 \
PGLC_BENCH_CONCURRENCY=16 \
PGLC_BENCH_PIPELINE=32 \
PGLC_BENCH_KEYS=16384 \
PGLC_BENCH_ROW_VALUE_SIZE=512 \
PGLC_BENCH_ROW_SQL_IN_KEYS=32 \
PGLC_BENCH_ROW_PAYLOAD_SIZES=64,512,2048 \
bash benchmarks/run.sh
```

Independent regression floors:

- `PGLC_BENCH_ROW_RESP_MIN_OPS` for full-row RESP;
- `PGLC_BENCH_ROW_SQL_MIN_OPS` for exact-key ordinary SQL;
- `PGLC_BENCH_ROW_SQL_IN_MIN_OPS` for ordinary SQL `IN` key throughput;
- `PGLC_BENCH_ROW_WIDTH_MIN_OPS` for the payload-width sweep.

The RESP, scalar SQL, and SQL `IN` floors default to 10,000 operations per
second. `PGLC_BENCH_ROW_SQL_IN_KEYS` defaults to 32 and is capped at 1,024.
Width has no default floor because response sizes are deliberately different.

## Full-row RESP

The test table has a composite primary key. Requests use the KVik-inspired
wire key:

```text
CRUD:database.schema.table:{"pk_column":<json-value>,...}
```

The harness changes JSON member order to exercise canonicalization. It loads
PostgreSQL's exact row JSON into Valkey and Redis, then uses the same clients,
connections, request order, pipeline depth, CPU quota, network, and reply
validation for all three targets.

Before measurement, each target is warmed over the complete keyspace.
pg_local_cache counter deltas must show no timed miss or database read.

## Ordinary SQL

Mapped and stock PostgreSQL receive identical parameterized queries:

- `SELECT *` with a complete primary key;
- columns in a different order;
- composite-key predicates in a different order.

Each operation is one successful `SELECT`, not one pipeline batch. Reports
retain failed-batch counts and exact pg_local_cache counter deltas.

### Ordinary SQL `IN` / `ANY`

A separate table uses one `bigint` primary key. The mapped and stock servers
receive the same prepared 32-key statement:

```sql
SELECT *
FROM public.pg_local_cache_whole_row_select_in_comparison
WHERE id IN ((:key_0)::bigint, ..., (:key_31)::bigint);
```

Each generated statement uses distinct contiguous keys. The report therefore
counts resolved key rows as `batch TPS × pipeline × keys per statement` and
also records statements/s. Before timing, the complete mapped keyspace must
produce one hit per row with no misses, fills, or bypasses. The measured mapped
window has the same strict counter contract. Any mixed hit/miss statement is a
correctness case, not a warm-throughput sample: the executor runs the complete
PostgreSQL child plan instead of merging partial results.

Run the SQL-only comparison for prepared and unnamed extended protocols:

```bash
bash tests/docker_sql_only_smoke.sh
```

That report counts equal-width KV key reads (`batch TPS × keys per MGET`) and
keeps them separate from the scalar-key latency pass. It compares stock
PostgreSQL, mapped PostgreSQL using the same stock batch with caching disabled,
and mapped PostgreSQL using `mget()`.

CI keeps c16/k32 as the strict SQL-only profile and adds a short, non-gating
c4/k8 snapshot on the same servers. Both profiles include prepared and unnamed
extended protocols. Key-array width is used only for throughput; latency is
reported from c4/k1 and c16/k1 passes. Counter mismatches, failed batches, and
invalid latency evidence remain fatal in the snapshot.

## Payload width

The width sweep changes only the text payload size and records actual minimum
and maximum row JSON bytes. It requires zero timed cache misses and database
reads. Rows that exceed the cacheable payload limit are covered by integration
tests rather than folded into warm-cache throughput.

## Reading results

- Treat CI smoke runs as regression checks, not hardware-independent rankings.
- Publish the raw JSON, source revision, image identities, CPU model, resource
  limits, duration, repetitions, and coefficient of variation.
- Compare throughput only within the same protocol and workload; do not
  compare SQL `IN` key ops/s directly with scalar statements/s.
- Compare latency from the dedicated scalar-key pass; batch latency is not
  single-key latency.
