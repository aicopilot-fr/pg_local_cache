# Benchmark workloads

`benchmarks/run.sh` produces `whole-row.json` and `whole-row.md`. The report
contains three independent measurements:

- full-row RESP `GET` against pg_local_cache, Valkey, and Redis;
- ordinary primary-key SQL against mapped and stock PostgreSQL;
- a pg_local_cache RESP payload-width sweep.

The separate SQL-only runner compares cached, cache-disabled, and stock
PostgreSQL for prepared and unnamed extended queries, including p50, p95,
p99, and mean one-operation latency. See
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
PGLC_BENCH_ROW_PAYLOAD_SIZES=64,512,2048 \
bash benchmarks/run.sh
```

Independent regression floors:

- `PGLC_BENCH_ROW_RESP_MIN_OPS` for full-row RESP;
- `PGLC_BENCH_ROW_SQL_MIN_OPS` for ordinary SQL;
- `PGLC_BENCH_ROW_WIDTH_MIN_OPS` for the payload-width sweep.

The RESP and SQL floors default to 10,000 operations per second. Width has no
default floor because response sizes are deliberately different.

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

Run the SQL-only comparison for prepared and unnamed extended protocols:

```bash
bash tests/docker_sql_only_smoke.sh
```

That report keeps pipelined throughput separate from a pipeline-depth-one
latency pass. It compares stock PostgreSQL, mapped PostgreSQL with caching
disabled, and mapped PostgreSQL with caching enabled.

CI keeps c16/p32 as the strict SQL-only profile and adds a short, non-gating
c4/p8 snapshot on the same servers. Both profiles include prepared and unnamed
extended protocols. The profile pipeline is used only for throughput; latency
is reported from c4/p1 and c16/p1 passes. Counter mismatches, failed batches,
and invalid latency evidence remain fatal in the snapshot.

## Payload width

The width sweep changes only the text payload size and records actual minimum
and maximum row JSON bytes. It requires zero timed cache misses and database
reads. Rows that exceed the cacheable payload limit are covered by integration
tests rather than folded into warm-cache throughput.

## Reading results

- Treat CI smoke runs as regression checks, not hardware-independent rankings.
- Publish the raw JSON, source revision, image identities, CPU model, resource
  limits, duration, repetitions, and coefficient of variation.
- Compare throughput only within the same protocol and workload.
- Compare latency from the dedicated one-operation pass; pipelined batch
  latency is not single-request latency.
