#!/usr/bin/env bash
set -Eeuo pipefail

repository_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
    pwd
)"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache_smoke.XXXXXX")"
project="pg_local_cache_smoke_$$"
override_file="${temporary_directory}/compose.override.yaml"
psql_wrapper="${temporary_directory}/psql"
postgres_secret="${temporary_directory}/postgres_password"
cache_secret="${temporary_directory}/pg_local_cache_auth_token"
database="${PG_LOCAL_CACHE_SMOKE_DATABASE:-app}"
worker_role="${PG_LOCAL_CACHE_SMOKE_ROLE:-local_cache_worker}"
cache_entries="${PG_LOCAL_CACHE_SMOKE_CACHE_ENTRIES:-65536}"
max_prepared_transactions="${PG_LOCAL_CACHE_SMOKE_MAX_PREPARED_TRANSACTIONS:-0}"
require_small_cache="${PG_LOCAL_CACHE_SMOKE_REQUIRE_SMALL_CACHE:-0}"
require_2pc="${PG_LOCAL_CACHE_SMOKE_REQUIRE_2PC:-0}"

[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$worker_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$cache_entries" =~ ^[0-9]+$ ]]
[[ "$max_prepared_transactions" =~ ^[0-9]+$ ]]
[[ "$require_small_cache" == "0" || "$require_small_cache" == "1" ]]
[[ "$require_2pc" == "0" || "$require_2pc" == "1" ]]

choose_port() {
    python3 -c \
        'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

postgres_host_port="${POSTGRES_HOST_PORT:-$(choose_port)}"
cache_host_port="${PG_LOCAL_CACHE_HOST_PORT:-$(choose_port)}"
while [[ "$cache_host_port" == "$postgres_host_port" ]]; do
    cache_host_port="$(choose_port)"
done
postgres_password="SmokePostgresPassword_0123456789"
auth_token="SmokeAuthToken_0123456789abcdef0123456789abcdef"

compose() {
    docker compose \
        --project-name "$project" \
        --file "${repository_directory}/compose.yaml" \
        --file "$override_file" \
        "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ "$temporary_directory" == "${TMPDIR:-/tmp}"/pg_local_cache_smoke.* ]]; then
        rm -rf -- "$temporary_directory"
    fi
}
trap cleanup EXIT

install -m 0600 /dev/null "$postgres_secret"
install -m 0600 /dev/null "$cache_secret"
printf '%s\n' "$postgres_password" >"$postgres_secret"
printf '%s\n' "$auth_token" >"$cache_secret"

cat >"$override_file" <<YAML
services:
  postgres:
    environment:
      POSTGRES_DB: ${database}
      PG_LOCAL_CACHE_DATABASE: ${database}
      PG_LOCAL_CACHE_ROLE: ${worker_role}
      PG_LOCAL_CACHE_CACHE_ENTRIES: "${cache_entries}"
    command:
      - postgres
      - -c
      - max_prepared_transactions=${max_prepared_transactions}
secrets:
  postgres_password:
    file: ${postgres_secret}
  pg_local_cache_auth_token:
    file: ${cache_secret}
YAML

cat >"$psql_wrapper" <<SCRIPT
#!/usr/bin/env bash
exec docker compose \
    --project-name "$project" \
    --file "${repository_directory}/compose.yaml" \
    --file "$override_file" \
    exec -T postgres psql --username postgres "\$@"
SCRIPT
chmod 0700 "$psql_wrapper"

export POSTGRES_HOST_PORT="$postgres_host_port"
export PG_LOCAL_CACHE_HOST_PORT="$cache_host_port"

compose up --detach --build --wait

extension_version="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'pg_local_cache'"
)"
[[ "$extension_version" == "1.0.0" ]]

worker_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker'"
)"
[[ "$worker_count" == "8" ]]

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_AUTH_USERNAME="$worker_role" \
PG_LOCAL_CACHE_TEST_ROLE="$worker_role" \
PG_LOCAL_CACHE_REQUIRE_SMALL_CACHE="$require_small_cache" \
PG_LOCAL_CACHE_REQUIRE_2PC="$require_2pc" \
    python3 -B "${repository_directory}/tests/integration.py"

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_TEST_ROLE="$worker_role" \
    python3 -B "${repository_directory}/tests/pipeline_integration.py"

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_BENCH_ROLE="$worker_role" \
PG_LOCAL_CACHE_BENCH_DURATION="${PG_LOCAL_CACHE_SMOKE_DURATION:-1}" \
PG_LOCAL_CACHE_BENCH_CONCURRENCY="${PG_LOCAL_CACHE_SMOKE_CONCURRENCY:-4}" \
PG_LOCAL_CACHE_BENCH_PIPELINE="${PG_LOCAL_CACHE_SMOKE_PIPELINE:-8}" \
PG_LOCAL_CACHE_BENCH_KEYS="${PG_LOCAL_CACHE_SMOKE_KEYS:-128}" \
PG_LOCAL_CACHE_MIN_OPS="${PG_LOCAL_CACHE_SMOKE_MIN_OPS:-0}" \
    python3 -B "${repository_directory}/tests/load.py"

printf 'docker smoke test passed (PostgreSQL %s, RESP %s)\n' \
    "$postgres_host_port" "$cache_host_port"
