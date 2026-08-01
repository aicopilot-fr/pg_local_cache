#!/usr/bin/env bash
set -Eeuo pipefail

readonly postgres_password_file="/run/secrets/postgres_password"
readonly monitor_password_file="/run/secrets/monitor_password"

fail() {
    printf 'pg_local_cache monitoring init: %s\n' "$*" >&2
    exit 1
}

[[ -r "$postgres_password_file" ]] \
    || fail "PostgreSQL password secret is not readable"
[[ -r "$monitor_password_file" ]] \
    || fail "monitor password secret is not readable"

monitor_password="$(<"$monitor_password_file")"
[[ -n "$monitor_password" ]] || fail "monitor password must not be empty"
(( ${#monitor_password} <= 1024 )) \
    || fail "monitor password must not exceed 1024 bytes"

export PGPASSWORD
PGPASSWORD="$(<"$postgres_password_file")"
[[ -n "$PGPASSWORD" ]] || fail "PostgreSQL password must not be empty"

until pg_isready --quiet; do
    sleep 1
done

psql \
    --no-psqlrc \
    --set ON_ERROR_STOP=1 \
    --set monitor_database="$PGDATABASE" \
    --set monitor_password="$monitor_password" <<'SQL'
SELECT 'CREATE ROLE local_cache_monitor LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2'
WHERE NOT EXISTS (
    SELECT 1
      FROM pg_catalog.pg_roles
     WHERE rolname = 'local_cache_monitor'
)
\gexec

ALTER ROLE local_cache_monitor
    LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    NOBYPASSRLS CONNECTION LIMIT 2;
ALTER ROLE local_cache_monitor PASSWORD :'monitor_password';
ALTER ROLE local_cache_monitor SET default_transaction_read_only = on;
ALTER ROLE local_cache_monitor SET statement_timeout = '5s';
ALTER ROLE local_cache_monitor SET lock_timeout = '1s';
ALTER ROLE local_cache_monitor SET idle_in_transaction_session_timeout = '5s';
ALTER ROLE local_cache_monitor SET idle_session_timeout = '60s';

GRANT pg_monitor TO local_cache_monitor;
GRANT CONNECT ON DATABASE :"monitor_database" TO local_cache_monitor;
GRANT USAGE ON SCHEMA local_cache TO local_cache_monitor;
GRANT EXECUTE ON FUNCTION local_cache.metrics() TO local_cache_monitor;
GRANT EXECUTE ON FUNCTION local_cache.health() TO local_cache_monitor;
GRANT EXECUTE ON FUNCTION local_cache.stats() TO local_cache_monitor;

SELECT metrics.up = 1 AS metrics_contract_ready,
       pg_catalog.jsonb_typeof(local_cache.health()) = 'object'
           AS health_contract_ready,
       pg_catalog.jsonb_typeof(local_cache.stats()) = 'object'
           AS stats_contract_ready
  FROM local_cache.metrics() AS metrics
 LIMIT 1;
SQL

unset PGPASSWORD monitor_password
printf 'pg_local_cache monitoring role is ready\n'
