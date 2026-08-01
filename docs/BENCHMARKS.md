# pg_local_cache benchmarks

The benchmark suite exists to answer separate questions without hiding their
different semantics behind one ops/s number:

- how fast is a warm whole-row `GET` compared with Valkey and Redis when the
  client, payload and network are identical;
- how fast is an ordinary SQL primary-key lookup through the transparent
  cache compared with the normal PostgreSQL plan;
- does the SQL-only deployment retain the same fast path with no RESP listener,
  token or workers;
- what do cold fills, same-key fan-in, transactional writes and invalidation
  cost;
- how throughput and latency change as the full row grows.

Each read lane has its own integrity checks and gate. Prepared and unnamed
extended SQL are never averaged together. RESP writes are never presented as
durability-equivalent to in-memory Valkey/Redis writes.

## Headline long-run reference

The long reference run used three measured repetitions of 120 seconds after a
15-second warmup, 16 persistent clients, pipeline 32, 16,384 warm keys, four
`pg_local_cache` workers and separate two-CPU server/client quotas.

| Lane | Target | Median ops/s |
|---|---|---:|
| Whole-row RESP GET | **pg_local_cache** | **106,948** |
| Whole-row RESP GET | Valkey | 118,387 |
| Whole-row RESP GET | Redis | 123,790 |
| Ordinary SQL prepared | **pg_local_cache cached fast path** | **70,275** |
| Ordinary SQL unnamed extended | **pg_local_cache cached fast path** | **18,985** |
| Transactional RESP SET | pg_local_cache | 5,456 |
| Transactional RESP DEL | pg_local_cache | 5,520 |

The whole-row RESP result is about 90% of Valkey in this environment. Unlike
Valkey/Redis, `pg_local_cache` derives the value from the authoritative table
and invalidates it with the PostgreSQL transaction. `SET` and `DEL` each
include a real PostgreSQL transaction, WAL and commit-time invalidation, so
their numbers belong in a separate write table.

The suite now packages raw JSON, Markdown and checksums with CI/release
artifacts. Older headline values are retained as a reference baseline; a new
publication-quality run should replace them only as a complete, immutable
report, never by selecting the best repetition.

## Current 1.0.0 CI smoke evidence

