\echo Use "CREATE EXTENSION pg_local_cache" to load this file. \quit

CREATE TABLE mapping (
    namespace text PRIMARY KEY
        CHECK (namespace ~ '^[A-Za-z0-9_.-]{1,63}$'),
    relation regclass NOT NULL UNIQUE,
    key_column name NOT NULL,
    value_column name NOT NULL,
    writable boolean NOT NULL DEFAULT false
);

REVOKE ALL ON TABLE mapping FROM PUBLIC;

CREATE FUNCTION _row_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_row_invalidate'
LANGUAGE C;

CREATE FUNCTION _truncate_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_truncate_invalidate'
LANGUAGE C;

CREATE FUNCTION _statement_guard()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_statement_guard'
LANGUAGE C;

CREATE FUNCTION _reload()
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_reload'
LANGUAGE C;

CREATE FUNCTION _ddl_invalidate()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger_ddl_commands() AS d
          JOIN local_cache.mapping AS m
            ON (
                d.objid = m.relation::oid
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_inherits AS inh
                         WHERE (
                                   inh.inhrelid = d.objid
                               AND inh.inhparent = m.relation
                               )
                            OR (
                                   inh.inhparent = d.objid
                               AND inh.inhrelid = m.relation
                               )
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_index AS i
                         WHERE i.indexrelid = d.objid
                           AND i.indrelid = m.relation
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_trigger'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_trigger AS t
                         WHERE t.oid = d.objid
                           AND t.tgrelid = m.relation
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_constraint'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint AS c
                         WHERE c.oid = d.objid
                           AND (
                               c.conrelid = m.relation
                               OR c.confrelid = m.relation
                           )
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_rewrite'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_rewrite AS r
                         WHERE r.oid = d.objid
                           AND r.ev_class = m.relation
                    )
                )
            )
    ) THEN
        PERFORM local_cache._reload();
    END IF;
END;
$function$;

CREATE FUNCTION _sql_drop_invalidate()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger_dropped_objects() AS d
          JOIN local_cache.mapping AS m
            ON (
                d.objid = m.relation::oid
                OR (
                    d.object_type IN (
                        'table column',
                        'table constraint',
                        'trigger',
                        'rule'
                    )
                    AND d.address_names[1] = (
                        SELECT n.nspname
                          FROM pg_catalog.pg_class AS c
                          JOIN pg_catalog.pg_namespace AS n
                            ON n.oid = c.relnamespace
                         WHERE c.oid = m.relation
                    )
                    AND d.address_names[2] = (
                        SELECT c.relname
                          FROM pg_catalog.pg_class AS c
                         WHERE c.oid = m.relation
                    )
                )
                /*
                 * Once an index has been dropped, its former owning table
                 * cannot be resolved from the catalogs.  Permanent index drops
                 * are rare and are conservatively treated as mapping changes.
                 * Temporary objects are excluded below.
                 */
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND d.object_type = 'index'
                    AND d.original
                )
            )
         WHERE NOT d.is_temporary
    ) THEN
        PERFORM local_cache._reload();
    END IF;
END;
$function$;

CREATE FUNCTION _forget(namespace text, relation oid)
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_forget'
LANGUAGE C STRICT;

CREATE FUNCTION invalidate(namespace text)
RETURNS bigint
AS 'MODULE_PATHNAME', 'pg_local_cache_invalidate'
LANGUAGE C STRICT;

CREATE FUNCTION stats()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_stats'
LANGUAGE C STABLE;

CREATE FUNCTION _metrics_json()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_metrics_json'
LANGUAGE C STABLE PARALLEL RESTRICTED;

