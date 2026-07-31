#!/usr/bin/env bash
set -Eeuo pipefail

readonly official_entrypoint="/usr/local/bin/docker-entrypoint.sh"
readonly runtime_directory="/run/pg_local_cache"
readonly runtime_config="${runtime_directory}/postgresql.conf"
readonly runtime_token="${runtime_directory}/auth_token"

fail() {
    printf 'pg_local_cache entrypoint: %s\n' "$*" >&2
    exit 1
}

require_integer_between() {
    local name="$1"
    local value="$2"
    local minimum="$3"
    local maximum="$4"

    [[ "$value" =~ ^[0-9]+$ ]] \
        || fail "${name} must be an integer"
    (( value >= minimum && value <= maximum )) \
        || fail "${name} must be between ${minimum} and ${maximum}"
}

if [[ "${1:-}" == -* ]]; then
    set -- postgres "$@"
fi

if [[ "${1:-}" != "postgres" ]]; then
    exec "$official_entrypoint" "$@"
fi

: "${PGDATA:=/var/lib/postgresql/data}"

bootstrap_database="${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"
database="${PG_LOCAL_CACHE_DATABASE:-$bootstrap_database}"
role="${PG_LOCAL_CACHE_ROLE:-local_cache_worker}"
bind_address="${PG_LOCAL_CACHE_BIND_ADDRESS:-0.0.0.0}"
port="${PG_LOCAL_CACHE_PORT:-6380}"
workers="${PG_LOCAL_CACHE_WORKERS:-8}"
cache_entries="${PG_LOCAL_CACHE_CACHE_ENTRIES:-65536}"
max_worker_processes="${PG_LOCAL_CACHE_MAX_WORKER_PROCESSES:-16}"
idle_timeout_ms="${PG_LOCAL_CACHE_IDLE_TIMEOUT_MS:-300000}"
statement_timeout_ms="${PG_LOCAL_CACHE_STATEMENT_TIMEOUT_MS:-2000}"
lock_timeout_ms="${PG_LOCAL_CACHE_LOCK_TIMEOUT_MS:-250}"
max_pipeline_commands="${PG_LOCAL_CACHE_MAX_PIPELINE_COMMANDS:-256}"
max_dirty_keys="${PG_LOCAL_CACHE_MAX_DIRTY_KEYS:-4096}"
token_file="${PG_LOCAL_CACHE_AUTH_TOKEN_FILE:-/run/secrets/pg_local_cache_auth_token}"

[[ "$token_file" != "$runtime_token" ]] \
    || fail "PG_LOCAL_CACHE_AUTH_TOKEN_FILE must point to an input secret"

[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "PG_LOCAL_CACHE_DATABASE must be an unquoted PostgreSQL identifier"
[[ "$role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "PG_LOCAL_CACHE_ROLE must be an unquoted PostgreSQL identifier"
[[ ! "$role" =~ ^[Pp][Gg]_ ]] \
    || fail "PG_LOCAL_CACHE_ROLE must not use PostgreSQL-reserved prefix pg_"
[[ "$database" == "$bootstrap_database" ]] \
    || fail "PG_LOCAL_CACHE_DATABASE must match POSTGRES_DB for first-run initialization"
[[ "$bind_address" == "127.0.0.1" || "$bind_address" == "0.0.0.0" ]] \
    || fail "PG_LOCAL_CACHE_BIND_ADDRESS must be 127.0.0.1 or 0.0.0.0"

require_integer_between "PG_LOCAL_CACHE_PORT" "$port" 1 65535
require_integer_between "PG_LOCAL_CACHE_WORKERS" "$workers" 1 32
require_integer_between "PG_LOCAL_CACHE_CACHE_ENTRIES" "$cache_entries" 128 65536
require_integer_between \
    "PG_LOCAL_CACHE_MAX_WORKER_PROCESSES" "$max_worker_processes" 4 128
require_integer_between \
    "PG_LOCAL_CACHE_IDLE_TIMEOUT_MS" "$idle_timeout_ms" 1000 86400000
require_integer_between \
    "PG_LOCAL_CACHE_STATEMENT_TIMEOUT_MS" "$statement_timeout_ms" 100 60000
require_integer_between \
    "PG_LOCAL_CACHE_LOCK_TIMEOUT_MS" "$lock_timeout_ms" 10 60000
require_integer_between \
    "PG_LOCAL_CACHE_MAX_PIPELINE_COMMANDS" "$max_pipeline_commands" 1 4096
require_integer_between \
    "PG_LOCAL_CACHE_MAX_DIRTY_KEYS" "$max_dirty_keys" 128 1048576
(( max_worker_processes >= workers + 2 )) \
    || fail "PG_LOCAL_CACHE_MAX_WORKER_PROCESSES must be at least workers + 2"

[[ -f "$token_file" ]] \
    || fail "auth token file does not exist: ${token_file}"
[[ ! -L "$token_file" ]] \
    || fail "auth token file must not be a symbolic link"

token_mode="$(stat -c '%a' "$token_file")"
[[ "$token_mode" == "600" ]] \
    || fail "auth token file must have mode 0600 (actual: ${token_mode})"

auth_token="$(<"$token_file")"
[[ "$auth_token" =~ ^[A-Za-z0-9_-]{32,256}$ ]] \
    || fail "auth token must be 32-256 base64url characters"

if [[ "$bind_address" != "127.0.0.1" && ${#auth_token} -lt 32 ]]; then
    fail "a non-loopback listener requires an auth token of at least 32 characters"
fi

[[ "$PGDATA" != *"'"* && "$PGDATA" != *$'\n'* ]] \
    || fail "PGDATA contains a character that cannot be safely placed in postgresql.conf"

install -d -o postgres -g postgres -m 0700 "$runtime_directory"
umask 077
install -o postgres -g postgres -m 0600 "$token_file" "$runtime_token"

export PG_LOCAL_CACHE_DATABASE="$database"
export PG_LOCAL_CACHE_ROLE="$role"

temporary_config="${runtime_config}.tmp"
{
    printf "include = '%s/postgresql.conf'\n" "$PGDATA"
    printf "shared_preload_libraries = 'pg_local_cache'\n"
    printf "pg_local_cache.database = '%s'\n" "$database"
    printf "pg_local_cache.role = '%s'\n" "$role"
    printf "pg_local_cache.bind_address = '%s'\n" "$bind_address"
    printf "pg_local_cache.port = %s\n" "$port"
    printf "pg_local_cache.workers = %s\n" "$workers"
    printf "pg_local_cache.cache_entries = %s\n" "$cache_entries"
    printf "pg_local_cache.idle_timeout_ms = %s\n" "$idle_timeout_ms"
    printf "pg_local_cache.statement_timeout_ms = %s\n" "$statement_timeout_ms"
    printf "pg_local_cache.lock_timeout_ms = %s\n" "$lock_timeout_ms"
    printf "pg_local_cache.max_pipeline_commands = %s\n" "$max_pipeline_commands"
    printf "pg_local_cache.max_dirty_keys = %s\n" "$max_dirty_keys"
    printf "pg_local_cache.auth_token_file = '%s'\n" "$runtime_token"
    printf "pg_local_cache.allow_superuser = off\n"
    printf "max_worker_processes = %s\n" "$max_worker_processes"
} > "$temporary_config"

chown postgres:postgres "$temporary_config"
chmod 0600 "$temporary_config"
mv -f "$temporary_config" "$runtime_config"

exec "$official_entrypoint" "$@" -c "config_file=${runtime_config}"