The clean 1.0.0 baseline was also exercised by
[CI run 30710976393](https://github.com/aicopilot-fr/pg_local_cache/actions/runs/30710976393)
at commit `08b788fb6fbcfe295b5ff7f931ab52136c6799fa`.

The smoke intentionally ran for only one second with one repetition, four
clients, pipeline 8, 128 keys, one extension worker and two-CPU quotas. It is a
correctness/regression gate, not a publishable engine ranking:

| Smoke lane | pg_local_cache | Comparison |
|---|---:|---:|
| Full-row RESP GET | 117,763 ops/s | Valkey 146,142; Redis 143,288 |
| Ordinary SQL `SELECT *` | 67,487 ops/s | stock PostgreSQL 51,482 |
| Reordered SQL projection | 69,221 ops/s | stock PostgreSQL 50,077 |
| Composite predicate reordered | 76,194 ops/s | stock PostgreSQL 54,415 |
| Prepared scalar projection | 97,762 ops/s | same server, cache off 56,193 |
| Unnamed extended projection | 19,801 ops/s | same server, cache off 27,484 |

All cached SQL operations were accounted as hits with zero timed miss, fill or
safety bypass. The prepared/extended difference is expected: unnamed extended
mode performs Parse/Bind/Execute for every lookup, while prepared mode reuses
server-side parse analysis.

## Dedicated SQL-only benchmark

`benchmarks/sql_only.py` runs against `compose.sql-only.yaml`, where:

- `pg_local_cache.port=0`;
- no RESP secret is mounted;
- zero RESP workers exist;
- an actual `LOGIN NOSUPERUSER` role issues ordinary `SELECT *` by complete PK;
- a whole-row mapping is installed with `attach_table`;
- the harness proves one miss, one fill and then one hit;
- it verifies the full source row count/key range and byte-identical
  direct-vs-cached whole rows for the first, middle and last keys;
- prepared and unnamed extended lanes compare cache off/on in the same
  container, query, keyspace and client configuration;
- successful timed statements must equal the `sql_cache_hits` delta;
- miss, fill and bypass deltas must remain zero during the warm measurement;
- both protocols independently enforce `>=10,000 ops/s` by default.

Run the Docker SQL-only correctness suite and benchmark together:

```bash
PGLC_SQL_ONLY_BENCH_DURATION=30 \
PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS=5 \
PGLC_SQL_ONLY_BENCH_REPETITIONS=3 \
PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS=10000 \
PGLC_SQL_ONLY_BENCH_OUTPUT_DIR="$PWD/benchmark-results/sql-only" \
bash tests/docker_sql_only_smoke.sh
```

The command writes `sql-only.json` and `sql-only.md`. GitHub CI uploads the
same files even on failure.

## Full comparison

The default full suite starts PostgreSQL with `pg_local_cache`, stock
PostgreSQL, Valkey, Redis and a separate benchmark client:

```bash
bash benchmarks/run.sh
```

Default workload:

| Parameter | Value |
|---|---:|
| Measured duration | 120 s per target/repetition |
| Warmup | 15 s |
| Repetitions | 3 |
| Persistent clients | 16 |
| Pipeline depth | 32 |
| Warm keys | 16,384 |
| Scalar value | 128 bytes |
| Whole-row payload sweep | 64, 512, 2,048 text bytes |
| Read gate | 10,000 ops/s per independent lane |

Output files:

- `comparison.json` / `comparison.md`: byte-identical scalar RESP comparison
  and separate stock PostgreSQL reference;
- `whole-row.json` / `whole-row.md`: KVik-style full-row RESP, width sweep and
  ordinary whole-row SQL projections;
- `scenarios.json` / `scenarios.md`: cold/warm reads, single-flight, writes,
  prepared/extended SQL and post-commit validation;
- failure reports when a harness exits before satisfying its contract.

The JSON includes every repetition, median/min/max/CV, client-observed
p50/p95/p99, operation counts, errors, cache/database counter deltas, image
identities, source revision and a SHA-256 of each harness.

## Fairness rules

### RESP comparison

`pg_local_cache`, Valkey and Redis receive:

- one byte-identical encoded `GET` stream;
- the same per-key full-row JSON bytes;
- one Python/multiprocess client implementation;
- the same number of connections and pipeline depth;
- one Docker bridge network and identical client CPU quota;
- a rotated target order between repetitions.

Every response is validated. A measured `pg_local_cache` warm run fails if it
performs a database read or cache miss. Valkey/Redis RDB and AOF are disabled
because this lane measures cache reads, not durability.

### Ordinary SQL comparison

SQL is intentionally separate from RESP because libpq extended protocol has
different framing and transaction semantics. Mapped and direct lanes use the
same PostgreSQL container, query text, parameters, clients, pipeline and seed;
only `SET pg_local_cache.sql_cache=on|off` differs. Whole-row lanes also execute
the same query against a separate stock PostgreSQL reference.

### Latency

RESP latency measures from sending a complete pipeline batch until each reply
finishes, including queueing behind earlier replies. Deterministic per-client
Algorithm R reservoirs cover the entire interval. This is client-observed
pipeline-completion latency, not isolated server service time.

`pgbench` reports average transaction-batch latency. Operations/s is batch TPS
multiplied by the number of validated lookups in that pipeline batch.

## Publication-quality run

GitHub hosted runners are useful for regression detection but provide CPU
quotas, not affinity. For a result intended for a public comparison:

1. pin server and client to separate physical CPU sets;
2. disable swap and unrelated workloads;
3. record CPU model/governor, memory, kernel, Docker and storage;
4. use digest-pinned PostgreSQL, Valkey and Redis images;
5. keep the default long warmup and at least three 120-second repetitions;
6. inspect CV and CPU-quota saturation instead of reporting only median;
7. retain the complete JSON and Markdown, harness checksums and commit SHA;
8. publish every run, not only the fastest one.

The benchmark workflow can also be triggered manually and runs monthly to
catch regressions. Release artifacts preserve the CI evidence that would
otherwise expire from Actions storage.

See [extended scenario definitions](../benchmarks/SCENARIOS.md) and the
[technical reference](TECHNICAL.md) for cache semantics and limits.