CREATE FUNCTION metrics()
RETURNS TABLE (
    up bigint,
    cache_capacity bigint,
    entries bigint,
    relation_states bigint,
    relation_state_capacity bigint,
    global_dirty_writers bigint,
    active_clients bigint,
    peak_active_clients bigint,
    max_clients bigint,
    client_slots bigint,
    workers_configured bigint,
    workers_running bigint,
    shared_memory_bytes bigint,
    worker_memory_bytes bigint,
    estimated_memory_bytes bigint,
    memory_budget_bytes bigint,
    cache_hits_total bigint,
    cache_misses_total bigint,
    negative_hits_total bigint,
    sql_cache_hits_total bigint,
    sql_cache_misses_total bigint,
    sql_cache_fills_total bigint,
    sql_cache_bypasses_total bigint,
    database_reads_total bigint,
    database_writes_total bigint,
    invalidations_total bigint,
    evictions_total bigint,
    singleflight_leaders_total bigint,
    singleflight_waiters_total bigint,
    singleflight_reuses_total bigint,
    singleflight_timeouts_total bigint,
    rejected_connections_total bigint,
    client_limit_rejections_total bigint,
    authentication_failures_total bigint,
    protocol_errors_total bigint,
    output_backpressure_events_total bigint,
    slow_client_drops_total bigint,
    worker_starts_total bigint,
    dirty_key_limit_fallbacks_total bigint,
    mapping_reload_failures_total bigint
)
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH snapshot AS MATERIALIZED (
    SELECT local_cache._metrics_json() AS payload
)
SELECT
    (payload ->> 'up')::bigint,
    (payload ->> 'cache_capacity')::bigint,
    (payload ->> 'entries')::bigint,
    (payload ->> 'relation_states')::bigint,
    (payload ->> 'relation_state_capacity')::bigint,
    (payload ->> 'global_dirty_writers')::bigint,
    (payload ->> 'active_clients')::bigint,
    (payload ->> 'peak_active_clients')::bigint,
    (payload ->> 'max_clients')::bigint,
    (payload ->> 'client_slots')::bigint,
    (payload ->> 'workers_configured')::bigint,
    (payload ->> 'workers_running')::bigint,
    (payload ->> 'shared_memory_bytes')::bigint,
    (payload ->> 'worker_memory_bytes')::bigint,
    (payload ->> 'estimated_memory_bytes')::bigint,
    (payload ->> 'memory_budget_bytes')::bigint,
    (payload ->> 'cache_hits_total')::bigint,
    (payload ->> 'cache_misses_total')::bigint,
    (payload ->> 'negative_hits_total')::bigint,
    (payload ->> 'sql_cache_hits_total')::bigint,
    (payload ->> 'sql_cache_misses_total')::bigint,
    (payload ->> 'sql_cache_fills_total')::bigint,
    (payload ->> 'sql_cache_bypasses_total')::bigint,
    (payload ->> 'database_reads_total')::bigint,
    (payload ->> 'database_writes_total')::bigint,
    (payload ->> 'invalidations_total')::bigint,
    (payload ->> 'evictions_total')::bigint,
    (payload ->> 'singleflight_leaders_total')::bigint,
    (payload ->> 'singleflight_waiters_total')::bigint,
    (payload ->> 'singleflight_reuses_total')::bigint,
    (payload ->> 'singleflight_timeouts_total')::bigint,
    (payload ->> 'rejected_connections_total')::bigint,
    (payload ->> 'client_limit_rejections_total')::bigint,
    (payload ->> 'authentication_failures_total')::bigint,
    (payload ->> 'protocol_errors_total')::bigint,
    (payload ->> 'output_backpressure_events_total')::bigint,
    (payload ->> 'slow_client_drops_total')::bigint,
    (payload ->> 'worker_starts_total')::bigint,
    (payload ->> 'dirty_key_limit_fallbacks_total')::bigint,
    (payload ->> 'mapping_reload_failures_total')::bigint
FROM snapshot;
$function$;

CREATE FUNCTION health()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH snapshot AS MATERIALIZED (
    SELECT local_cache._metrics_json() AS payload
)
SELECT pg_catalog.jsonb_build_object(
    'ready',
        (payload ->> 'estimated_memory_bytes')::bigint <=
            (payload ->> 'memory_budget_bytes')::bigint
        AND (payload ->> 'workers_running')::bigint =
            (payload ->> 'workers_configured')::bigint
        AND (payload ->> 'active_clients')::bigint <=
            (payload ->> 'max_clients')::bigint,
    'resp_enabled', (payload ->> 'workers_configured')::bigint > 0,
    'workers_configured', (payload ->> 'workers_configured')::bigint,
    'workers_running', (payload ->> 'workers_running')::bigint,
    'active_clients', (payload ->> 'active_clients')::bigint,
    'max_clients', (payload ->> 'max_clients')::bigint,
    'estimated_memory_bytes', (payload ->> 'estimated_memory_bytes')::bigint,
    'memory_budget_bytes', (payload ->> 'memory_budget_bytes')::bigint
)
FROM snapshot;
$function$;

