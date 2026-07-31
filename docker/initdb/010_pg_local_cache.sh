#!/usr/bin/env bash
set -Eeuo pipefail

database="${PG_LOCAL_CACHE_DATABASE:?PG_LOCAL_CACHE_DATABASE is required}"
role="${PG_LOCAL_CACHE_ROLE:?PG_LOCAL_CACHE_ROLE is required}"
postgres_user="${POSTGRES_USER:-postgres}"

[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ ! "$role" =~ ^[Pp][Gg]_ ]] || {
    printf 'pg_local_cache init: worker role must not use reserved prefix pg_\n' >&2
    exit 1
}

role_is_superuser="$(
    psql \
        --no-psqlrc \
        --quiet \
        --tuples-only \
        --no-align \
        --set ON_ERROR_STOP=1 \
        --set worker_role="$role" \
        --username "$postgres_user" \
        --dbname "$database" <<'SQL'
SELECT rolsuper
  FROM pg_catalog.pg_roles
 WHERE rolname = :'worker_role';
SQL
)"
[[ "$role_is_superuser" != "t" ]] || {
    printf 'pg_local_cache init: worker role must not be a superuser\n' >&2
    exit 1
}

psql \
    --no-psqlrc \
    --set ON_ERROR_STOP=1 \
    --set worker_role="$role" \
    --set cache_database="$database" \
    --username "$postgres_user" \
    --dbname "$database" <<'SQL'
SELECT pg_catalog.format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION',
    :'worker_role'
)
WHERE NOT EXISTS (
    SELECT 1
      FROM pg_catalog.pg_roles
     WHERE rolname = :'worker_role'
)
\gexec

ALTER ROLE :"worker_role"
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
GRANT CONNECT ON DATABASE :"cache_database" TO :"worker_role";

CREATE EXTENSION IF NOT EXISTS pg_local_cache;

GRANT USAGE ON SCHEMA local_cache TO :"worker_role";
GRANT SELECT ON TABLE local_cache.mapping TO :"worker_role";
SQL
