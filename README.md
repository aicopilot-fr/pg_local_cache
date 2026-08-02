# pg_local_cache

`pg_local_cache` is a PostgreSQL 16 extension that caches complete rows for
supported primary-key lookups in PostgreSQL shared memory. Applications keep
using parameterized SQL through libpq, JDBC, Npgsql, psycopg, or an ORM. No
cache-specific driver is required.

Attached tables use transaction-aware invalidation. Source writes fence affected
entries before commit visibility, and rollback never exposes uncommitted row
data. PostgreSQL remains authoritative: a missing, unsafe, malformed, or
oversized entry runs the normal source plan.

The current implementation supports PostgreSQL 16 on Linux, one configured
database, and one writable primary. It is a narrow primary-key fast path, not a
general query cache.

## Capabilities

| Capability | Behavior |
|---|---|
| Ordinary SQL | Supported primary-key `SELECT` statements can use the cache without changing the application protocol. |
| Whole rows | Each entry stores one versioned PostgreSQL composite row. |
| Transactional invalidation | `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` fence affected entries before commit visibility. |
| Bounded extension memory | Entry capacity, client slots, and deterministic extension allocations are fixed at startup. |
| Optional RESP2 | Trusted internal clients can use authenticated whole-row `GET`, `SET`, and `DEL`. |
| Operations | SQL metrics, health checks, Prometheus rules, and a Grafana dashboard are included. |

## Docker quick start

Requirements: Docker with Compose v2 and OpenSSL.

```bash
git clone https://github.com/aicopilot-fr/pg_local_cache.git
cd pg_local_cache

install -d -m 0700 secrets
openssl rand -base64 36 | tr -d '\n' > secrets/postgres_password
chmod 0600 secrets/postgres_password

docker compose -f compose.sql-only.yaml \
  up --detach --build --wait postgres
```

Open `psql`:

```bash
docker compose -f compose.sql-only.yaml \
  exec postgres psql --username postgres --dbname app
```

Create and attach a table:

```sql
CREATE TABLE public.items (
    id bigint PRIMARY KEY,
    value text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    metadata jsonb
);

INSERT INTO public.items VALUES
    (1, 'hello', true, '{"source":"postgres"}');

SELECT local_cache.attach_table('public.items'::regclass);
```

Application code continues to issue an ordinary parameterized query:

```sql
SELECT *
FROM public.items
WHERE id = $1;
```

Warm supported reads can use `Custom Scan (pg_local_cache_sql)`. Verify the
plan and health with a literal key in `psql`:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT * FROM public.items WHERE id = 1;