REVOKE ALL ON FUNCTION _reload() FROM PUBLIC;
REVOKE ALL ON FUNCTION _forget(text, oid) FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION stats() FROM PUBLIC;
REVOKE ALL ON FUNCTION _metrics_json() FROM PUBLIC;
REVOKE ALL ON FUNCTION metrics() FROM PUBLIC;
REVOKE ALL ON FUNCTION health() FROM PUBLIC;

CREATE FUNCTION register_mapping(
    p_namespace text,
    p_relation regclass,
    p_key_column name,
    p_value_column name,
    p_writable boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key_type oid;
    v_value_type oid;
    v_key_collation oid;
    v_key_not_null boolean;
    v_value_not_null boolean;
    v_old_relation oid;
    v_relkind "char";
    v_relpersistence "char";
    v_relispartition boolean;
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
    v_key_generated "char";
    v_key_identity "char";
    v_value_generated "char";
    v_value_identity "char";
BEGIN
    IF p_namespace !~ '^[A-Za-z0-9_.-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid pg_local_cache namespace: %', p_namespace
            USING HINT = 'Use 1-63 ASCII letters, digits, dot, dash, or underscore.';
    END IF;

    IF p_key_column = p_value_column THEN
        RAISE EXCEPTION 'key and value columns must be different';
    END IF;

    SELECT c.relkind, c.relpersistence, c.relispartition,
           c.relrowsecurity, c.relforcerowsecurity
      INTO v_relkind, v_relpersistence, v_relispartition,
           v_relrowsecurity, v_relforcerowsecurity
      FROM pg_class AS c
     WHERE c.oid = p_relation;

    IF v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION 'pg_local_cache supports only permanent ordinary tables';
    END IF;

    -- relhassubclass can remain true after the last child is dropped, so use
    -- exact pg_inherits rows in both directions.  relispartition also makes
    -- the declarative-partition exclusion explicit.
    IF v_relispartition OR EXISTS (
        SELECT 1
          FROM pg_catalog.pg_inherits AS inh
         WHERE inh.inhparent = p_relation
            OR inh.inhrelid = p_relation
    ) THEN
        RAISE EXCEPTION 'table inheritance is not supported by pg_local_cache'
            USING HINT =
                'Attach a standalone table with no inheritance parent or children.';
    END IF;

    IF v_relrowsecurity OR v_relforcerowsecurity THEN
        RAISE EXCEPTION 'row-level security is not supported by pg_local_cache'
            USING HINT = 'Use a dedicated table or a security-barrier API instead.';
    END IF;

    SELECT a.atttypid, a.attcollation, a.attnotnull,
           a.attgenerated, a.attidentity
      INTO v_key_type, v_key_collation, v_key_not_null,
           v_key_generated, v_key_identity
      FROM pg_attribute AS a
     WHERE a.attrelid = p_relation
       AND a.attname = p_key_column
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'key column % does not exist on %',
            p_key_column, p_relation;
    END IF;

    SELECT a.atttypid, a.attnotnull, a.attgenerated, a.attidentity
      INTO v_value_type, v_value_not_null,
           v_value_generated, v_value_identity
      FROM pg_attribute AS a
     WHERE a.attrelid = p_relation
       AND a.attname = p_value_column
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'value column % does not exist on %',
            p_value_column, p_relation;
    END IF;

    IF NOT v_key_not_null OR NOT v_value_not_null THEN
        RAISE EXCEPTION 'key and value columns must be NOT NULL';
    END IF;

    IF v_value_type NOT IN (
        'int2'::regtype, 'int4'::regtype, 'int8'::regtype,
        'numeric'::regtype, 'bool'::regtype,
        'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype,
        'uuid'::regtype, 'json'::regtype, 'jsonb'::regtype
    ) THEN
        RAISE EXCEPTION 'unsupported value type %', v_value_type::regtype
            USING HINT =
                'Use a built-in scalar value type documented by pg_local_cache.';
    END IF;

    IF p_writable AND (
        v_key_generated <> '' OR v_key_identity <> '' OR
        v_value_generated <> '' OR v_value_identity <> ''
    ) THEN
        RAISE EXCEPTION
            'writable mappings do not support generated or identity key/value columns';
    END IF;

    IF p_writable AND EXISTS (
        SELECT 1
          FROM pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname <> p_key_column
           AND a.attname <> p_value_column
           AND a.attnotnull
           AND NOT a.atthasdef
           AND a.attgenerated = ''
           AND a.attidentity = ''
    ) THEN
        RAISE EXCEPTION
            'writable mapping has another NOT NULL column without a default'
            USING HINT =
                'SET only supplies the configured key and value columns.';
    END IF;

    IF v_key_type NOT IN (
        'int2'::regtype, 'int4'::regtype, 'int8'::regtype,
        'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype,
        'uuid'::regtype
    ) THEN
        RAISE EXCEPTION 'unsupported key type %', v_key_type::regtype
            USING HINT = 'Supported key types: int2, int4, int8, text, varchar, bpchar, uuid.';
    END IF;

    IF v_key_collation <> 0 AND EXISTS (
        SELECT 1
          FROM pg_collation AS coll
         WHERE coll.oid = v_key_collation
           AND NOT coll.collisdeterministic
    ) THEN
        RAISE EXCEPTION
            'nondeterministic key collations are not supported by pg_local_cache'
            USING HINT =
                'Use a deterministic collation so SQL equality and cache-key invalidation agree.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_index AS i
          JOIN pg_class AS ic ON ic.oid = i.indexrelid
          JOIN pg_am AS am
            ON am.oid = ic.relam
           AND am.amname = 'btree'
          JOIN pg_opclass AS opc
            ON opc.oid = i.indclass[0]
           AND opc.opcmethod = am.oid
           AND opc.opcdefault
           AND (
               opc.opcintype = v_key_type
               OR EXISTS (
                   SELECT 1
                     FROM pg_cast AS pc
                    WHERE pc.castsource = v_key_type
                      AND pc.casttarget = opc.opcintype
                      AND pc.castmethod = 'b'
               )
           )
         WHERE i.indrelid = p_relation
           AND i.indisunique
           AND i.indimmediate
           AND i.indisvalid
           AND i.indisready
           AND i.indpred IS NULL
           AND i.indnkeyatts = 1
           AND i.indkey[0] = (
               SELECT a.attnum
                 FROM pg_attribute AS a
                WHERE a.attrelid = p_relation
                  AND a.attname = p_key_column
           )
    ) THEN
        RAISE EXCEPTION 'key column %.% needs a valid single-column UNIQUE index',
            p_relation, p_key_column;
    END IF;

    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

    SELECT relation::oid
      INTO v_old_relation
      FROM local_cache.mapping
     WHERE namespace = p_namespace;

    IF FOUND AND v_old_relation <> p_relation::oid THEN
        PERFORM local_cache._forget(p_namespace, v_old_relation);
        IF EXISTS (
            SELECT 1 FROM pg_class AS c WHERE c.oid = v_old_relation
        ) THEN
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_statement_guard ON %s',
                v_old_relation::regclass
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_row_invalidate ON %s',
                v_old_relation::regclass
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_truncate_invalidate ON %s',
                v_old_relation::regclass
            );
        END IF;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM local_cache.mapping WHERE namespace = p_namespace
    ) AND (SELECT count(*) FROM local_cache.mapping) >= 128 THEN
        RAISE EXCEPTION 'pg_local_cache supports at most 128 mappings';
    END IF;

    INSERT INTO local_cache.mapping(
        namespace, relation, key_column, value_column, writable
    )
    VALUES (
        p_namespace, p_relation, p_key_column, p_value_column, p_writable
    )
    ON CONFLICT (namespace) DO UPDATE SET
        relation = EXCLUDED.relation,
        key_column = EXCLUDED.key_column,
        value_column = EXCLUDED.value_column,
        writable = EXCLUDED.writable;

    EXECUTE format(
        'DROP TRIGGER IF EXISTS pg_local_cache_statement_guard ON %s',
        p_relation
    );
    EXECUTE format(
        'CREATE TRIGGER pg_local_cache_statement_guard
           BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s
           FOR EACH STATEMENT
           EXECUTE FUNCTION local_cache._statement_guard()',
        p_relation
    );
    EXECUTE format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_statement_guard',
        p_relation
    );

    EXECUTE format(
        'DROP TRIGGER IF EXISTS pg_local_cache_row_invalidate ON %s',
        p_relation
    );
    EXECUTE format(
        'CREATE TRIGGER pg_local_cache_row_invalidate
           AFTER INSERT OR UPDATE OR DELETE ON %s
           FOR EACH ROW
           EXECUTE FUNCTION local_cache._row_invalidate(%L, %L)',
        p_relation, p_namespace, p_key_column
    );
    EXECUTE format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_row_invalidate',
        p_relation
    );

    EXECUTE format(
        'DROP TRIGGER IF EXISTS pg_local_cache_truncate_invalidate ON %s',
        p_relation
    );
    EXECUTE format(
        'CREATE TRIGGER pg_local_cache_truncate_invalidate
           AFTER TRUNCATE ON %s
           FOR EACH STATEMENT
           EXECUTE FUNCTION local_cache._truncate_invalidate(%L)',
        p_relation, p_namespace
    );
    EXECUTE format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_truncate_invalidate',
        p_relation
    );

    PERFORM local_cache._reload();
