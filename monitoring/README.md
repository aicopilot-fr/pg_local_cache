# pg_local_cache monitoring stack

This optional Compose overlay adds a least-privilege PostgreSQL monitoring role,
`postgres_exporter` 0.19.1, Prometheus 3.12.0, and a provisioned Grafana 13.1
dashboard. The only new host port is Grafana on loopback. Prometheus and the
exporter remain reachable only on the Compose network.

The exporter reads the typed, one-row `local_cache.metrics()` function. It also
collects the standard PostgreSQL metrics so the same Prometheus can be used for
database health. The `local_cache_monitor` role has `CONNECTION LIMIT 2`, read-only
transactions, short timeouts, `pg_monitor`, and execute permission only for the
cache `metrics()`, `health()`, and `stats()` functions. The exporter itself uses
only `metrics()` for its stable, one-row contract.

## Start

Create the two additional secrets next to the base stack secrets. Secret files
may contain punctuation; a trailing newline is ignored.

```sh
install -d -m 0700 secrets
openssl rand -base64 36 | tr -d '\n' > secrets/monitor_password
openssl rand -base64 36 | tr -d '\n' > secrets/grafana_admin_password
chmod 0444 secrets/monitor_password secrets/grafana_admin_password
```

The containing `secrets/` directory remains mode `0700`, so other host users
cannot traverse it. The two files are readable inside the non-root exporter and
Grafana containers; local Compose file-backed secrets preserve host ownership
and ignore per-service `uid`, `gid`, and `mode` remapping.

Then start the base stack and monitoring overlay together:

```sh
docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  up -d --build
```

Open <http://127.0.0.1:3000> and sign in as `admin` (or the value of
`GRAFANA_ADMIN_USER`) using `secrets/grafana_admin_password`. Set
`GRAFANA_HOST_PORT` to change the loopback port. For a reverse proxy, also set
`GRAFANA_ROOT_URL` to its external HTTPS URL; do not expose Grafana directly on
an untrusted network.

`monitoring-init` is an idempotent one-shot service. Recreate it and the
exporter after rotating the monitor password:

```sh
docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  up -d --force-recreate monitoring-init postgres-exporter
```

## Optional container OOM metrics

On a Linux Docker host, enable cAdvisor with the opt-in profile:

```sh
docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  --profile host-metrics \
  up -d
```

cAdvisor needs privileged, read-only access to host cgroups and Docker state.
Keep this profile disabled where that host-level access is not acceptable. Its
Prometheus target is expected to be down while the profile is disabled; no
availability alert is attached to that optional target. When enabled, the
dashboard displays container memory utilization and alerts on memory pressure
and cgroup OOM events.

## Alerts

The bundled rules cover:

- exporter, extension, and worker availability;
- cache, client, and deterministic memory budget saturation;
- global client-limit rejections and slow clients;
- transaction dirty-key fallback and mapping reload failures;
- PostgreSQL container memory pressure and OOM events when cAdvisor is enabled.

Tune warning windows to the workload before routing them to a pager. Counters
reset on PostgreSQL restart, so the rules use `increase()` and tolerate resets.

## Validate configuration

Run the same pinned Prometheus release used by the stack:

```sh
docker run --rm \
  -v "$PWD/monitoring/prometheus:/etc/prometheus:ro" \
  --entrypoint promtool \
  prom/prometheus:v3.12.0 \
  check config /etc/prometheus/prometheus.yml

docker run --rm \
  -v "$PWD/monitoring/prometheus:/work:ro" \
  --entrypoint promtool \
  prom/prometheus:v3.12.0 \
  check rules /work/alerts.yml

docker run --rm \
  -v "$PWD/monitoring/prometheus:/work:ro" \
  -w /work \
  --entrypoint promtool \
  prom/prometheus:v3.12.0 \
  test rules alerts.test.yml

docker run --rm \
  -v "$PWD/monitoring/postgres-exporter/queries.yaml:/etc/postgres_exporter/queries.yaml:ro" \
  quay.io/prometheuscommunity/postgres-exporter:v0.19.1 \
  --extend.query-path=/etc/postgres_exporter/queries.yaml \
  --dumpmaps >/dev/null

python3 -m json.tool \
  monitoring/grafana/dashboards/pg-local-cache.json >/dev/null

docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  config --quiet
```

For an existing PostgreSQL data volume created by an older image, ensure that
the installed extension exposes `local_cache.metrics()` before starting the
overlay. The one-shot initializer deliberately fails instead of granting broad
schema access when the metrics contract is absent.
