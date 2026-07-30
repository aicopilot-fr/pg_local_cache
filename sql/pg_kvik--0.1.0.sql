\echo Use "CREATE EXTENSION pg_kvik" to load this file. \quit

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
AS 'MODULE_PATHNAME', 'pg_kvik_row_invalidate'
LANGUAGE C;

CREATE FUNCTION _truncate_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_kvik_truncate_invalidate'
LANGUAGE C;

CREATE FUNCTION _ddl_invalidate()
RETURNS event_trigger
AS 'MODULE_PATHNAME', 'pg_kvik_ddl_invalidate'
LANGUAGE C;

CREATE FUNCTION _reload()
RETURNS void
AS 'MODULE_PATHNAME', 'pg_kvik_reload'
LANGUAGE C;

CREATE FUNCTION invalidate(namespace text)
RETURNS bigint
AS 'MODULE_PATHNAME', 'pg_kvik_invalidate'
LANGUAGE C STRICT;

CREATE FUNCTION stats()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_kvik_stats'
LANGUAGE C STABLE;

REVOKE ALL ON FUNCTION _reload() FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate(text) FROM PUBLIC;

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
    v_key_not_null boolean;
    v_value_not_null boolean;
    v_old_relation oid;
    v_relkind "char";
    v_relpersistence "char";
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
    v_key_generated "char";
    v_key_identity "char";
    v_value_generated "char";
    v_value_identity "char";
BEGIN
    IF p_namespace !~ '^[A-Za-z0-9_.-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid pg_kvik namespace: %', p_namespace
            USING HINT = 'Use 1-63 ASCII letters, digits, dot, dash, or underscore.';
    END IF;

    IF p_key_column = p_value_column THEN
        RAISE EXCEPTION 'key and value columns must be different';
    END IF;

    SELECT c.relkind, c.relpersistence,
           c.relrowsecurity, c.relforcerowsecurity
      INTO v_relkind, v_relpersistence,
           v_relrowsecurity, v_relforcerowsecurity
      FROM pg_class AS c
     WHERE c.oid = p_relation;

    IF v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION 'pg_kvik alpha supports only permanent ordinary tables';
    END IF;

    IF v_relrowsecurity OR v_relforcerowsecurity THEN
        RAISE EXCEPTION 'row-level security is not supported by pg_kvik'
            USING HINT = 'Use a dedicated table or a security-barrier API instead.';
    END IF;

    SELECT a.atttypid, a.attnotnull, a.attgenerated, a.attidentity
      INTO v_key_type, v_key_not_null, v_key_generated, v_key_identity
      FROM pg_attribute AS a
     WHERE a.attrelid = p_relation
       AND a.attname = p_key_column
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'key column % does not exist on %',
            p_key_column, p_relation;
    END IF;

    SELECT a.attnotnull, a.attgenerated, a.attidentity
      INTO v_value_not_null, v_value_generated, v_value_identity
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

    IF NOT EXISTS (
        SELECT 1
          FROM pg_index AS i
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

    LOCK TABLE kvik.mapping IN EXCLUSIVE MODE;

    SELECT relation::oid
      INTO v_old_relation
      FROM kvik.mapping
     WHERE namespace = p_namespace;

    IF FOUND AND v_old_relation <> p_relation::oid THEN
        IF EXISTS (
            SELECT 1 FROM pg_class AS c WHERE c.oid = v_old_relation
        ) THEN
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_kvik_row_invalidate ON %s',
                v_old_relation::regclass
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS pg_kvik_truncate_invalidate ON %s',
                v_old_relation::regclass
            );
        END IF;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM kvik.mapping WHERE namespace = p_namespace
    ) AND (SELECT count(*) FROM kvik.mapping) >= 128 THEN
        RAISE EXCEPTION 'pg_kvik supports at most 128 mappings';
    END IF;

    INSERT INTO kvik.mapping(
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
        'DROP TRIGGER IF EXISTS pg_kvik_row_invalidate ON %s',
        p_relation
    );
    EXECUTE format(
        'CREATE TRIGGER pg_kvik_row_invalidate
           AFTER INSERT OR UPDATE OR DELETE ON %s
           FOR EACH ROW
           EXECUTE FUNCTION kvik._row_invalidate(%L, %L)',
        p_relation, p_namespace, p_key_column
    );
    EXECUTE format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_kvik_row_invalidate',
        p_relation
    );

    EXECUTE format(
        'DROP TRIGGER IF EXISTS pg_kvik_truncate_invalidate ON %s',
        p_relation
    );
    EXECUTE format(
        'CREATE TRIGGER pg_kvik_truncate_invalidate
           AFTER TRUNCATE ON %s
           FOR EACH STATEMENT
           EXECUTE FUNCTION kvik._truncate_invalidate(%L)',
        p_relation, p_namespace
    );
    EXECUTE format(
        'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_kvik_truncate_invalidate',
        p_relation
    );

    PERFORM kvik._reload();
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
    DELETE FROM kvik.mapping
     WHERE namespace = p_namespace
     RETURNING relation::oid INTO v_relation;

    IF FOUND AND EXISTS (
        SELECT 1 FROM pg_class AS c WHERE c.oid = v_relation
    ) THEN
        EXECUTE format(
            'DROP TRIGGER IF EXISTS pg_kvik_row_invalidate ON %s',
            v_relation::regclass
        );
        EXECUTE format(
            'DROP TRIGGER IF EXISTS pg_kvik_truncate_invalidate ON %s',
            v_relation::regclass
        );
    END IF;
    PERFORM kvik._reload();
END;
$function$;

REVOKE ALL ON FUNCTION register_mapping(text, regclass, name, name, boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION unregister_mapping(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION _row_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _truncate_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _ddl_invalidate() FROM PUBLIC;

CREATE EVENT TRIGGER pg_kvik_ddl_invalidate
    ON ddl_command_end
    EXECUTE FUNCTION kvik._ddl_invalidate();

SELECT kvik._reload();