SELECT local_cache.health();
SELECT * FROM local_cache.metrics();
```

## SQL API

`local_cache.attach_table()` discovers the complete primary key, records a
whole-row mapping, and installs extension-owned invalidation triggers:

```sql
BEGIN;
SET LOCAL lock_timeout = '2s';
SELECT local_cache.attach_table('public.items'::regclass);
COMMIT;
```

The transparent SQL path accepts:

- one attached permanent table without inheritance, partitioning, or RLS;
- equality predicates for every primary-key column, including composite keys;
- constants or external parameters;
- `SELECT *` or direct column projections, including aliases and reordered
  projections;
- no limit, or a constant `LIMIT 1`.

Unsupported query shapes use PostgreSQL's normal plan. So do
`REPEATABLE READ`, `SERIALIZABLE`, recovery, and reads after the current
transaction writes an attached table. A nonexistent key returns the normal
empty SQL result after consulting the source table.

Application roles need only their normal source-table privileges. They do not
need access to the `local_cache` schema to benefit from a cached `SELECT`.
Administrative functions are separate:

```sql
SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.reconcile_all();
SELECT local_cache.detach_table('public.items'::regclass);
```

See the [technical reference](docs/TECHNICAL.md#planner-and-executor-fast-path)
for the exact planner, snapshot, and type rules.

## Install on an existing server

Use the source archive on a compatible build host, or the binary archive only
for its labelled PostgreSQL, distribution, and architecture combination. The
[existing-database guide](docs/INSTALL_EXISTING.md) covers prerequisites,
preflight, online staging, restart, HA, verification, and rollback.

The first installation requires one restart because
`shared_preload_libraries` is evaluated at postmaster startup. File staging and
configuration validation stay online. The installer's 30-second setting is a
warning target; actual interruption depends on shutdown, recovery, and client
reconnection.

## Optional RESP2 endpoint

SQL-only mode sets `pg_local_cache.port=0` and starts no RESP workers. RESP mode
uses the same shared cache and invalidation machinery, but has a separate
security boundary: one worker role and one shared token cover every accepted
mapping, with no TLS or per-client PostgreSQL ACL context.

Keep the listener on loopback or behind an authenticated TLS proxy. A whole-row
key has this form:

```text
CRUD:database.schema.table:{"pk_column":<json-scalar>,...}
```

`GET` returns the complete row as JSON and reads the source table on a cache
miss. Writable mappings expose PostgreSQL-backed `SET` and `DEL`. See the
[wire API and compatibility boundary](docs/TECHNICAL.md#resp2-wire-api) and the
[existing-server RESP setup](docs/INSTALL_EXISTING.md#optional-resp-mode).

## Benchmarks

The SQL-only suite compares stock PostgreSQL, the mapped server with caching
disabled, and the cached path under the same schema, rows, query, role,
PostgreSQL version, and client settings. Prepared and unnamed extended protocols
are reported separately; latency uses a separate one-operation pass.

### CI regression snapshot

The table below is pinned to [source `9cf12e3`](https://github.com/aicopilot-fr/pg_local_cache/commit/9cf12e34bee512e4d453e117c39ca8eb140afd4d)
and [CI run 30729192604](https://github.com/aicopilot-fr/pg_local_cache/actions/runs/30729192604):
three repetitions, 4,096 keys, 128-byte payloads. Throughput uses c16/p32;
latency is a separate c16/p1 pass.

| Protocol | Path | Median ops/s | Mean | p50 | p95 | p99 |
|---|---|---:|---:|---:|---:|---:|
| Prepared | Stock PostgreSQL 16 | 37,054 | 1.200 ms | 1.070 ms | 2.501 ms | 3.982 ms |
| Prepared | Mapped, cache off | 37,310 | 1.157 ms | 1.038 ms | 2.486 ms | 4.239 ms |
| Prepared | Cache on | 34,662 | 1.160 ms | 1.040 ms | 2.459 ms | 3.989 ms |
| Extended | Stock PostgreSQL 16 | 16,595 | 2.183 ms | 1.954 ms | 4.501 ms | 6.162 ms |
| Extended | Mapped, cache off | 16,704 | 2.193 ms | 1.945 ms | 4.501 ms | 6.145 ms |
| Extended | Cache on | 14,941 | 2.214 ms | 2.016 ms | 4.585 ms | 6.366 ms |

Prepared cache-on was 0.94x stock and 0.93x mapped cache-off. Extended
cache-on was 0.90x stock and 0.89x mapped cache-off in this hot-table run.
The same artifact includes a non-gating c4/p8 snapshot:

| Protocol | Stock ops/s | Cache off ops/s | Cache on ops/s | Cache/stock | Latency profile | Stock p99 | Cache-off p99 | Cache-on p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Prepared | 32,716 | 32,834 | 31,071 | 0.95x | c4/p1 | 1.365 ms | 1.669 ms | 1.668 ms |
| Extended | 15,065 | 14,906 | 14,012 | 0.93x | c4/p1 | 1.767 ms | 1.115 ms | 1.272 ms |

The same run also contains a one-second, one-repetition c4/p8 RESP smoke test
using byte-identical whole-row JSON. This is a separate interface and workload:

| Target | Median ops/s | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| pg_local_cache 1.0.0 | 165,495 | 0.157 ms | 0.290 ms | 0.507 ms |
| Valkey 9.1.1 | 214,310 | 0.118 ms | 0.232 ms | 0.329 ms |
| Redis 8.8.1 | 201,990 | 0.107 ms | 0.265 ms | 0.420 ms |

A separate one-repetition prepared SQL smoke measured the mapped cache path at
113,799–124,305 ops/s and stock PostgreSQL at 62,254–67,099 ops/s across three
whole-row projection shapes. Its duration and harness differ from the repeated
SQL-only result, so the two sets are not pooled.

GitHub-hosted measurements are regression evidence, not capacity claims. A
published snapshot must link the source SHA and run, record configuration and
all repetitions, and retain raw JSON. The default 10,000 ops/s floor is only a
test threshold.

See [benchmark methodology](docs/BENCHMARKS.md) and
[scenario definitions](benchmarks/SCENARIOS.md).

## Monitoring

`local_cache.metrics()` exposes typed cache, memory, worker, client,
invalidation, backpressure, and mapping counters. The optional stack adds
postgres_exporter, Prometheus rules, container memory signals, and a provisioned
Grafana dashboard. Start with the [monitoring and OOM guide](docs/MONITORING.md).

## Releases

Download source, platform-labelled binaries, checksums, and CI evidence from
[GitHub Releases](https://github.com/aicopilot-fr/pg_local_cache/releases). Never
use a binary archive on a different PostgreSQL major, distribution, or
architecture; build the source archive against the target PGXS instead.

## Current limits

- PostgreSQL 16 on Linux only.
- One configured database and one writable primary per extension instance.
- Permanent, non-partitioned tables with a supported primary key; no views,
  inheritance, or RLS.
- Encoded cache entries are limited to 8 KiB; oversized rows use PostgreSQL.
- At most 128 mappings and 16 primary-key columns per mapping.
- No TTL, Redis Cluster, Lua, Pub/Sub, multi-primary, or standby cache serving.
- RESP authentication is a shared-token boundary, not PostgreSQL user
  authentication.

## Documentation

- [Install on an existing PostgreSQL server](docs/INSTALL_EXISTING.md)
- [SQL, consistency, security, and configuration reference](docs/TECHNICAL.md)
- [Benchmarks and latency methodology](docs/BENCHMARKS.md)
- [Monitoring and OOM protection](docs/MONITORING.md)
- [Benchmark scenarios](benchmarks/SCENARIOS.md)
