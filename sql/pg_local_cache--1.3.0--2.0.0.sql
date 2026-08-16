\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO '2.0.0'" to load this file. \quit

DROP FUNCTION local_cache.get(regclass, text[]);
DROP FUNCTION local_cache.get(regclass, anyelement);
