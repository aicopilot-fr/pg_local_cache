/* Serialize the preflight and catalog migration with every 1.0 registration. */
LOCK TABLE local_cache.mapping IN ACCESS EXCLUSIVE MODE;

/* CRUD becomes the reserved whole-row wire prefix in 1.1. */
DO $pg_local_cache_upgrade$
BEGIN
    IF EXISTS (
        SELECT 1 FROM local_cache.mapping WHERE namespace = 'CRUD'
    ) THEN
        RAISE EXCEPTION
            'cannot upgrade pg_local_cache while legacy namespace CRUD exists'
            USING HINT =
                'Unregister that 1.0 mapping, register the same table under a different namespace, and retry ALTER EXTENSION.';
    END IF;
    IF pg_catalog.to_regprocedure(
        'local_cache._attach_table_1_0_compat(regclass,name,text,boolean)'
    ) IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot upgrade pg_local_cache: compatibility function name is occupied'
            USING HINT =
                'Rename local_cache._attach_table_1_0_compat(regclass,name,text,boolean) and retry ALTER EXTENSION.';
    END IF;
END;
$pg_local_cache_upgrade$;

/* Migrate scalar 1.0 mappings before replacing the administrative API. */
ALTER TABLE local_cache.mapping
    ADD COLUMN key_columns name[];
UPDATE local_cache.mapping
   SET key_columns = ARRAY[key_column]::name[];
ALTER TABLE local_cache.mapping
    ALTER COLUMN key_columns SET NOT NULL,
    ALTER COLUMN key_column DROP NOT NULL,
    ALTER COLUMN value_column DROP NOT NULL,
    ADD CONSTRAINT mapping_key_columns_shape CHECK (
        pg_catalog.array_ndims(key_columns) = 1
        AND pg_catalog.array_lower(key_columns, 1) = 1
        AND pg_catalog.cardinality(key_columns) BETWEEN 1 AND 16
        AND pg_catalog.array_position(key_columns, NULL::name) IS NULL
    ),
    ADD CONSTRAINT mapping_key_column_projection CHECK (
        key_column IS NOT DISTINCT FROM
            CASE WHEN pg_catalog.cardinality(key_columns) = 1
                 THEN key_columns[1]
                 ELSE NULL::name
            END
    );

ALTER TABLE local_cache.mapping
    DROP CONSTRAINT mapping_namespace_check,
    ADD CONSTRAINT mapping_namespace_shape CHECK (
        namespace ~ '^[A-Za-z0-9_.-]{1,63}$'
        AND namespace <> 'CRUD'
    );

SELECT pg_catalog.pg_extension_config_dump('local_cache.mapping', '');

CREATE FUNCTION _mapping_changed()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM local_cache._reload();
    RETURN NULL;
END;
$function$;

CREATE TRIGGER pg_local_cache_mapping_reload
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON local_cache.mapping
    FOR EACH STATEMENT
    EXECUTE FUNCTION local_cache._mapping_changed();

CREATE OR REPLACE FUNCTION _ddl_invalidate()
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
    ) OR EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger_ddl_commands() AS d
         WHERE d.classid IN (
             'pg_catalog.pg_type'::regclass,
             'pg_catalog.pg_proc'::regclass,
             'pg_catalog.pg_cast'::regclass,
             'pg_catalog.pg_collation'::regclass
         )
    ) THEN
        PERFORM local_cache._reload();
    END IF;
END;
$function$;

CREATE FUNCTION _validate_attach_relation(p_relation regclass)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relkind "char";
    v_relpersistence "char";
    v_relispartition boolean;
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
BEGIN
    SELECT c.relkind, c.relpersistence, c.relispartition,
           c.relrowsecurity, c.relforcerowsecurity
      INTO v_relkind, v_relpersistence, v_relispartition,
           v_relrowsecurity, v_relforcerowsecurity
      FROM pg_catalog.pg_class AS c
     WHERE c.oid = p_relation;
    IF NOT FOUND OR v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION
            'pg_local_cache supports only permanent ordinary tables: %',
            p_relation;
    END IF;
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
            USING HINT =
                'Use a dedicated table or a security-barrier API instead.';
    END IF;
END;
$function$;

