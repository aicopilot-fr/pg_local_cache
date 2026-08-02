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
  --namespace NAME       Internal mapping name; defaults to schema.table
  --writable             Enable RESP SET and DEL (default: read-only)
  --database NAME        Target database (default: PG_LOCAL_CACHE_DATABASE)
  --replace              Allow replacing a namespace mapped to another table
  --help                 Show this help

The command discovers the table PRIMARY KEY in index order and supports 1-16
columns. The SQL API validates the table and worker role, grants least privilege,
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
result="$("${psql_base[@]}" \
    --set namespace="$namespace" \
    --set relation="$relation" \
    --set writable="$writable" \
    --set replace="$replace" <<'SQL'
/* The post-lock mapping recheck needs a fresh statement snapshot even when a
 * deployment changes default_transaction_isolation at role/database scope. */
BEGIN ISOLATION LEVEL READ COMMITTED;

/* Resolve the requested name exactly once.  All later operations use its OID
 * so a concurrent rename cannot redirect this command to another table. */
CREATE TEMP TABLE pg_local_cache_attach_target (
    relation_oid oid PRIMARY KEY,
    effective_namespace text NOT NULL,
    namespace_is_default boolean NOT NULL
) ON COMMIT DROP;

INSERT INTO pg_temp.pg_local_cache_attach_target
SELECT target.relation_oid,
       COALESCE(
           NULLIF(:'namespace', ''),
           local_cache._default_namespace(target.relation_oid::pg_catalog.regclass)
       ),
       NULLIF(:'namespace', '') IS NULL
  FROM (
      SELECT :'relation'::pg_catalog.regclass::oid AS relation_oid
  ) AS target;

/* Snapshot every mapping that a replacement would remove before taking any
 * heavyweight relation locks.  Two opposing A<->B replacements therefore
 * discover the same OID set and lock it in the same global order. */
CREATE TEMP TABLE pg_local_cache_attach_conflicts (
    namespace text NOT NULL,
    relation_oid oid NOT NULL,
    PRIMARY KEY (namespace, relation_oid)
) ON COMMIT DROP;

INSERT INTO pg_temp.pg_local_cache_attach_conflicts
SELECT m.namespace, m.relation::oid
  FROM local_cache.mapping AS m
  CROSS JOIN pg_temp.pg_local_cache_attach_target AS target
 WHERE (
           m.namespace = target.effective_namespace
       AND m.relation::oid <> target.relation_oid
       )
    OR (
           m.relation::oid = target.relation_oid
       AND m.namespace <> target.effective_namespace
       );

DO $pg_local_cache_attach$
DECLARE
    v_relation_oid oid;
BEGIN
    FOR v_relation_oid IN
        SELECT candidate.relation_oid
          FROM (
              SELECT target.relation_oid
                FROM pg_temp.pg_local_cache_attach_target AS target
              UNION
              SELECT conflict.relation_oid
                FROM pg_temp.pg_local_cache_attach_conflicts AS conflict
          ) AS candidate
         ORDER BY candidate.relation_oid
    LOOP
        IF NOT local_cache._lock_relation(v_relation_oid) THEN
            RAISE EXCEPTION
                'pg_local_cache attach: relation OID % no longer exists; retry the command',
                v_relation_oid
                USING ERRCODE = '40001';
        END IF;
    END LOOP;
END;
$pg_local_cache_attach$;

LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

/* A mapping may have changed while the sorted relation locks were acquired.
 * Never chase a newly discovered relation while holding the mapping lock:
 * fail and retry from a fresh, globally ordered snapshot instead. */
WITH current_conflicts AS (
    SELECT m.namespace, m.relation::oid AS relation_oid
      FROM local_cache.mapping AS m
      CROSS JOIN pg_temp.pg_local_cache_attach_target AS target
     WHERE (
               m.namespace = target.effective_namespace
           AND m.relation::oid <> target.relation_oid
           )
        OR (
               m.relation::oid = target.relation_oid
           AND m.namespace <> target.effective_namespace
           )
), changed_conflicts AS (
    (
        SELECT current_conflicts.namespace, current_conflicts.relation_oid
          FROM current_conflicts
        EXCEPT
        SELECT snapshot.namespace, snapshot.relation_oid
          FROM pg_temp.pg_local_cache_attach_conflicts AS snapshot
    )
    UNION ALL
    (
        SELECT snapshot.namespace, snapshot.relation_oid
          FROM pg_temp.pg_local_cache_attach_conflicts AS snapshot
        EXCEPT
        SELECT current_conflicts.namespace, current_conflicts.relation_oid
          FROM current_conflicts
    )
)
SELECT COALESCE(
           (
               NOT target.namespace_is_default
               OR target.effective_namespace = local_cache._default_namespace(
                   target.relation_oid::pg_catalog.regclass
               )
           )
           AND NOT EXISTS (SELECT 1 FROM changed_conflicts),
           false
       ) AS mapping_snapshot_stable,
       EXISTS (
           SELECT 1 FROM pg_temp.pg_local_cache_attach_conflicts
       ) AS mapping_conflict
  FROM pg_temp.pg_local_cache_attach_target AS target
\gset

\if :mapping_snapshot_stable
\else
ROLLBACK;
DO $pg_local_cache_attach$
BEGIN
    RAISE EXCEPTION
        'pg_local_cache attach: table name or mapping changed concurrently; retry the command'
        USING ERRCODE = '40001';
END;
$pg_local_cache_attach$;
\endif

\if :mapping_conflict
\if :replace
SELECT pg_catalog.format(
           'SELECT local_cache.detach_table(%s::oid::pg_catalog.regclass);',
           m.relation_oid
       )
  FROM pg_temp.pg_local_cache_attach_conflicts AS m
 ORDER BY m.relation_oid, m.namespace
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

SELECT target.relation_oid::text AS target_relation_oid,
       target.effective_namespace
  FROM pg_temp.pg_local_cache_attach_target AS target
\gset

SELECT local_cache.attach_table(
    :'target_relation_oid'::oid::pg_catalog.regclass,
    :'writable'::boolean,
    :'effective_namespace'
)::text;

COMMIT;
SQL
)"

printf 'attached %s\n' "$result"