END;
$function$;

CREATE FUNCTION unregister_mapping(p_namespace text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relation oid;
BEGIN
    DELETE FROM local_cache.mapping
     WHERE namespace = p_namespace
     RETURNING relation::oid INTO v_relation;

    IF FOUND THEN
        PERFORM local_cache._forget(p_namespace, v_relation);
        IF EXISTS (
            SELECT 1 FROM pg_class AS c WHERE c.oid = v_relation
        ) THEN
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_statement_guard ON %s',
                v_relation::regclass
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_row_invalidate ON %s',
                v_relation::regclass
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_local_cache_truncate_invalidate ON %s',
                v_relation::regclass
            );
        END IF;
    END IF;
    PERFORM local_cache._reload();
END;
$function$;

CREATE FUNCTION attach_table(
    p_relation regclass,
    p_value_column name DEFAULT NULL,
    p_namespace text DEFAULT NULL,
    p_writable boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema_name name;
    v_relation_name name;
    v_relkind "char";
    v_relpersistence "char";
    v_relispartition boolean;
    v_primary_key_columns integer;
    v_key_column name;
    v_value_column name;
    v_value_candidates integer;
    v_namespace text;
    v_worker_role text;
    v_worker_is_superuser boolean;
    v_worker_is_dedicated boolean;
    v_existing_namespace text;
    v_qualified_relation text;
    v_key_template text;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF p_writable IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache writable flag must not be NULL';
    END IF;

    SELECT n.nspname, c.relname, c.relkind, c.relpersistence,
           c.relispartition
      INTO v_schema_name, v_relation_name, v_relkind, v_relpersistence,
           v_relispartition
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'pg_local_cache relation % does not exist', p_relation;
    END IF;
    IF v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION
            'pg_local_cache can attach only a permanent ordinary table: %',
            p_relation;
    END IF;
    -- Check exact hierarchy membership rather than sticky relhassubclass.
    IF v_relispartition OR EXISTS (
        SELECT 1
          FROM pg_catalog.pg_inherits AS inh
         WHERE inh.inhparent = p_relation
            OR inh.inhrelid = p_relation
    ) THEN
        RAISE EXCEPTION 'table inheritance is not supported by pg_local_cache'
            USING HINT =
                'Attach a standalone table with no inheritance parent or children.';
    END IF;

    SELECT i.indnkeyatts, a.attname
      INTO v_primary_key_columns, v_key_column
      FROM pg_catalog.pg_index AS i
      LEFT JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = i.indrelid
       AND a.attnum = i.indkey[0]
       AND a.attnum > 0
       AND NOT a.attisdropped
     WHERE i.indrelid = p_relation
       AND i.indisprimary;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'table % has no primary key', p_relation
            USING HINT =
                'Add a single-column PRIMARY KEY before attaching the table.';
    END IF;
    IF v_primary_key_columns <> 1 OR v_key_column IS NULL THEN
        RAISE EXCEPTION
            'table % has a composite primary key with % columns',
            p_relation, v_primary_key_columns
            USING HINT =
                'Composite primary keys are not supported yet; use a single-column surrogate PRIMARY KEY.';
    END IF;

    IF p_value_column IS NULL THEN
        SELECT count(*), min(a.attname::text)::name
          INTO v_value_candidates, v_value_column
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname <> v_key_column;
        IF v_value_candidates <> 1 OR v_value_column IS NULL THEN
            RAISE EXCEPTION
                'table % has % non-primary-key columns',
                p_relation, v_value_candidates
                USING HINT =
                    'Pass p_value_column unless the table has exactly one non-primary-key column.';
        END IF;
    ELSE
        v_value_column := p_value_column;
    END IF;

    v_namespace := COALESCE(
        p_namespace,
        v_relation_name::text
    );
    IF v_namespace !~ '^[A-Za-z0-9_.-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid pg_local_cache namespace: %', v_namespace
            USING HINT =
                'Pass p_namespace with 1-63 ASCII letters, digits, dot, dash, or underscore.';
    END IF;

    v_worker_role := pg_catalog.current_setting(
        'pg_local_cache.role', true
    );
    IF v_worker_role IS NULL OR v_worker_role = '' THEN
        RAISE EXCEPTION 'pg_local_cache.role is not configured'
            USING HINT =
                'Configure a dedicated non-superuser worker role and restart PostgreSQL.';
    END IF;

    SELECT r.rolsuper,
           r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolinherit
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls
           AND pg_catalog.has_database_privilege(
               r.oid, pg_catalog.current_database(), 'CONNECT'
           )
           AND pg_catalog.has_schema_privilege(
               r.oid, 'local_cache', 'USAGE'
           )
           AND pg_catalog.has_table_privilege(
               r.oid, 'local_cache.mapping', 'SELECT'
           )
      INTO v_worker_is_superuser, v_worker_is_dedicated
      FROM pg_catalog.pg_roles AS r
     WHERE r.rolname = v_worker_role;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % does not exist',
            v_worker_role
            USING HINT =
                'Create the configured role before attaching a table.';
    END IF;
    IF v_worker_is_superuser THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % must not be a superuser',
            v_worker_role
            USING HINT =
                'Use a dedicated LOGIN NOSUPERUSER role for pg_local_cache workers.';
    END IF;
    IF v_worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % is not a dedicated least-privilege role',
            v_worker_role
            USING HINT =
                'Require LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS plus CONNECT and read-only local_cache metadata access.';
    END IF;

    /* Serialize namespace/relation conflict checks with register_mapping(). */
    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

    SELECT m.namespace
      INTO v_existing_namespace
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation
       AND m.namespace <> v_namespace;

    IF FOUND THEN
        RAISE EXCEPTION 'table % is already attached as namespace %',
            p_relation, v_existing_namespace
            USING HINT =
                'Unregister the existing mapping before changing its namespace.';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM local_cache.mapping AS m
         WHERE m.namespace = v_namespace
           AND m.relation <> p_relation
    ) THEN
        RAISE EXCEPTION 'pg_local_cache namespace % is already in use',
            v_namespace
            USING HINT =
                'Pass a different p_namespace or unregister the existing mapping.';
    END IF;

    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA %I TO %I',
        v_schema_name, v_worker_role
    );
    EXECUTE pg_catalog.format(
        CASE WHEN p_writable
             THEN 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I'
             ELSE 'GRANT SELECT ON TABLE %I.%I TO %I'
        END,
        v_schema_name, v_relation_name, v_worker_role
    );

    PERFORM local_cache.register_mapping(
        v_namespace,
        p_relation,
        v_key_column,
        v_value_column,
        p_writable
    );

    v_qualified_relation := pg_catalog.format(
        '%I.%I', v_schema_name, v_relation_name
    );
    v_key_template := pg_catalog.format(
        '%s:<%s>', v_namespace, v_key_column
    );

    RETURN pg_catalog.jsonb_build_object(
        'relation', v_qualified_relation,
        'namespace', v_namespace,
        'primary_key_column', v_key_column,
        'value_column', v_value_column,
        'writable', p_writable,
        'worker_role', v_worker_role,
        'templates', pg_catalog.jsonb_build_object(
            'key', v_key_template,
            'get', pg_catalog.format('GET %s', v_key_template),
            'set', CASE WHEN p_writable THEN pg_catalog.format(
                'SET %s <value>', v_key_template
            ) ELSE NULL END,
            'del', CASE WHEN p_writable THEN pg_catalog.format(
                'DEL %s', v_key_template
            ) ELSE NULL END
        )
    );
END;
$function$;

REVOKE ALL ON FUNCTION register_mapping(text, regclass, name, name, boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION unregister_mapping(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_table(regclass, name, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION _statement_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION _row_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _truncate_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _ddl_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _sql_drop_invalidate() FROM PUBLIC;

CREATE EVENT TRIGGER pg_local_cache_ddl_invalidate
    ON ddl_command_end
    EXECUTE FUNCTION local_cache._ddl_invalidate();

CREATE EVENT TRIGGER pg_local_cache_sql_drop_invalidate
    ON sql_drop
    EXECUTE FUNCTION local_cache._sql_drop_invalidate();

SELECT local_cache._reload();