CREATE FUNCTION _primary_key_columns(p_relation regclass)
RETURNS name[]
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT ARRAY(
           SELECT a.attname
             FROM pg_catalog.unnest(i.indkey::smallint[])
                  WITH ORDINALITY AS key(attnum, key_position)
             JOIN pg_catalog.pg_attribute AS a
               ON a.attrelid = i.indrelid
              AND a.attnum = key.attnum
              AND a.attnum > 0
              AND NOT a.attisdropped
            WHERE key.key_position <= i.indnkeyatts
            ORDER BY key.key_position
       )::name[]
  FROM pg_catalog.pg_index AS i
 WHERE i.indrelid = p_relation
   AND i.indisprimary;
$function$;

CREATE FUNCTION _default_namespace(p_relation regclass)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT CASE
       WHEN (n.nspname || '.' || c.relname) ~ '^[A-Za-z0-9_.-]{1,63}$'
       THEN n.nspname || '.' || c.relname
       ELSE 'rel_' || c.oid::text
       END
  FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n
    ON n.oid = c.relnamespace
 WHERE c.oid = p_relation;
$function$;

CREATE FUNCTION _mapping_result(
    p_namespace text,
    p_relation regclass,
    p_key_columns name[],
    p_value_column name,
    p_writable boolean
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema_name name;
    v_relation_name name;
    v_qualified_relation text;
    v_wire_relation text;
    v_key_object text;
    v_key_template text;
BEGIN
    SELECT n.nspname, c.relname
      INTO STRICT v_schema_name, v_relation_name
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    SELECT '{' || pg_catalog.string_agg(
               pg_catalog.to_json(key_column::text)::text || ':' ||
               pg_catalog.to_json('<' || key_column::text || '>')::text,
               ',' ORDER BY key_position
           ) || '}'
      INTO v_key_object
      FROM pg_catalog.unnest(p_key_columns)
           WITH ORDINALITY AS key(key_column, key_position);

    v_qualified_relation := pg_catalog.format(
        '%I.%I', v_schema_name, v_relation_name
    );
    /* KVik wire names are literal components, not SQL identifiers. */
    v_wire_relation := pg_catalog.current_database() || '.' ||
        v_schema_name || '.' || v_relation_name;
    IF p_value_column IS NULL THEN
        v_key_template := 'CRUD:' || v_wire_relation || ':' || v_key_object;
    ELSE
        /* The legacy scalar protocol is namespace:key, independent of SQL names. */
        v_key_template := pg_catalog.format(
            '%s:<%s>', p_namespace, p_key_columns[1]
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'relation', v_qualified_relation,
        'namespace', p_namespace,
        'primary_key_columns', pg_catalog.to_jsonb(p_key_columns),
        'primary_key_column', CASE
            WHEN pg_catalog.cardinality(p_key_columns) = 1
            THEN p_key_columns[1]
            ELSE NULL
        END,
        'whole_row', p_value_column IS NULL,
        'value_column', p_value_column,
        'writable', p_writable,
        'worker_role', pg_catalog.current_setting('pg_local_cache.role', true),
        'templates', pg_catalog.jsonb_build_object(
            'key', v_key_template,
            'get', 'GET ' || v_key_template,
            'set', CASE WHEN p_writable THEN
                'SET ' || v_key_template || CASE
                    WHEN p_value_column IS NULL THEN ' <row-json>'
                    ELSE ' <value>'
                END
                ELSE NULL
            END,
            'del', CASE WHEN p_writable THEN 'DEL ' || v_key_template ELSE NULL END,
            'invalidate', CASE WHEN p_value_column IS NULL THEN
                'INVALIDATE CRUD:' || v_wire_relation
                ELSE 'INVALIDATE ' || p_namespace
            END,
            'invalidate_key', 'INVALIDATE ' || v_key_template,
            'invalidate_database', 'INVALIDATE CRUD:' ||
                pg_catalog.current_database(),
            'invalidate_all', 'INVALIDATE CRUD'
        )
    );
END;
$function$;

CREATE FUNCTION _register_mapping(
    p_namespace text,
    p_relation regclass,
    p_key_columns name[],
    p_value_column name,
    p_writable boolean
)
RETURNS void
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
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
    v_primary_key_count integer;
    v_primary_key_columns name[];
    v_primary_key_valid boolean;
    v_has_primary_key boolean;
    v_key_attribute_count integer;
    v_key_column name;
    v_key_position integer;
    v_key_type oid;
    v_key_collation oid;
    v_key_not_null boolean;
    v_value_type oid;
    v_value_not_null boolean;
    v_value_generated "char";
    v_value_identity "char";
    v_worker_role text;
    v_configured_database text;
    v_worker_is_superuser boolean;
    v_worker_is_dedicated boolean;
    v_existing_namespace text;
    v_old_relation oid;
    v_trigger_arguments text;
BEGIN
    IF p_namespace IS NULL OR p_namespace = 'CRUD' OR
       p_namespace !~ '^[A-Za-z0-9_.-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid pg_local_cache namespace: %', p_namespace
            USING HINT =
                'Use 1-63 ASCII letters, digits, dot, dash, or underscore.';
    END IF;
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF p_writable IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache writable flag must not be NULL';
    END IF;

    v_configured_database := pg_catalog.current_setting(
        'pg_local_cache.database', true
    );
    IF v_configured_database IS NULL OR
       v_configured_database <> pg_catalog.current_database() THEN
        RAISE EXCEPTION
            'database % is not served by pg_local_cache workers (configured: %)',
            pg_catalog.current_database(), v_configured_database
            USING HINT =
                'Set pg_local_cache.database at postmaster start or attach the table in the configured database.';
    END IF;
    IF p_key_columns IS NULL OR
       COALESCE(pg_catalog.array_ndims(p_key_columns), 0) <> 1 OR
       COALESCE(pg_catalog.array_lower(p_key_columns, 1), 0) <> 1 OR
       pg_catalog.cardinality(p_key_columns) NOT BETWEEN 1 AND 16 OR
       pg_catalog.array_position(p_key_columns, NULL::name) IS NOT NULL THEN
        RAISE EXCEPTION 'pg_local_cache key_columns must contain 1 to 16 names'
            USING HINT =
                'Pass a one-dimensional, one-based array with no NULL entries.';
    END IF;

    SELECT n.nspname, c.relname, c.relkind, c.relpersistence,
           c.relispartition, c.relrowsecurity, c.relforcerowsecurity
      INTO v_schema_name, v_relation_name, v_relkind, v_relpersistence,
           v_relispartition, v_relrowsecurity, v_relforcerowsecurity
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    IF NOT FOUND OR v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION
            'pg_local_cache supports only permanent ordinary tables: %',
            p_relation;
    END IF;
    IF p_value_column IS NULL AND (
       pg_catalog.current_database() ~ '[.:]' OR
       v_schema_name::text ~ '[.:]' OR v_relation_name::text ~ '[.:]') THEN
        RAISE EXCEPTION
            'KVik wire names cannot contain dot or colon: %.%',
            v_schema_name, v_relation_name
            USING HINT =
                'Rename the database, schema, or table before attaching it.';
    END IF;
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
            USING HINT =
                'Use a dedicated table or a security-barrier API instead.';
    END IF;

    SELECT i.indnkeyatts,
           ARRAY(
               SELECT a.attname
                 FROM pg_catalog.unnest(i.indkey::smallint[])
                      WITH ORDINALITY AS key(attnum, key_position)
                 JOIN pg_catalog.pg_attribute AS a
                   ON a.attrelid = i.indrelid
                  AND a.attnum = key.attnum
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                WHERE key.key_position <= i.indnkeyatts
                ORDER BY key.key_position
           )::name[],
           i.indisunique
           AND i.indimmediate
           AND i.indisvalid
           AND i.indisready
           AND i.indpred IS NULL
           AND i.indexprs IS NULL
           AND am.amname = 'btree'
      INTO v_primary_key_count, v_primary_key_columns, v_primary_key_valid
      FROM pg_catalog.pg_index AS i
      JOIN pg_catalog.pg_class AS ic
        ON ic.oid = i.indexrelid
      JOIN pg_catalog.pg_am AS am
        ON am.oid = ic.relam
     WHERE i.indrelid = p_relation
       AND i.indisprimary;

    v_has_primary_key := FOUND;
    IF p_value_column IS NULL THEN
        IF NOT v_has_primary_key THEN
            RAISE EXCEPTION 'table % has no primary key', p_relation
                USING HINT =
                    'Add a PRIMARY KEY with at most 16 supported columns before attaching the table.';
        END IF;
        IF v_primary_key_count NOT BETWEEN 1 AND 16 THEN
            RAISE EXCEPTION 'table % primary key has % columns; maximum is 16',
                p_relation, v_primary_key_count;
        END IF;
        IF NOT v_primary_key_valid OR
           pg_catalog.cardinality(v_primary_key_columns) <>
               v_primary_key_count THEN
            RAISE EXCEPTION 'table % primary key is not cache-safe', p_relation
                USING HINT =
                    'Use a valid, ready, immediate, non-partial btree PRIMARY KEY over table columns.';
        END IF;
        IF p_key_columns <> v_primary_key_columns THEN
            RAISE EXCEPTION
                'key_columns % do not exactly match primary key columns % on %',
                p_key_columns, v_primary_key_columns, p_relation
                USING HINT =
                    'Pass every PRIMARY KEY column exactly once and in primary-key order.';
        END IF;
    ELSIF pg_catalog.cardinality(p_key_columns) <> 1 THEN
        RAISE EXCEPTION 'scalar mappings require exactly one unique key column';
    END IF;

    SELECT pg_catalog.count(*)
      INTO v_key_attribute_count
      FROM pg_catalog.unnest(p_key_columns) AS key(key_column)
      JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = p_relation
       AND a.attname = key.key_column
       AND a.attnum > 0
       AND NOT a.attisdropped;
    IF v_key_attribute_count <> pg_catalog.cardinality(p_key_columns) THEN
        RAISE EXCEPTION 'one or more key columns % do not exist on %',
            p_key_columns, p_relation;
    END IF;

    FOR v_key_column, v_key_position, v_key_type,
        v_key_collation, v_key_not_null IN
        SELECT key.key_column, key.key_position,
               a.atttypid, a.attcollation, a.attnotnull
          FROM pg_catalog.unnest(p_key_columns)
               WITH ORDINALITY AS key(key_column, key_position)
          JOIN pg_catalog.pg_attribute AS a
            ON a.attrelid = p_relation
           AND a.attname = key.key_column
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY key.key_position
    LOOP
        IF NOT v_key_not_null THEN
            RAISE EXCEPTION 'primary-key column %.% must be NOT NULL',
                p_relation, v_key_column;
        END IF;
        IF v_key_type NOT IN (
            'int2'::regtype, 'int4'::regtype, 'int8'::regtype,
            'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype,
            'uuid'::regtype
        ) THEN
            RAISE EXCEPTION 'unsupported key type % for column %.%',
                v_key_type::regtype, p_relation, v_key_column
                USING HINT =
                    'Supported key types: int2, int4, int8, text, varchar, bpchar, uuid.';
        END IF;
        IF NOT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_index AS i
              JOIN pg_catalog.pg_class AS ic
                ON ic.oid = i.indexrelid
              JOIN pg_catalog.pg_am AS am
                ON am.oid = ic.relam
               AND am.amname = 'btree'
              JOIN pg_catalog.pg_opclass AS opc
                ON opc.oid = i.indclass[v_key_position - 1]
               AND opc.opcmethod = am.oid
               AND opc.opcdefault
               AND (
                   opc.opcintype = v_key_type
                   OR EXISTS (
                       SELECT 1
                         FROM pg_catalog.pg_cast AS pc
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
               AND i.indexprs IS NULL
               AND i.indnkeyatts = pg_catalog.cardinality(p_key_columns)
               AND (p_value_column IS NOT NULL OR i.indisprimary)
               AND i.indkey[v_key_position - 1] = (
                   SELECT a.attnum
                     FROM pg_catalog.pg_attribute AS a
                    WHERE a.attrelid = p_relation
                      AND a.attname = v_key_column
                      AND a.attnum > 0
                      AND NOT a.attisdropped
               )
        ) THEN
            IF p_value_column IS NOT NULL THEN
                RAISE EXCEPTION
                    'key column %.% needs a valid single-column UNIQUE index',
                    p_relation, v_key_column;
            ELSE
                RAISE EXCEPTION
                    'primary-key column %.% must use its default btree operator class',
                    p_relation, v_key_column
                    USING HINT =
                        'The cache uses PostgreSQL default equality semantics for key lookups.';
            END IF;
        END IF;
        IF v_key_collation <> 0 AND EXISTS (
            SELECT 1
              FROM pg_catalog.pg_collation AS coll
             WHERE coll.oid = v_key_collation
               AND NOT coll.collisdeterministic
        ) THEN
            RAISE EXCEPTION
                'nondeterministic key collation is not supported for %.%',
                p_relation, v_key_column
                USING HINT =
                    'Use deterministic collations so SQL equality and cache invalidation agree.';
        END IF;
    END LOOP;

    IF p_value_column IS NOT NULL THEN
        IF p_value_column = ANY (p_key_columns) THEN
            RAISE EXCEPTION 'value column must differ from primary-key columns';
        END IF;
        SELECT a.atttypid, a.attnotnull, a.attgenerated, a.attidentity
          INTO v_value_type, v_value_not_null,
               v_value_generated, v_value_identity
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attname = p_value_column
           AND a.attnum > 0
           AND NOT a.attisdropped;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'value column % does not exist on %',
                p_value_column, p_relation;
        END IF;
        IF NOT v_value_not_null THEN
            RAISE EXCEPTION 'scalar value column %.% must be NOT NULL',
                p_relation, p_value_column;
        END IF;
        IF v_value_type NOT IN (
            'int2'::regtype, 'int4'::regtype, 'int8'::regtype,
            'numeric'::regtype, 'bool'::regtype,
            'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype,
            'uuid'::regtype, 'json'::regtype, 'jsonb'::regtype
        ) THEN
            RAISE EXCEPTION 'unsupported scalar value type %',
                v_value_type::regtype
                USING HINT =
                    'Use a built-in scalar value type documented by pg_local_cache.';
        END IF;
    END IF;

    IF p_writable AND p_value_column IS NULL AND EXISTS (
        SELECT 1
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname = ANY (p_key_columns)
           AND a.attgenerated <> ''
    ) THEN
        RAISE EXCEPTION
            'writable whole-row mappings do not support generated primary keys'
            USING HINT =
                'Use a read-only mapping or a non-generated primary key; identity columns are supported.';
    END IF;
    IF p_writable AND p_value_column IS NOT NULL AND (
        v_value_generated <> '' OR v_value_identity <> '' OR EXISTS (
            SELECT 1
              FROM pg_catalog.pg_attribute AS a
             WHERE a.attrelid = p_relation
               AND a.attname = ANY (p_key_columns)
               AND (a.attgenerated <> '' OR a.attidentity <> '')
        )
    ) THEN
        RAISE EXCEPTION
            'writable scalar mappings do not support generated or identity key/value columns';
    END IF;
    IF p_writable AND p_value_column IS NOT NULL AND EXISTS (
        SELECT 1
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname <> ALL (p_key_columns)
           AND a.attname <> p_value_column
           AND a.attnotnull
           AND NOT a.atthasdef
           AND a.attgenerated = ''
           AND a.attidentity = ''
    ) THEN
        RAISE EXCEPTION
            'writable scalar mapping has another NOT NULL column without a default'
            USING HINT =
                'SET supplies only the configured primary key and value column.';
    END IF;

    v_worker_role := pg_catalog.current_setting('pg_local_cache.role', true);
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
            v_worker_role;
    END IF;
    IF v_worker_is_superuser THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % must not be a superuser',
            v_worker_role;
    END IF;
    IF v_worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % is not a dedicated least-privilege role',
            v_worker_role
            USING HINT =
                'Require LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS plus CONNECT and read-only local_cache metadata access.';
    END IF;

    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

    SELECT m.namespace
      INTO v_existing_namespace
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation
       AND m.namespace <> p_namespace;
    IF FOUND THEN
        RAISE EXCEPTION 'table % is already attached as namespace %',
            p_relation, v_existing_namespace
            USING HINT =
                'Unregister the existing mapping before changing its namespace.';
    END IF;

    SELECT relation::oid
      INTO v_old_relation
      FROM local_cache.mapping
     WHERE namespace = p_namespace;
    IF FOUND AND v_old_relation <> p_relation::oid THEN
        RAISE EXCEPTION
            'namespace % is already attached to table %',
            p_namespace, v_old_relation::regclass
            USING HINT =
                'Call unregister_mapping() first, or use docker/attach-table.sh --replace.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM local_cache.mapping WHERE namespace = p_namespace
    ) AND (SELECT pg_catalog.count(*) FROM local_cache.mapping) >= 128 THEN
        RAISE EXCEPTION 'pg_local_cache supports at most 128 mappings';
    END IF;

    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA %I TO %I',
        v_schema_name, v_worker_role
    );
    IF p_writable THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
            v_schema_name, v_relation_name, v_worker_role
        );
    ELSE
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE %I.%I TO %I',
            v_schema_name, v_relation_name, v_worker_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE INSERT, UPDATE, DELETE ON TABLE %I.%I FROM %I',
            v_schema_name, v_relation_name, v_worker_role
        );
    END IF;

    INSERT INTO local_cache.mapping(
        namespace, relation, key_columns, key_column, value_column, writable
    )
    VALUES (
        p_namespace, p_relation, p_key_columns,
        CASE WHEN pg_catalog.cardinality(p_key_columns) = 1
             THEN p_key_columns[1]
             ELSE NULL::name
        END,
        p_value_column, p_writable
    )
    ON CONFLICT (namespace) DO UPDATE SET
        relation = EXCLUDED.relation,
        key_columns = EXCLUDED.key_columns,
        key_column = EXCLUDED.key_column,
        value_column = EXCLUDED.value_column,
        writable = EXCLUDED.writable;

    EXECUTE pg_catalog.format(
        'DROP TRIGGER IF EXISTS pg_local_cache_statement_guard ON %s',
        p_relation
    );
    EXECUTE pg_catalog.format(
        'CREATE TRIGGER pg_local_cache_statement_guard
           BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s
           FOR EACH STATEMENT
           EXECUTE FUNCTION local_cache._statement_guard()',
        p_relation
    );
    EXECUTE pg_catalog.format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_statement_guard',
        p_relation
    );

    v_trigger_arguments := pg_catalog.quote_literal(p_namespace);
    FOREACH v_key_column IN ARRAY p_key_columns LOOP
        v_trigger_arguments := v_trigger_arguments || ', ' ||
            pg_catalog.quote_literal(v_key_column::text);
    END LOOP;
    EXECUTE pg_catalog.format(
        'DROP TRIGGER IF EXISTS pg_local_cache_row_invalidate ON %s',
        p_relation
    );
    EXECUTE pg_catalog.format(
        'CREATE TRIGGER pg_local_cache_row_invalidate
           AFTER INSERT OR UPDATE OR DELETE ON %s
           FOR EACH ROW
           EXECUTE FUNCTION local_cache._row_invalidate(%s)',
        p_relation, v_trigger_arguments
    );
    EXECUTE pg_catalog.format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_row_invalidate',
        p_relation
    );

    EXECUTE pg_catalog.format(
        'DROP TRIGGER IF EXISTS pg_local_cache_truncate_invalidate ON %s',
        p_relation
    );
    EXECUTE pg_catalog.format(
        'CREATE TRIGGER pg_local_cache_truncate_invalidate
           AFTER TRUNCATE ON %s
           FOR EACH STATEMENT
           EXECUTE FUNCTION local_cache._truncate_invalidate(%L)',
        p_relation, p_namespace
    );
    EXECUTE pg_catalog.format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_truncate_invalidate',
        p_relation
    );

    PERFORM local_cache._reload();
