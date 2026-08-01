# pg_local_cache

**100k+ whole-row reads/s inside PostgreSQL, with ordinary SQL and
transactional invalidation.**

`pg_local_cache` is a PostgreSQL 16 extension for primary-key lookups. It
keeps the whole row in bounded shared memory, serves supported parameterized
`SELECT` statements through the existing libpq/JDBC/Npgsql/psycopg/ORM driver,
and invalidates entries only when the source transaction commits.

There is no separate Redis or Valkey process and no cache-specific SQL client.
An optional KVik-style RESP2 endpoint provides `GET`, `SET` and `DEL` for
trusted internal services.

Status: production candidate for Linux, PostgreSQL 16 and one PostgreSQL
primary. Run the supplied integration and performance gates on the target
hardware before production rollout.

## Performance at a glance

Recorded long-run reference results from the reproducible benchmark harness:

| Read path | Median ops/s |
|---|---:|
| Whole-row RESP `GET`, pg_local_cache | **106,948** |
| Whole-row RESP `GET`, Valkey | 118,387 |
| Whole-row RESP `GET`, Redis | 123,790 |
| Ordinary prepared SQL, cached | **70,275** |
| Ordinary unnamed extended SQL, cached | **18,985** |

The RESP comparison uses byte-identical full-row JSON, the same client,
connections, pipeline and Docker network for all three targets. The SQL lanes
use normal parameterized PostgreSQL queries and independently require at least
10,000 ops/s with one cache hit per successful lookup and no timed misses,
fills or bypasses. On this run, whole-row `pg_local_cache` reached about 90% of
Valkey throughput while PostgreSQL remained the source of truth.

These are regression results from a shared runner, not a capacity promise.
See [benchmark results and methodology](docs/BENCHMARKS.md), including payload,
CPU limits, latency semantics, write results and the one-command reproduction.
Every release workflow keeps machine-readable CI benchmark evidence with its
downloadable artifacts.

## Choose a mode

| Mode | Use it when |
|---|---|
| **SQL-only** | Recommended drop-in path. The application keeps issuing ordinary SQL; no RESP port or token exists. |
| **SQL + RESP** | You also need KVik-style whole-row `GET`/`SET`/`DEL` from a trusted internal network. |

Both modes use the same shared row cache, transaction-aware invalidation,
whole-row payload and monitoring API. `pg_local_cache.port=0` removes all RESP
workers and their client buffers from the SQL-only profile.

## Try SQL-only in five minutes

Requirements: Docker with Compose v2, GitHub CLI access to this private
repository and an available local TCP port.

```bash
gh repo clone aicopilot-fr/pg_local_cache
cd pg_local_cache

mkdir -p secrets
umask 077
openssl rand -base64 36 > secrets/postgres_password
chmod 600 secrets/postgres_password

docker compose -f compose.sql-only.yaml up --detach --build --wait
psql 'postgresql://postgres@127.0.0.1:5432/app'
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

The application continues to use ordinary SQL:

```sql
SELECT *
FROM public.items
WHERE id = $1;
```

The first supported lookup reads the table and fills the cache. A warm lookup
is served by `Custom Scan (pg_local_cache_sql)`. If the entry is absent,
expired, unsafe for the current snapshot or too large, PostgreSQL executes the
normal table/index plan; the application still gets the authoritative result.

Check the path and health:

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT * FROM public.items WHERE id = 1;

SELECT local_cache.health();
SELECT * FROM local_cache.metrics();
```

Supported SQL fast-path shape:

- one permanent application table;
- the full primary key, including composite keys of 1–16 columns;
- equality predicates with constants or parameters;
- `SELECT *` or direct table-column projections, aliases and reordering;
- optional `LIMIT 1`.

Joins, extra filters, expressions, row locks, RLS, recovery,
`REPEATABLE READ` and `SERIALIZABLE` safely use PostgreSQL's normal plan.

## Enable the RESP endpoint

Create a second secret and start the default Compose profile:

```bash
openssl rand -base64 48 \
  | tr '+/' '-_' | tr -d '=[:space:]' \
  > secrets/pg_local_cache_auth_token
chmod 600 secrets/pg_local_cache_auth_token

docker compose up --detach --build --wait
docker compose exec postgres pg_local_cache_attach \
  --table public.items \
  --writable
```

The endpoint is published on `127.0.0.1:6380` by default. `GET` returns the
entire row as JSON:

```bash
export PG_LOCAL_CACHE_TOKEN="$(tr -d '\r\n' \
  < secrets/pg_local_cache_auth_token)"

redis-cli -h 127.0.0.1 -p 6380 \
  -a "$PG_LOCAL_CACHE_TOKEN" --raw \
  GET 'CRUD:app.public.items:{"id":1}'
```

