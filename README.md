# pg_local_cache

`pg_local_cache` is a PostgreSQL 16 extension that turns attached tables into a
transaction-aware SQL key-value store. Applications keep using parameterized
`SELECT` through libpq, JDBC, Npgsql, psycopg, or an ORM. No cache server, token,
cache-specific driver, or proprietary SQL syntax is required.

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
| Native tuple API | Ordinary primary-key `SELECT` returns the table row type and supports normal SQL projection. |
| Batch JSON API | `local_cache.get()` and `local_cache.mget()` provide ordered whole-row JSON for KV-style callers. |
| Whole rows | Each entry stores one versioned PostgreSQL composite row. |
| Transactional invalidation | `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` fence affected entries before commit visibility. |
| Bounded extension memory | Entry capacity, client slots, and deterministic extension allocations are fixed at startup. |
| Optional RESP2 | Trusted internal clients can use authenticated whole-row `GET`, `SET`, and `DEL`. |
| Operations | SQL metrics, health checks, Prometheus rules, and a Grafana dashboard are included. |

## Docker quick start

Requirements: Docker with Compose v2 and OpenSSL.

```bash
git clone https://github.com/profundium/pg_local_cache.git
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

Use ordinary PostgreSQL SQL. Select every column or only the columns needed by
the caller:

```sql
SELECT * FROM public.items WHERE id = $1::bigint;

SELECT value, metadata FROM public.items WHERE id = $1::bigint;
```

Supported exact-primary-key reads can use `Custom Scan (pg_local_cache_sql)`;
the row shape and zero-or-one-row semantics remain ordinary PostgreSQL:

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

The canonical tuple API is an ordinary exact-primary-key query:

```sql
SELECT * FROM public.items WHERE id = 42::bigint;
SELECT metadata FROM public.items WHERE id = 42::bigint;
```

Composite primary keys use normal SQL predicates, in any order:

```sql
SELECT * FROM public.tenant_items
WHERE item_id = 42::bigint AND tenant_id = 'tenant-a';
```

KV-style callers can opt into the JSON scalar and ordered batch functions:

```sql
SELECT local_cache.get('public.items'::regclass, 42::bigint);
SELECT local_cache.mget(
    'public.items'::regclass,
    ARRAY[42, 7, 42]::bigint[]
);
```

The functions are `SECURITY INVOKER`: the caller still needs `SELECT` on the
source table. Ordinary tuple reads need no `local_cache` schema or function
grant. Writes remain ordinary PostgreSQL DML, so a transaction can update a row
and immediately read its own value; commit invalidates the old entry and rollback
never publishes the new one.

The SQL fast path accepts:

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

Application roles need only their normal source-table privileges to benefit
from a transparent cached `SELECT`. Administrative functions are separate:

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

### Current ordinary SQL result (`fe2d23c`)

[CI run 30803546805](https://github.com/profundium/pg_local_cache/actions/runs/30803546805)
for [source `fe2d23c`](https://github.com/profundium/pg_local_cache/commit/fe2d23c87ddc7e523ada2951376ebcb7d8570fb1)
passed every benchmark gate and produced the preserved
[`comparison-smoke` evidence bundle](assets/benchmark-evidence/fe2d23c/comparison-smoke.zip)
(Actions artifact ID `8851825673`; ZIP digest
`sha256:fc624e7ebed11b10c8470d11e7d2a91855813e04f9fb809e62e4f0852f7c8a76`).
The table shows the complete prepared SQL template used on both the mapped and
stock PostgreSQL 16.14 servers. pgbench replaces `:key` with the measured key:

| Command template | Mapped cache ops/s | Stock PostgreSQL ops/s | Mapped/stock |
|---|---:|---:|---:|
| `SELECT * FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 126,710 | 65,257 | 1.94x |
| `SELECT metadata, payload, enabled, amount, note, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE tenant_id = 7 AND id = :key;` | 123,051 | 65,236 | 1.89x |
| `SELECT payload, metadata, id, tenant_id FROM public.pg_local_cache_whole_row_comparison WHERE id = :key AND tenant_id = 7;` | 131,017 | 71,398 | 1.84x |

The AMD EPYC 7763 shared runner exposed four logical CPUs. The client and each
server target had a two-CPU quota. The smoke used four clients, pipeline depth
8, 128 keys, 128-byte text values, a prefilled working set, and one one-second
repetition. Every timed mapped lookup was an exact cache hit with zero misses,
fills, or bypasses. The absolute 10,000 ops/s floor, result integrity, and
counter accounting gate CI; the displayed ratios do not. This is a regression
smoke, not a capacity claim.

The [evidence manifest](assets/benchmark-evidence/fe2d23c/README.md) records the
artifact identity and digest.

See [benchmark methodology](docs/BENCHMARKS.md) and
[scenario definitions](benchmarks/SCENARIOS.md).

## Monitoring

`local_cache.metrics()` exposes typed cache, memory, worker, client,
invalidation, backpressure, and mapping counters. The optional stack adds
postgres_exporter, Prometheus rules, container memory signals, and a provisioned
Grafana dashboard. Start with the [monitoring and OOM guide](docs/MONITORING.md).

## Releases

Download source, platform-labelled binaries, checksums, and CI evidence from
[GitHub Releases](https://github.com/profundium/pg_local_cache/releases). Never
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
