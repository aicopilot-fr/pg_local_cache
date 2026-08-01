# Extended benchmark scenarios

The benchmark suite targets the complete pg_local_cache 1.0.0 API.
`compare.py` is the strict scalar warm-GET comparison. `scenarios.py` adds
workloads whose semantics differ too much to combine into one throughput
ranking. `whole_row.py` measures the KVik-compatible full-row API separately.
The default `benchmarks/run.sh` runs all three and writes:

- `comparison.json` and `comparison.md` for identical warm RESP GETs;
- `scenarios.json` and `scenarios.md` for the scenarios below;
- `whole-row.json` and `whole-row.md` for full-row RESP, SQL and width lanes.

Set `PGLC_BENCH_RUN_SCENARIOS=0` to run only the warm comparison. All common
`PGLC_BENCH_*` sizing variables still apply. Extended duration defaults to the
smaller of the main duration and 30 seconds, so the one-second Docker smoke
remains short:

```bash
PGLC_BENCH_SCENARIO_DURATION=30 \
PGLC_BENCH_SCENARIO_REPETITIONS=3 \
PGLC_BENCH_STAMPEDE_ROUNDS=10 \
bash benchmarks/run.sh
```

Set `PGLC_BENCH_RUN_WHOLE_ROW=0` to omit the whole-row suite. Its duration and
repetitions can be bounded independently:

```bash
PGLC_BENCH_ROW_DURATION=30 \
PGLC_BENCH_ROW_REPETITIONS=3 \
PGLC_BENCH_ROW_VALUE_SIZE=512 \
PGLC_BENCH_ROW_PAYLOAD_SIZES=64,512,2048 \
bash benchmarks/run.sh
```

Once single-flight is enabled in the server, turn its observation into a
regression gate with `PGLC_BENCH_REQUIRE_SINGLE_FLIGHT=1`. Every wave must then
perform exactly one SQL read.

## Scenarios and counters

| Scenario | Timed operation | Required correctness evidence |
|---|---|---|
| RESP warm GET | Repeated GET over the fully preloaded working set | Every value matches; zero cache misses and SQL reads |
| RESP cold GET | Each existing key is read exactly once after namespace invalidation | One cache miss and one SQL read per key |
| Same-key stampede | All authenticated clients issue one cold GET from a barrier | Every value matches; SQL reads per wave is reported |
| RESP SET | Each configured key is created exactly once | Every reply is `OK`; PostgreSQL write counters are captured |
| RESP DEL | Every key created by the preceding SET is deleted exactly once | Every reply is `1`; no keys remain |
| Direct SQL SELECT | Prepared, parameterized stock-PostgreSQL lookup | Zero failed pgbench batches |
| Repeated mapped SQL write | Prepared UPDATEs committed against one disjoint per-client key set; keys are warm only before the run | The authoritative table is read after commit and every RESP key is compared with it |
| Ordinary-SQL prepared lookup | The same `SELECT value ... WHERE id = :key` under `pgbench -M prepared` in direct and cached sessions | Dedicated `sql_cache_*` deltas and failed batches are recorded |
| Ordinary-SQL unnamed extended lookup | The same SELECT under `pgbench -M extended`, which does not reuse a named/server-side prepared statement | Its own direct/cached results, counters and throughput gate are recorded; no values are pooled with the prepared lane |
| Ordinary-SQL cold self-fill | Two identical SELECTs through each lane's own protocol after invalidation | Exactly one SQL-cache miss, one fill, then one hit per protocol probe |

The mutation comparison deliberately includes Valkey and Redis with
persistence disabled. It uses byte-identical commands and the same client,
but is not durability-equivalent: every pg_local_cache SET/DEL includes a
PostgreSQL transaction, WAL and commit-time invalidation.

## Whole-row and KVik-compatible lanes

`whole_row.py` does not alter or reuse the scalar throughput samples. The
`resp_full_row` lane creates a composite-primary-key table and reads it through
`CRUD:database.schema.table:{pk-json}` keys. JSON key members are intentionally
sent in a different order than the primary key. PostgreSQL `row_to_json` output
for every row is then installed byte-for-byte in Valkey and Redis. The same
client processes, connections, key sequence, pipeline, and exact per-key reply
validation are used for all three targets. Before measurement, the harness
globally invalidates pg_local_cache and flushes each external target so entries
from scalar or earlier lanes cannot consume the measured whole-row keyspace.
It then repeats complete pg_local_cache warm/validation passes until one whole
pass increments neither `cache_misses` nor `database_reads`. Those untimed
stabilization reads are reported separately and never counted as throughput.

The `resp_payload_width_sweep` lane measures only pg_local_cache at each
configured text payload size. It records the resulting minimum/maximum JSON
response bytes and requires zero cache misses or database reads in every timed
window. Values are capped at 3000 text bytes so this is a warm-cache width
sweep; oversized safe-bypass behavior belongs to integration tests, not a
mislabelled warm hit result.

Three ordinary SQL lanes compare the mapped server with stock PostgreSQL using
identical prepared/pipelined SELECT text:

- `select_star` reads the complete native row;
- `reordered_projection` reads multiple columns in a non-table order;
- `composite_predicate_reordered` reverses the composite-PK predicate order.