`--writable` only enables RESP `SET`/`DEL`. It does not control ordinary SQL
writes by application roles. The shared RESP token grants access to every
registered mapping on this instance; PostgreSQL per-user ACL and RLS are not
applied to the RESP endpoint.

## Install on an existing PostgreSQL server

Use the downloadable `pg_local_cache-*-source.tar.gz`, or the explicitly
labelled PostgreSQL 16 / Debian 12 / amd64 binary archive, then follow
[Installing on an existing database](docs/INSTALL_EXISTING.md).

The included installer is deliberately two-phase:

```bash
sudo ./install.sh preflight --database app --mode sql-only
sudo ./install.sh install   --database app --mode sql-only
```

For the source archive, use `./scripts/install-existing.sh` in place of
`./install.sh`.

It builds or stages files, creates the isolated worker role, preserves the
existing `shared_preload_libraries`, backs up `postgresql.auto.conf`, validates
the resulting configuration and stops before restart by default.

A first installation **requires one PostgreSQL restart** because shared memory
and planner/background-worker hooks are registered at postmaster start.
`pg_reload_conf()` or `LOAD` cannot activate them. All preparation is online;
only the final controlled restart is disruptive. A healthy standalone server
often restarts within 30 seconds, but recovery, active sessions and storage can
make it longer, so 30 seconds is an operational target rather than an SLA.

For Patroni or a Kubernetes operator, install the artifact on every member and
use the platform's rolling restart/switchover workflow. Arbitrary native C
extensions usually cannot be installed on managed PostgreSQL services without
explicit provider support.

## Attach, reconcile and detach

Run administrative functions as the extension owner or a trusted deploy role:

```sql
-- Whole-row, read-only through RESP; ordinary SQL writes remain unchanged.
SELECT local_cache.attach_table('public.items'::regclass);

-- Whole-row with an explicit namespace and RESP writes.
SELECT local_cache.attach_table(
    'public.items'::regclass,
    p_writable => true,
    p_namespace => 'items'
);

SELECT local_cache.reconcile_table('public.items'::regclass);
SELECT local_cache.reconcile_all();
SELECT local_cache.detach_table('public.items'::regclass);
```

Attach installs extension-owned `ENABLE ALWAYS` triggers. SQL
`INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` invalidates only at commit; rollback does
not publish invalidation. Changing a primary key invalidates both the old and
new key. Attach takes a short `ShareRowExclusiveLock`, so production deploys
should use a bounded `lock_timeout` and retry outside peak DDL/DML bursts.

## Roles

| Role | Required access |
|---|---|
| Application role | Normal privileges on the source table only; no access to `local_cache` is needed. |
| Extension owner / deploy role | `CREATE EXTENSION` and explicitly granted attach/detach/reconcile functions. |
| `local_cache_worker` | Isolated `LOGIN`, `NOSUPERUSER`, `NOINHERIT`; table ACLs are maintained by attach/reconcile. |
| Monitoring role | Explicit `EXECUTE` on stats/health/metrics plus only the PostgreSQL monitoring privileges it needs. |

## Releases and verification

After CI succeeds on `main`, GitHub Actions builds:

- a portable source archive;
- a binary archive explicitly scoped to PostgreSQL 16, Debian 12 and amd64;
- `SHA256SUMS`;
- CI benchmark evidence when available.

Each successful main commit also has a 90-day downloadable Actions artifact
and an immutable `main-<sha>` prerelease. The first commit for a new
`default_version` publishes an immutable `vX.Y.Z` GitHub Release; later code
changes must bump the version rather than overwrite an existing tag or asset.

## Documentation

- [Existing database installation, restart and rollback](docs/INSTALL_EXISTING.md)
- [Benchmarks and reproducibility](docs/BENCHMARKS.md)
- [Full technical reference](docs/TECHNICAL.md)
- [Monitoring stack](monitoring/README.md)
- [Extended benchmark scenarios](benchmarks/SCENARIOS.md)

## Current limits

- PostgreSQL 16 on Linux only.
- One configured database and one writable primary per extension instance.
- No TTL, Pub/Sub, Lua, Redis Cluster or standby-serving cache.
- Cache entries are bounded; an encoded row is limited to 8 KiB.
- RESP is a shared-token trust boundary, not PostgreSQL user authentication.
- The project is a production candidate: repeat correctness, restart and load
  gates in the actual HA, storage and connection-pool environment.