END;
$function$;

CREATE FUNCTION register_mapping(
    p_namespace text,
    p_relation regclass,
    p_key_columns name[],
    p_writable boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM local_cache._register_mapping(
        p_namespace, p_relation, p_key_columns, NULL::name, p_writable
    );
END;
$function$;

CREATE FUNCTION register_value_mapping(
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
BEGIN
    IF p_key_column IS NULL OR p_value_column IS NULL THEN
        RAISE EXCEPTION 'scalar key and value columns must not be NULL';
    END IF;
    PERFORM local_cache._register_mapping(
        p_namespace, p_relation, ARRAY[p_key_column]::name[],
        p_value_column, p_writable
    );
END;
$function$;

/* Preserve the 1.0 function OID and any explicit grants while routing it to
 * the maintained scalar implementation. */
CREATE OR REPLACE FUNCTION register_mapping(
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
BEGIN
    PERFORM local_cache.register_value_mapping(
        p_namespace, p_relation, p_key_column, p_value_column, p_writable
    );
END;
$function$;

CREATE OR REPLACE FUNCTION unregister_mapping(p_namespace text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relation oid;
    v_worker_role text;
BEGIN
    IF p_namespace IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache namespace must not be NULL';
    END IF;
    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;
    DELETE FROM local_cache.mapping
     WHERE namespace = p_namespace
     RETURNING relation::oid INTO v_relation;

    IF FOUND THEN
        PERFORM local_cache._forget(p_namespace, v_relation);
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS c WHERE c.oid = v_relation
        ) THEN
            EXECUTE pg_catalog.format(
                'DROP TRIGGER IF EXISTS pg_local_cache_statement_guard ON %s',
                v_relation::regclass
            );
            EXECUTE pg_catalog.format(
                'DROP TRIGGER IF EXISTS pg_local_cache_row_invalidate ON %s',
                v_relation::regclass
            );
            EXECUTE pg_catalog.format(
                'DROP TRIGGER IF EXISTS pg_local_cache_truncate_invalidate ON %s',
                v_relation::regclass
            );
            v_worker_role := pg_catalog.current_setting(
                'pg_local_cache.role', true
            );
            IF v_worker_role IS NOT NULL AND EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_roles AS r
                 WHERE r.rolname = v_worker_role
            ) THEN
                EXECUTE pg_catalog.format(
                    'REVOKE ALL PRIVILEGES ON TABLE %s FROM %I',
                    v_relation::regclass, v_worker_role
                );
            END IF;
        END IF;
    END IF;
    PERFORM local_cache._reload();
END;
$function$;

/* PostgreSQL cannot remove a parameter default with CREATE OR REPLACE.
 * Renaming preserves the 1.0 function OID, dependent objects, and ACL while
 * freeing the public name before any 1.1 overload is introduced. */
ALTER FUNCTION local_cache.attach_table(regclass, name, text, boolean)
    RENAME TO _attach_table_1_0_compat;

CREATE FUNCTION attach_table(
    p_relation regclass,
    p_row_writable boolean,
    p_row_namespace text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key_columns name[];
    v_namespace text;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    PERFORM local_cache._validate_attach_relation(p_relation);
    v_key_columns := local_cache._primary_key_columns(p_relation);
    IF v_key_columns IS NULL THEN
        RAISE EXCEPTION 'table % has no primary key', p_relation
            USING HINT =
                'Add a PRIMARY KEY with at most 16 supported columns before attaching the table.';
    END IF;
    v_namespace := COALESCE(
        p_row_namespace, local_cache._default_namespace(p_relation)
    );
    PERFORM local_cache.register_mapping(
        v_namespace, p_relation, v_key_columns, p_row_writable
    );
    RETURN local_cache._mapping_result(
        v_namespace, p_relation, v_key_columns, NULL::name, p_row_writable
    );
END;
$function$;

CREATE FUNCTION attach_value(
    p_relation regclass,
    p_value_column name,
    p_namespace text DEFAULT NULL,
    p_writable boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key_columns name[];
    v_namespace text;
    v_value_column name;
    v_value_candidates integer;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    PERFORM local_cache._validate_attach_relation(p_relation);
    v_key_columns := local_cache._primary_key_columns(p_relation);
    IF v_key_columns IS NULL THEN
        RAISE EXCEPTION 'table % has no primary key', p_relation;
    END IF;
    IF pg_catalog.cardinality(v_key_columns) <> 1 THEN
        RAISE EXCEPTION
            'scalar mappings require a single-column primary key; table % has % columns',
            p_relation, pg_catalog.cardinality(v_key_columns)
            USING HINT =
                'Use attach_table() to cache a whole row with a composite primary key.';
    END IF;
    IF p_value_column IS NULL THEN
        SELECT pg_catalog.count(*),
               pg_catalog.min(a.attname::text)::name
          INTO v_value_candidates, v_value_column
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname <> v_key_columns[1];
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
        p_namespace, local_cache._default_namespace(p_relation)
    );
    PERFORM local_cache.register_value_mapping(
        v_namespace, p_relation, v_key_columns[1],
        v_value_column, p_writable
    );
    RETURN local_cache._mapping_result(
        v_namespace, p_relation, v_key_columns,
        v_value_column, p_writable
    );
END;
$function$;

/* Align the preserved object with fresh 1.1 while keeping its OID and ACL. */
CREATE OR REPLACE FUNCTION _attach_table_1_0_compat(
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
BEGIN
    RETURN local_cache.attach_value(
        p_relation, p_value_column, p_namespace, p_writable
    );
END;
$function$;

CREATE FUNCTION attach_table(
    p_relation regclass,
    p_value_column name,
    p_namespace text DEFAULT NULL,
    p_writable boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RETURN local_cache.attach_value(
        p_relation, p_value_column, p_namespace, p_writable
    );
END;
$function$;

/* The exact one-argument overload is the unambiguous whole-row default. */
CREATE FUNCTION attach_table(p_relation regclass)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT local_cache.attach_table(p_relation, false, NULL::text);
$function$;

/* Add mapping health without changing the stable 1.0 metrics() row type. */
CREATE FUNCTION mapping_metrics()
RETURNS TABLE (
    workers_with_incomplete_mappings bigint,
    mapping_reload_incomplete_retries_total bigint
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
    (payload ->> 'workers_with_incomplete_mappings')::bigint,
    (payload ->> 'mapping_reload_incomplete_retries_total')::bigint
FROM snapshot;
$function$;

CREATE OR REPLACE FUNCTION health()
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
        AND (payload ->> 'workers_with_incomplete_mappings')::bigint = 0
        AND (payload ->> 'active_clients')::bigint <=
            (payload ->> 'max_clients')::bigint,
    'resp_enabled', (payload ->> 'workers_configured')::bigint > 0,
    'workers_configured', (payload ->> 'workers_configured')::bigint,
    'workers_running', (payload ->> 'workers_running')::bigint,
    'workers_with_incomplete_mappings',
        (payload ->> 'workers_with_incomplete_mappings')::bigint,
    'mapping_reload_incomplete_retries_total',
        (payload ->> 'mapping_reload_incomplete_retries_total')::bigint,
    'active_clients', (payload ->> 'active_clients')::bigint,
    'max_clients', (payload ->> 'max_clients')::bigint,
    'estimated_memory_bytes', (payload ->> 'estimated_memory_bytes')::bigint,
    'memory_budget_bytes', (payload ->> 'memory_budget_bytes')::bigint
)
FROM snapshot;
$function$;

REVOKE ALL ON FUNCTION _validate_attach_relation(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _primary_key_columns(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _mapping_changed() FROM PUBLIC;
REVOKE ALL ON FUNCTION _default_namespace(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _mapping_result(text, regclass, name[], name, boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION _register_mapping(text, regclass, name[], name, boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION register_mapping(text, regclass, name[], boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION register_mapping(
    text, regclass, name, name, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION register_value_mapping(
    text, regclass, name, name, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION unregister_mapping(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_table(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_table(regclass, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_table(
    regclass, name, text, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_value(regclass, name, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION _attach_table_1_0_compat(
    regclass, name, text, boolean
) FROM PUBLIC;
REVOKE ALL ON FUNCTION mapping_metrics() FROM PUBLIC;
REVOKE ALL ON FUNCTION health() FROM PUBLIC;

/* Copy non-PUBLIC 1.0 EXECUTE grants to the replacement public signature.
 * The renamed function retains its own ACL for objects bound to its OID. */
DO $pg_local_cache_copy_attach_acl$
DECLARE
    v_grant record;
BEGIN
    FOR v_grant IN
        SELECT r.rolname, acl.is_grantable
          FROM pg_catalog.pg_proc AS p
          CROSS JOIN LATERAL pg_catalog.aclexplode(
              COALESCE(
                  p.proacl,
                  pg_catalog.acldefault('f', p.proowner)
              )
          ) AS acl
          JOIN pg_catalog.pg_roles AS r
            ON r.oid = acl.grantee
         WHERE p.oid =
               'local_cache._attach_table_1_0_compat(regclass,name,text,boolean)'::regprocedure
           AND acl.privilege_type = 'EXECUTE'
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION local_cache.attach_table(regclass, name, text, boolean) TO %I%s',
            v_grant.rolname,
            CASE WHEN v_grant.is_grantable
                 THEN ' WITH GRANT OPTION'
                 ELSE ''
            END
        );
    END LOOP;
END;
$pg_local_cache_copy_attach_acl$;

SELECT local_cache._reload();
