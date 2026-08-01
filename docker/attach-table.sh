#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'pg_local_cache attach: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
Usage:
  pg_local_cache_attach --table SCHEMA.TABLE [options]

Options:
  --namespace NAME       RESP namespace; defaults to the table name
  --value-column NAME    Cached column; inferred only for a two-column table
  --writable             Enable RESP SET and DEL (default: read-only)
  --database NAME        Target database (default: PG_LOCAL_CACHE_DATABASE)
  --replace              Allow replacing a namespace mapped to another table
  --help                 Show this help

The table must have exactly one primary-key column. The command grants the
worker role the required table privileges and calls register_mapping(), which
creates and validates the transaction guard plus row and truncate invalidators.
USAGE
}

require_value() {
    local option="$1"
    local value="${2:-}"

    [[ -n "$value" && "$value" != -* ]] \
        || fail "${option} requires a value"
}

database="${PG_LOCAL_CACHE_DATABASE:-${POSTGRES_DB:-${POSTGRES_USER:-postgres}}}"
postgres_user="${POSTGRES_USER:-postgres}"
namespace=""
relation=""
value_column=""
writable="false"
replace="false"

while (( $# > 0 )); do
    case "$1" in
        --database)
            require_value "$1" "${2:-}"
            database="$2"
            shift 2
            ;;
        --namespace)
            require_value "$1" "${2:-}"
            namespace="$2"
            shift 2
            ;;
        --table)
            require_value "$1" "${2:-}"
            relation="$2"
            shift 2
            ;;
        --value-column)
            require_value "$1" "${2:-}"
            value_column="$2"
            shift 2
            ;;
        --writable)
            writable="true"
            shift
            ;;
        --replace)
            replace="true"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[[ -n "$relation" ]] || fail "--table is required"
