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
mismatch_database="postgres"
[[ "$database" != "$mismatch_database" ]] || mismatch_database="template1"
worker_role="${PG_LOCAL_CACHE_SMOKE_ROLE:-local_cache_worker}"
app_role="${PG_LOCAL_CACHE_SMOKE_APP_ROLE:-app_user}"
app_password="${PG_LOCAL_CACHE_SMOKE_APP_PASSWORD:-SmokeAppPassword_0123456789}"
cache_entries="${PG_LOCAL_CACHE_SMOKE_CACHE_ENTRIES:-65536}"
max_prepared_transactions="${PG_LOCAL_CACHE_SMOKE_MAX_PREPARED_TRANSACTIONS:-0}"
require_small_cache="${PG_LOCAL_CACHE_SMOKE_REQUIRE_SMALL_CACHE:-0}"
require_2pc="${PG_LOCAL_CACHE_SMOKE_REQUIRE_2PC:-0}"

[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$worker_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$app_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ ${#app_password} -ge 16 ]]
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
extra_env=()
if [[ -n "\${PGPASSWORD:-}" ]]; then
    extra_env+=(--env PGPASSWORD)
fi
exec docker compose \
    --project-name "$project" \
    --file "${repository_directory}/compose.yaml" \
    --file "$override_file" \
    exec -T "\${extra_env[@]}" postgres psql --username postgres "\$@"
SCRIPT
chmod 0700 "$psql_wrapper"

export POSTGRES_HOST_PORT="$postgres_host_port"
export PG_LOCAL_CACHE_HOST_PORT="$cache_host_port"

compose up --detach --build --wait

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

app_identity="$(
    PGPASSWORD="$app_password" "$psql_wrapper" \
        --host 127.0.0.1 --username "$app_role" --dbname "$database" \
        --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT current_user, session_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user"
)"
[[ "$app_identity" == "${app_role}|${app_role}|f|f|f|f|f" ]]

app_acl="$(
    PGPASSWORD="$app_password" "$psql_wrapper" \
        --host 127.0.0.1 --username "$app_role" --dbname "$database" \
        --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.has_schema_privilege(current_user, 'local_cache', 'USAGE'), COALESCE((SELECT pg_catalog.has_table_privilege(current_user, c.oid, 'SELECT') FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'local_cache' AND c.relname = 'mapping'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'register_mapping'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'unregister_mapping'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'invalidate'), false)"
)"
[[ "$app_acl" == "f|f|f|f|f" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.pglc_attach_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO public.pglc_attach_smoke VALUES (1, 'attached');
CREATE TABLE public.pglc_attach_composite_smoke (
    tenant_id bigint NOT NULL,
    id bigint NOT NULL,
    value text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE public.pglc_attach_other_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
SQL

database_mismatch_error="${temporary_directory}/attach-database.error"
if compose exec -T postgres pg_local_cache_attach \
    --database "$mismatch_database" \
    --table public.pglc_attach_smoke >"$database_mismatch_error" 2>&1; then
    printf 'non-configured database was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'is not served by pg_local_cache workers' "$database_mismatch_error"

unprivileged_attach_error="${temporary_directory}/attach-unprivileged.error"
if compose exec -T \
    --env "POSTGRES_USER=${app_role}" \
    --env "PGPASSWORD=${app_password}" \
    postgres pg_local_cache_attach \
        --database "$database" \
        --table public.pglc_attach_smoke >"$unprivileged_attach_error" 2>&1; then
    printf 'unprivileged attach unexpectedly succeeded\n' >&2
    exit 1
fi
grep -Fq 'permission denied' "$unprivileged_attach_error"

unprivileged_mapping_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*) FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$unprivileged_mapping_count" == "0" ]]

attach_output="$(
    compose exec -T postgres pg_local_cache_attach \
        --database "$database" \
        --table public.pglc_attach_smoke \
        --writable
)"
[[ "$attach_output" == *"namespace=pglc_attach_smoke"* ]]
[[ "$attach_output" == *"key=id value=value writable=true"* ]]

attach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT m.key_column, m.value_column, m.writable, (SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid = m.relation AND t.tgname IN ('pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate') AND t.tgenabled = 'A'), pg_catalog.has_schema_privilege('$worker_role', 'public', 'USAGE'), pg_catalog.has_table_privilege('$worker_role', m.relation, 'SELECT') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'INSERT') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'UPDATE') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'DELETE') FROM local_cache.mapping m WHERE m.namespace = 'pglc_attach_smoke'"
)"
[[ "$attach_state" == "id|value|t|2|t|t" ]]

namespace_conflict_error="${temporary_directory}/attach-namespace.error"
if compose exec -T postgres pg_local_cache_attach \
    --database "$database" \
    --namespace pglc_attach_smoke \
    --table public.pglc_attach_other_smoke >"$namespace_conflict_error" 2>&1; then
    printf 'occupied namespace was unexpectedly replaced without --replace\n' >&2
    exit 1
fi
grep -Fq 'pass --replace to remap it' "$namespace_conflict_error"

namespace_after_conflict="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT relation = 'public.pglc_attach_smoke'::pg_catalog.regclass FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$namespace_after_conflict" == "t" ]]

replace_output="$(
    compose exec -T postgres pg_local_cache_attach \
        --database "$database" \
        --namespace pglc_attach_smoke \
        --table public.pglc_attach_other_smoke \
        --replace
)"
[[ "$replace_output" == *"table=public.pglc_attach_other_smoke"* ]]

namespace_after_replace="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT relation = 'public.pglc_attach_other_smoke'::pg_catalog.regclass FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$namespace_after_replace" == "t" ]]

composite_error="${temporary_directory}/attach-composite.error"
if compose exec -T postgres pg_local_cache_attach \
    --database "$database" \
    --table public.pglc_attach_composite_smoke \
    --value-column value >"$composite_error" 2>&1; then
    printf 'composite primary key was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'table must have exactly one primary-key column' "$composite_error"

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.unregister_mapping('pglc_attach_smoke'); DROP TABLE public.pglc_attach_smoke, public.pglc_attach_composite_smoke, public.pglc_attach_other_smoke"

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
PG_LOCAL_CACHE_TEST_WRITER_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_WRITER_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_WRITER_HOST="127.0.0.1" \
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