The full-row RESP and SQL gates are independent from both the scalar gate and
each other. `PGLC_BENCH_ROW_RESP_MIN_OPS` and
`PGLC_BENCH_ROW_SQL_MIN_OPS` default to 10,000 ops/s.
`PGLC_BENCH_ROW_WIDTH_MIN_OPS` defaults to zero because response size is the
independent variable; set it explicitly for a hardware-specific regression
gate. A failure is emitted in `whole-row-failure.json/.md`, without deleting
the scalar or extended-scenario reports.

## Ordinary SQL fast-path feature lane

The optional lane does not introduce a cache-specific function, key format or
driver. Both modes run this ordinary prepared statement against the same
PostgreSQL container:

```sql
SELECT value
FROM public.pg_local_cache_comparison
WHERE id = $1;
```

Only a one-time session setup differs. Both protocol lanes are emitted as
`SKIPPED`, rather than as zero throughput, unless the fast-path setup is
explicitly supplied.
Enable the implemented interception mode with its session GUC:

```bash
PGLC_BENCH_SQL_DIRECT_SETUP='SET pg_local_cache.sql_cache = off' \
PGLC_BENCH_SQL_FAST_PATH_SETUP='SET pg_local_cache.sql_cache = on' \
bash benchmarks/run.sh
```

The harness validates the setup as a simple `SET guc.name = value` and passes
it through libpq `PGOPTIONS`, so the mode is active when every persistent
session connects and contributes no statement to the timed workload. Each
protocol has a direct/cache pair with identical scripts, parameters, random
seeds, connection count and pipeline depth. The fast-path environment variable
is intentionally a setup statement, not a replacement query: that prevents the
benchmark from quietly measuring a custom function or a different application
API.

Two protocol results are deliberately kept separate:

- `sql_cached_fast_path` uses `pgbench -M prepared`. Parse analysis is reused
  starting with the second execution, so this represents applications or
  drivers that explicitly or automatically prepare a hot statement.
- `sql_cached_extended_protocol` uses `pgbench -M extended`. It exercises the
  ordinary unnamed extended-query path (`Parse/Bind/Execute` per execution),
  which is the closer drop-in model for a driver issuing parameterized ad-hoc
  SQL without statement reuse. Drivers differ in auto-prepare policy, so this
  lane is not presented as a universal driver benchmark.

Adding the extended direct/cache pair costs approximately
`2 × duration × repetitions`, plus process startup (and the same bounded
warmup, when configured). It reuses `PGLC_BENCH_SCENARIO_DURATION`, so a
one-second Docker smoke adds roughly two timed seconds rather than silently
switching to a longer workload.

When enabled, the lane is fail-closed: direct mode must leave the dedicated
`sql_cache_hits`, `sql_cache_misses`, `sql_cache_fills` and
`sql_cache_bypasses` counters untouched. Cached warm mode must report one SQL
cache hit per successful lookup and zero misses, fills and safety bypasses.
Before throughput starts, a cold probe invalidates the namespace and requires
the first ordinary SELECT to miss and self-fill and the second to hit. Merely
accepting the GUC without using the CustomScan therefore cannot pass.
The prepared cached lane retains its independent throughput gate:
`PGLC_BENCH_SQL_MIN_OPS` defaults to `10000`. The unnamed extended lane has a
separate `PGLC_BENCH_SQL_EXTENDED_MIN_OPS`; by default it inherits the prepared
threshold, so both defaults are 10k without combining their medians. A miss in
either enabled protocol-specific gate fails the scenario process even if the
RESP gate passes.

Counter validation is also per protocol. In each timed direct lane, all four
`sql_cache_*` deltas must remain zero. In each cached lane, cache hits must
equal that lane's successful operations, while miss/fill/bypass deltas must be
zero. Untimed value probes use explicit `PREPARE` for the prepared lane and
psql's `\bind`/`\g` unnamed extended protocol for the extended lane.

## Output semantics

- `operations_per_second` always counts validated commands or SQL statements,
  never pipeline batches. `successful_batches` is retained separately for
  pgbench results.
- Prepared and unnamed-extended SQL runs have a `query_protocol` field and
  separate `throughput_gate` objects. Their medians and counters are never
  summed, averaged or used as repetitions of one another.
- Fixed cold/SET/DEL timers start after every process has connected,
  authenticated and reached the barrier. Connection and AUTH time is excluded.
- RESP latency starts when a pipeline is sent and ends when each response is
  decoded. It includes queueing behind earlier responses in that pipeline and
  is not server-only service time.
- `database_reads_per_round` is the stampede coalescing metric. Its
  single-flight ideal is `1`; total client requests per round equal configured
  concurrency.
- Cache and database counter fields suffixed with
  `_during_measurement` are deltas around only the named timed window.
- SQL write throughput is shown both for the mapped database and a separate
  stock PostgreSQL container. `mapped_to_stock_throughput_ratio` quantifies
  trigger/commit bookkeeping under the same pgbench workload. The fixed key
  set starts warm but is not re-warmed after the first invalidation, so this
  number must not be presented as active-cache invalidation throughput. A
  re-warmed invalidation wave remains a separate follow-up benchmark.
- SQL write correctness is stronger than a counter check: after each measured
  run, the harness reads the committed source of truth and validates every
  cached key. Any stale or wrong value fails the suite.
- Container CPU settings are quotas, not affinity. Use an otherwise idle host
  with pinned, isolated CPUs and several repetitions for publishable numbers.