[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "database must be an unquoted PostgreSQL identifier"
[[ "$relation" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}\.[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "table must be SCHEMA.TABLE using unquoted identifiers"
if [[ -n "$namespace" ]]; then
    [[ "$namespace" =~ ^[A-Za-z0-9_.-]{1,63}$ ]] \
        || fail "namespace must contain 1-63 ASCII letters, digits, dot, dash, or underscore"
fi
if [[ -n "$value_column" ]]; then
    [[ "$value_column" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
        || fail "value column must be an unquoted PostgreSQL identifier"
fi

psql_base=(
    psql
    --no-psqlrc
    --quiet
    --tuples-only
    --no-align
    --set ON_ERROR_STOP=1
    --username "$postgres_user"
    --dbname "$database"
)

configured="$("${psql_base[@]}" --field-separator '|' <<'SQL'
SELECT pg_catalog.current_setting('pg_local_cache.database'),
       pg_catalog.current_setting('pg_local_cache.role');
SQL
)"
IFS='|' read -r configured_database worker_role <<<"$configured"
[[ "$database" == "$configured_database" ]] \
    || fail "database ${database} is not served by pg_local_cache workers (configured: ${configured_database})"
[[ "$worker_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "configured worker role is not an unquoted PostgreSQL identifier"

metadata="$("${psql_base[@]}" --field-separator '|' --set relation="$relation" <<'SQL'
SELECT n.nspname,
       c.relname,
       COALESCE(i.indnkeyatts, 0),
       COALESCE(a.attname, '')
  FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n
    ON n.oid = c.relnamespace
  LEFT JOIN pg_catalog.pg_index AS i
    ON i.indrelid = c.oid
   AND i.indisprimary
  LEFT JOIN pg_catalog.pg_attribute AS a
    ON a.attrelid = c.oid
   AND a.attnum = i.indkey[0]
 WHERE c.oid = pg_catalog.to_regclass(:'relation')
   AND c.relkind = 'r';
SQL
)"

[[ -n "$metadata" ]] || fail "table does not exist or is not an ordinary table: ${relation}"
IFS='|' read -r source_schema table_name primary_key_columns primary_key_column \
    <<<"$metadata"
[[ "$primary_key_columns" == "1" && -n "$primary_key_column" ]] \
    || fail "table must have exactly one primary-key column: ${relation}"
[[ "$primary_key_column" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "primary-key column must be an unquoted identifier for automatic attach"

if [[ -z "$namespace" ]]; then
    namespace="$table_name"
    [[ "$namespace" =~ ^[A-Za-z0-9_.-]{1,63}$ ]] \
        || fail "table name is not a valid namespace; pass --namespace"
fi

if [[ -z "$value_column" ]]; then
    value_metadata="$("${psql_base[@]}" --field-separator '|' \
        --set relation="$relation" --set primary_key_column="$primary_key_column" <<'SQL'
SELECT count(*), COALESCE(min(a.attname::text), '')
  FROM pg_catalog.pg_attribute AS a
 WHERE a.attrelid = :'relation'::pg_catalog.regclass
   AND a.attnum > 0
   AND NOT a.attisdropped
   AND a.attname <> :'primary_key_column';
SQL
)"
    IFS='|' read -r candidate_count candidate_column <<<"$value_metadata"
    [[ "$candidate_count" == "1" && -n "$candidate_column" ]] \
        || fail "--value-column is required unless the table has exactly one non-PK column"
    value_column="$candidate_column"
fi

[[ "$value_column" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]] \
    || fail "value column must be an unquoted PostgreSQL identifier"

[[ "$value_column" != "$primary_key_column" ]] \
    || fail "value column must differ from the primary-key column"

role_state="$("${psql_base[@]}" --set worker_role="$worker_role" <<'SQL'
SELECT CASE
       WHEN rolcanlogin
        AND NOT rolsuper
        AND NOT rolinherit
        AND NOT rolcreatedb
        AND NOT rolcreaterole
        AND NOT rolreplication
        AND NOT rolbypassrls
        AND pg_catalog.has_database_privilege(
                :'worker_role', pg_catalog.current_database(), 'CONNECT')
        AND pg_catalog.has_schema_privilege(
                :'worker_role', 'local_cache', 'USAGE')
        AND pg_catalog.has_table_privilege(
                :'worker_role', 'local_cache.mapping', 'SELECT')
       THEN 'dedicated'
       ELSE 'unsafe'
       END
  FROM pg_catalog.pg_roles
 WHERE rolname = :'worker_role';
SQL
)"
[[ "$role_state" == "dedicated" ]] \
    || fail "configured worker role is missing or is not a dedicated least-privilege role: ${worker_role}"

"${psql_base[@]}" \
    --set namespace="$namespace" \
    --set relation="$relation" \
    --set source_schema="$source_schema" \
    --set primary_key_column="$primary_key_column" \
    --set value_column="$value_column" \
    --set worker_role="$worker_role" \
    --set writable="$writable" \
    --set replace="$replace" <<'SQL'
BEGIN;

LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

SELECT CASE WHEN EXISTS (
    SELECT 1
      FROM local_cache.mapping AS m
     WHERE m.namespace = :'namespace'
       AND m.relation <> :'relation'::pg_catalog.regclass
) THEN 'true' ELSE 'false' END AS mapping_conflict
\gset

\if :mapping_conflict
\if :replace
\else
\warn pg_local_cache attach: namespace :namespace is mapped to another table; pass --replace to remap it
ROLLBACK;
SELECT 1 / 0;
\endif
\endif

SELECT pg_catalog.format(
    'GRANT USAGE ON SCHEMA %I TO %I',
    :'source_schema', :'worker_role'
)
\gexec

SELECT pg_catalog.format(
    CASE WHEN :'writable'::boolean
         THEN 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO %I'
         ELSE 'GRANT SELECT ON TABLE %s TO %I'
    END,
    :'relation'::pg_catalog.regclass,
    :'worker_role'
)
\gexec

SELECT local_cache.register_mapping(
    :'namespace',
    :'relation'::pg_catalog.regclass,
    :'primary_key_column'::name,
    :'value_column'::name,
    :'writable'::boolean
);

COMMIT;
SQL

printf 'attached namespace=%s table=%s key=%s value=%s writable=%s worker_role=%s\n' \
    "$namespace" "$relation" "$primary_key_column" "$value_column" \
    "$writable" "$worker_role"
