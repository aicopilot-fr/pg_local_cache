---
layout: doc
title: PostgreSQL cache monitoring and OOM protection
description: Export pg_local_cache metrics to Prometheus, use the Grafana dashboard, and monitor extension and container memory limits.
section: Monitoring
permalink: /docs/MONITORING.html
---

# Monitoring pg_local_cache

`local_cache.metrics()` exposes typed counters and gauges for cache activity,
memory, RESP workers and clients, invalidation, backpressure, and mapping
reloads. The optional Compose overlay collects them with postgres_exporter and
provides Prometheus rules and a Grafana dashboard.

## Start the monitoring overlay

Configure the base Docker stack first, then create the monitoring and Grafana
secrets. A trailing newline is ignored.

```bash
install -d -m 0700 secrets
openssl rand -base64 36 | tr -d '\n' > secrets/monitor_password
openssl rand -base64 36 | tr -d '\n' > secrets/grafana_admin_password
chmod 0444 secrets/monitor_password secrets/grafana_admin_password

docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  up --detach --build
```

Open <http://127.0.0.1:3000> and sign in as `admin` with the value in
`secrets/grafana_admin_password`. Grafana is published on loopback;
Prometheus and postgres_exporter remain on the Compose network. Set
`GRAFANA_HOST_PORT` to use another loopback port. Remote access should use an
authenticated HTTPS reverse proxy.

The `monitoring-init` service creates a least-privilege PostgreSQL role with a
connection limit, read-only transactions, short timeouts, `pg_monitor`, and
execution rights only for the cache monitoring functions.

## SQL health and metrics

These queries are available without the optional stack when execution is
granted to the monitoring role:

```sql
SELECT * FROM local_cache.metrics();
SELECT local_cache.health();
SELECT local_cache.stats();
```

Use `metrics()` for collection, `health()` for readiness, and `stats()` for a
detailed JSON diagnostic snapshot. Counters reset at PostgreSQL restart, so
rate queries and alerts must tolerate resets.

Monitor at least:

- cache hit, miss, fill, bypass, eviction, and invalidation rates;
- estimated extension memory versus its startup budget;
- active and peak RESP clients, rejected connections, backpressure, and slow
  client drops;
- dirty-key relation fallbacks;
- mapping reload failures and workers with incomplete mappings;
- worker restarts and readiness.

## Memory and OOM boundary

`pg_local_cache.memory_budget_mb` bounds deterministic extension allocations:
shared hashes, RESP client buffers, and worker state. PostgreSQL refuses startup
when that layout exceeds the configured budget. The same model is exported as
`estimated_memory_bytes` and `memory_budget_bytes`.

This is not a whole-process OOM limit. It excludes `shared_buffers`, backend
processes, `work_mem`, exporters, the operating system, and other containers.
Set a container or cgroup memory limit with headroom and monitor both the
extension estimate and process or container memory.

On a Linux Docker host, cAdvisor can add container pressure and OOM-event
metrics:

```bash
docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  --profile host-metrics \
  up --detach
```

cAdvisor requires privileged, read-only access to host cgroups and Docker
state. Keep the profile disabled when that access is not acceptable.

## Alerts

The bundled Prometheus rules cover:

- exporter, extension, and RESP worker availability;
- cache, client, and deterministic memory-budget saturation;
- global client-limit rejections and slow clients;
- dirty-key fallback and mapping reload failures;
- workers that have not accepted the current mapping generation;
- container memory pressure and OOM events when cAdvisor is enabled.

Tune thresholds and warning windows against the deployment before paging on
them. The supplied rules are operational defaults, not service objectives.

## Rotate and validate

After rotating the monitoring password, recreate the initializer and exporter:

```bash
docker compose \
  -f compose.yaml \
  -f compose.monitoring.yaml \
  up --detach --force-recreate monitoring-init postgres-exporter
```

The repository includes pinned validation commands for Prometheus rules,
postgres_exporter mappings, the Grafana dashboard, and the combined Compose
configuration in the
[monitoring source guide](https://github.com/aicopilot-fr/pg_local_cache/blob/main/monitoring/README.md).
For memory accounting and metric semantics, see the
[technical reference]({{ '/docs/TECHNICAL.html' | relative_url }}).
