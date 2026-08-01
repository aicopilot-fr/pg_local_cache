#!/usr/bin/env bash
set -Eeuo pipefail

repository_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
    pwd
)"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache_sql_only.XXXXXX")"
project="pg_local_cache_sql_only_$$"
override_file="${temporary_directory}/compose.override.yaml"
psql_wrapper="${temporary_directory}/psql"
postgres_secret="${temporary_directory}/postgres_password"
postgres_host_port="${POSTGRES_HOST_PORT:-$(
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
)}"
database="pg_local_cache_sql_only"
app_role="sql_only_app"
app_password="SqlOnlyAppPassword_0123456789"

compose() {
    docker compose \
        --project-name "$project" \
        --file "${repository_directory}/compose.sql-only.yaml" \
        --file "$override_file" \
        "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ "$temporary_directory" == "${TMPDIR:-/tmp}"/pg_local_cache_sql_only.* ]]; then
        rm -rf -- "$temporary_directory"
    fi
}
trap cleanup EXIT

install -m 0600 /dev/null "$postgres_secret"
printf '%s\n' 'SqlOnlyPostgresPassword_0123456789' >"$postgres_secret"

cat >"$override_file" <<YAML
services:
  postgres:
    environment:
      POSTGRES_DB: ${database}
      PG_LOCAL_CACHE_DATABASE: ${database}
secrets:
  postgres_password:
    file: ${postgres_secret}
YAML

cat >"$psql_wrapper" <<SCRIPT
#!/usr/bin/env bash
extra_env=()
if [[ -n "\${PGPASSWORD:-}" ]]; then
    extra_env+=(--env PGPASSWORD)
fi
exec docker compose \
    --project-name "$project" \
    --file "${repository_directory}/compose.sql-only.yaml" \
    --file "$override_file" \
    exec -T "\${extra_env[@]}" postgres psql --username postgres "\$@"
SCRIPT
chmod 0700 "$psql_wrapper"

export POSTGRES_HOST_PORT="$postgres_host_port"
compose up --detach --build --wait

sql_only_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT current_setting('pg_local_cache.port'), (SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker')"
)"
[[ "$sql_only_state" == "0|0" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --set app_role="$app_role" \
    --set app_password="$app_password" --set cache_database="$database" <<'SQL'
SELECT pg_catalog.format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_role', :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'app_role'
)
\gexec
GRANT CONNECT ON DATABASE :"cache_database" TO :"app_role";
GRANT USAGE ON SCHEMA public TO :"app_role";
SQL

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_PORT="0" \
PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_APP_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_APP_HOST="127.0.0.1" \
    python3 -B "${repository_directory}/tests/sql_fastpath_integration.py"

sql_only_metrics="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT m.up, m.workers_configured, m.workers_running, m.active_clients, m.max_clients, m.worker_memory_bytes, m.estimated_memory_bytes <= m.memory_budget_bytes, (local_cache.health() ->> 'ready')::boolean FROM local_cache.metrics() AS m"
)"
[[ "$sql_only_metrics" == "1|0|0|0|0|0|t|t" ]]

printf 'ok: SQL-only Docker profile, zero RESP workers, ordinary-role transparent cache\n'
