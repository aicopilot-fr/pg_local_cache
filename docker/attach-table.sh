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
  --namespace NAME       RESP namespace; defaults to schema.table
  --value-column NAME    Use legacy scalar-value mode instead of whole-row JSON
  --writable             Enable RESP SET and DEL (default: read-only)
  --database NAME        Target database (default: PG_LOCAL_CACHE_DATABASE)
  --replace              Allow replacing a namespace mapped to another table
  --help                 Show this help

Whole-row mode discovers the table PRIMARY KEY in index order and supports
1-16 columns. Scalar-value mode preserves the 1.0 API and requires one PK
column. The SQL API validates the table and worker role, grants least privilege,
and installs the transaction guard plus row and truncate invalidators.
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
if [[ -n "$value_column" ]]; then
    scalar_mode="true"
else
    scalar_mode="false"
fi

result="$("${psql_base[@]}" \
    --set namespace="$namespace" \
    --set relation="$relation" \
    --set value_column="$value_column" \
    --set writable="$writable" \
    --set scalar_mode="$scalar_mode" \
    --set replace="$replace" <<'SQL'
BEGIN;

LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

SELECT COALESCE(
           NULLIF(:'namespace', ''),
           local_cache._default_namespace(:'relation'::pg_catalog.regclass)
       ) AS effective_namespace
\gset

SELECT EXISTS (
    SELECT 1
      FROM local_cache.mapping AS m
     WHERE (
               m.namespace = :'effective_namespace'
           AND m.relation <> :'relation'::pg_catalog.regclass
           )
        OR (
               m.relation = :'relation'::pg_catalog.regclass
           AND m.namespace <> :'effective_namespace'
           )
) AS mapping_conflict
\gset

\if :mapping_conflict
\if :replace
SELECT pg_catalog.format(
           'SELECT local_cache.unregister_mapping(%L);', m.namespace
       )
  FROM local_cache.mapping AS m
 WHERE (
           m.namespace = :'effective_namespace'
       AND m.relation <> :'relation'::pg_catalog.regclass
       )
    OR (
           m.relation = :'relation'::pg_catalog.regclass
       AND m.namespace <> :'effective_namespace'
       )
 ORDER BY m.namespace
\gexec
\else
ROLLBACK;
DO $pg_local_cache_attach$
BEGIN
    RAISE EXCEPTION
        'pg_local_cache attach: namespace/table mapping is occupied; pass --replace to remap it';
END;
$pg_local_cache_attach$;
\endif
\endif

\if :scalar_mode
SELECT local_cache.attach_value(
    :'relation'::pg_catalog.regclass,
    :'value_column'::name,
    :'effective_namespace',
    :'writable'::boolean
)::text;
\else
SELECT local_cache.attach_table(
    :'relation'::pg_catalog.regclass,
    :'writable'::boolean,
    :'effective_namespace'
)::text;
\endif

COMMIT;
SQL
)"

printf 'attached %s\n' "$result"
