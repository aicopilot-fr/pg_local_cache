#!/usr/bin/env python3
"""Source contracts for the whole-row and explicit scalar SQL APIs."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SQL = ROOT / "sql" / "pg_local_cache--1.0.0.sql"
SQL = INSTALL_SQL.read_text(encoding="utf-8")
ATTACH_SCRIPT = (ROOT / "docker" / "attach-table.sh").read_text(
    encoding="utf-8"
)


def sql_function(name: str) -> str:
    match = re.search(
        rf"CREATE FUNCTION {re.escape(name)}\(.*?\n\$function\$;",
        SQL,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{name}() definition is missing")
    return match.group(0)


class WholeRowSchemaTests(unittest.TestCase):
    def test_mapping_distinguishes_whole_row_and_scalar_modes(self) -> None:
        mapping = SQL[SQL.index("CREATE TABLE mapping"):SQL.index(
            "REVOKE ALL ON TABLE mapping"
        )]
        self.assertIn("key_columns name[] NOT NULL", mapping)
        self.assertRegex(mapping, r"\n\s*value_column name,")
        self.assertNotIn("value_column name NOT NULL", mapping)
        self.assertIn("pg_catalog.array_ndims(key_columns) = 1", mapping)
        self.assertIn("pg_catalog.array_lower(key_columns, 1) = 1", mapping)
        self.assertIn(
            "pg_catalog.cardinality(key_columns) BETWEEN 1 AND 16", mapping
        )
        self.assertIn(
            "pg_catalog.array_position(key_columns, NULL::name) IS NULL",
            mapping,
        )
        self.assertNotIn("key_column name", mapping)
        self.assertIn("namespace <> 'CRUD'", mapping)
        expected_order = (
            "namespace text",
            "relation regclass",
            "key_columns name[]",
            "value_column name",
            "writable boolean",
        )
        offsets = [mapping.index(column) for column in expected_order]
        self.assertEqual(offsets, sorted(offsets))

    def test_mapping_is_dumped_and_direct_restore_changes_reload_workers(self) -> None:
        self.assertIn(
            "pg_extension_config_dump('local_cache.mapping', '')", SQL
        )
        self.assertRegex(
            SQL,
            r"CREATE TRIGGER pg_local_cache_mapping_reload\s+"
            r"AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON mapping",
        )
        changed = sql_function("_mapping_changed")
        self.assertIn("PERFORM local_cache._reload()", changed)
        self.assertIn("SECURITY DEFINER", changed)

    def test_attach_validates_table_shape_before_primary_key_lookup(self) -> None:
        validator = sql_function("_validate_attach_relation")
        for token in (
            "v_relkind <> 'r'",
            "v_relpersistence <> 'p'",
            "pg_catalog.pg_inherits",
            "v_relispartition",
            "v_relrowsecurity",
            "v_relforcerowsecurity",
        ):
            self.assertIn(token, validator)
        for function_name in ("attach_table", "attach_value"):
            attach = sql_function(function_name)
            self.assertLess(
                attach.index("local_cache._lock_relation(p_relation::oid)"),
                attach.index("local_cache._validate_attach_relation(p_relation)"),
            )
            self.assertLess(
                attach.index("local_cache._validate_attach_relation(p_relation)"),
                attach.index("local_cache._primary_key_columns(p_relation)"),
            )

    def test_default_attach_is_whole_row_and_has_the_small_api(self) -> None:
        attach = sql_function("attach_table")
        self.assertRegex(
            attach,
            r"p_relation regclass,\s*"
            r"p_writable boolean DEFAULT false,\s*"
            r"p_namespace text DEFAULT NULL",
        )
        self.assertNotIn("p_value_column", attach.partition("DECLARE")[0])
        self.assertIn("local_cache._primary_key_columns(p_relation)", attach)
        self.assertIn("local_cache._register_mapping(", attach)
        self.assertIn("NULL::name", attach)
        self.assertIn("RETURNS jsonb", attach)

    def test_scalar_mode_is_explicit_and_requires_one_primary_key(self) -> None:
        attach = sql_function("attach_value")
        self.assertRegex(
            attach,
            r"p_relation regclass,\s*"
            r"p_value_column name,\s*"
            r"p_namespace text DEFAULT NULL,\s*"
            r"p_writable boolean DEFAULT false",
        )
        self.assertIn("cardinality(v_key_columns) <> 1", attach)
        self.assertIn("scalar value column must not be NULL", attach)
        self.assertIn("ARRAY[v_key_columns[1]]::name[]", attach)
        self.assertIn("p_value_column, p_writable", attach)

    def test_detach_table_resolves_relation_and_is_idempotent(self) -> None:
        detach = sql_function("detach_table")
        self.assertIn("LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE", detach)
        self.assertIn("WHERE m.relation = p_relation", detach)
        self.assertIn("IF NOT FOUND THEN", detach)
        self.assertIn("RETURN false", detach)
        self.assertIn("local_cache.unregister_mapping(v_namespace)", detach)
        self.assertIn("RETURN true", detach)


class RegistrationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register = sql_function("_register_mapping")

    def test_exact_ordered_primary_key_is_required(self) -> None:
        for token in (
            "i.indisprimary",
            "WITH ORDINALITY",
            "key.key_position <= i.indnkeyatts",
            "ORDER BY key.key_position",
            "p_key_columns <> v_primary_key_columns",
            "every PRIMARY KEY column exactly once and in primary-key order",
            "BETWEEN 1 AND 16",
            "i.indisunique",
            "i.indimmediate",
            "i.indisvalid",
            "i.indisready",
            "i.indpred IS NULL",
            "i.indexprs IS NULL",
            "am.amname = 'btree'",
            "i.indclass[v_key_position - 1]",
            "opc.opcdefault",
            "v_key_attribute_count",
            "one or more key columns % do not exist on %",
        ):
            self.assertIn(token, self.register)

    def test_scalar_mode_has_no_non_pk_registration_backdoor(self) -> None:
        self.assertIn("i.indpred IS NULL", SQL)
        self.assertIn("i.indexprs IS NULL", SQL)
        self.assertIn("p_value_column IS NOT NULL AND v_primary_key_count <> 1", SQL)
        self.assertIn("AND i.indisprimary", self.register)
        self.assertNotIn("valid single-column UNIQUE index", SQL)

    def test_table_rls_collation_and_writable_rows_fail_closed(self) -> None:
        for token in (
            "c.relpersistence",
            "c.relispartition",
            "inh.inhparent = p_relation",
            "inh.inhrelid = p_relation",
            "c.relrowsecurity",
            "c.relforcerowsecurity",
            "v_schema_name::text ~ '^pg_'",
            "v_schema_name = 'information_schema'",
            "ext.extnamespace = v_schema_oid",
            "dep.deptype = 'e'",
            "pg_local_cache cannot attach extension or system table",
            "NOT coll.collisdeterministic",
            "writable whole-row mappings do not support generated primary keys",
            "a.attgenerated <> ''",
            "identity columns are supported",
        ):
            self.assertIn(token, self.register)

    def test_namespace_reassignment_requires_an_explicit_detach(self) -> None:
        self.assertIn("namespace % is already attached to table %", self.register)
        self.assertIn("Call detach_table() for the existing table first", self.register)
        conflict = self.register.index("v_old_relation <> p_relation::oid")
        insert = self.register.index("INSERT INTO local_cache.mapping")
        self.assertLess(conflict, insert)
        self.assertNotIn(
            "PERFORM local_cache._forget(p_namespace, v_old_relation)",
            self.register,
        )

    def test_database_and_wire_namespace_are_validated_before_side_effects(self) -> None:
        self.assertIn("p_namespace = 'CRUD'", self.register)
        self.assertIn("pg_local_cache.database", self.register)
        self.assertIn(
            "v_configured_database <> pg_catalog.current_database()",
            self.register,
        )
        self.assertLess(
            self.register.index("v_key_attribute_count"),
            self.register.index("GRANT USAGE ON SCHEMA %s TO %I"),
        )

    def test_registration_owns_least_privilege_and_mapping_limit(self) -> None:
        for role_attribute in (
            "rolcanlogin",
            "rolsuper",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        ):
            self.assertIn(role_attribute, self.register)
        self.assertIn("has_database_privilege", self.register)
        self.assertIn("has_schema_privilege", self.register)
        self.assertIn("has_table_privilege", self.register)
        self.assertIn("GRANT USAGE ON SCHEMA %s TO %I", self.register)
        self.assertIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO %I",
            self.register,
        )
        self.assertIn(
            "REVOKE INSERT, UPDATE, DELETE ON TABLE %s FROM %I",
            self.register,
        )
        self.assertIn("LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE", self.register)
        self.assertIn(">= 128", self.register)

    def test_exact_oid_lock_precedes_catalog_reads_mapping_and_acl(self) -> None:
        lock_at = self.register.index(
            "local_cache._lock_relation(p_relation::oid)"
        )
        catalog_at = self.register.index("FROM pg_catalog.pg_class AS c")
        mapping_at = self.register.index(
            "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
        )
        grant_at = self.register.index("GRANT USAGE ON SCHEMA %s TO %I")
        self.assertLess(lock_at, catalog_at)
        self.assertLess(catalog_at, mapping_at)
        self.assertLess(mapping_at, grant_at)
        self.assertIn("v_schema_oid::pg_catalog.regnamespace", self.register)
        self.assertIn("p_relation, v_worker_role", self.register)
        self.assertIn("has_schema_privilege(", self.register)
        self.assertIn("has_table_privilege(", self.register)
        self.assertIn("v_ready_trigger_count <> 3", self.register)

        detach = sql_function("detach_table")
        self.assertLess(
            detach.index("local_cache._lock_relation(p_relation::oid)"),
            detach.index("LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"),
        )

    def test_unregister_conditionally_deletes_the_locked_relation(self) -> None:
        unregister = sql_function("unregister_mapping")
        lookup = unregister.index("SELECT m.relation::oid")
        relation_lock = unregister.index("local_cache._lock_relation(v_relation)")
        mapping_lock = unregister.index(
            "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
        )
        conditional_delete = unregister.index("AND relation::oid = v_relation")
        self.assertLess(lookup, relation_lock)
        self.assertLess(relation_lock, mapping_lock)
        self.assertLess(mapping_lock, conditional_delete)
        self.assertIn("ERRCODE = '40001'", unregister)

    def test_reserved_trigger_names_are_never_blindly_removed(self) -> None:
        trigger_slots = sql_function("_prepare_trigger_slots")
        for trigger_function in (
            "local_cache._statement_guard()",
            "local_cache._row_invalidate()",
            "local_cache._truncate_invalidate()",
        ):
            self.assertIn(trigger_function, trigger_slots)
        for structural_check in (
            "t.tgisinternal",
            "t.tgconstraint = 0",
            "t.tgparentid = 0",
            "t.tgqual IS NULL",
            "t.tgtype = CASE",
            "t.tgnargs = CASE",
            "t.tgargs = CASE",
            "dep.deptype = 'x'",
        ):
            self.assertIn(structural_check, trigger_slots)
        self.assertIn("reserved pg_local_cache trigger name", trigger_slots)
        self.assertIn("local_cache._prepare_trigger_slots(", self.register)
        self.assertNotIn("DROP TRIGGER", self.register)
        self.assertIn("v_owned_trigger.tgname", trigger_slots)
        self.assertIn("NOT COALESCE((", trigger_slots)
        self.assertIn("DROP TRIGGER %I ON %s", trigger_slots)
        self.assertEqual(
            self.register.count("DEPENDS ON EXTENSION pg_local_cache"), 3
        )
        drop_owned = sql_function("_drop_owned_triggers")
        self.assertEqual(drop_owned.count("DROP TRIGGER "), 1)
        self.assertIn("t.tgfoid IN (", drop_owned)
        self.assertIn("v_owned_trigger.tgname", drop_owned)
        self.assertIn("dep.deptype = 'x'", drop_owned)
        self.assertNotIn("DROP TRIGGER IF EXISTS", drop_owned)

    def test_transactional_invalidators_receive_all_key_columns(self) -> None:
        self.assertIn(
            "FOREACH v_key_column IN ARRAY p_key_columns LOOP", self.register
        )
        self.assertIn(
            "v_trigger_arguments := pg_catalog.quote_literal(p_namespace)",
            self.register,
        )
        self.assertIn(
            "EXECUTE FUNCTION local_cache._row_invalidate(%s)", self.register
        )
        self.assertIn(
            "ENABLE ALWAYS TRIGGER pg_local_cache_statement_guard", self.register
        )
        self.assertIn(
            "ENABLE ALWAYS TRIGGER pg_local_cache_row_invalidate", self.register
        )
        self.assertIn(
            "ENABLE ALWAYS TRIGGER pg_local_cache_truncate_invalidate",
            self.register,
        )


class DeveloperApiTests(unittest.TestCase):
    def test_drop_table_forgets_and_deletes_the_orphan_mapping(self) -> None:
        drop = sql_function("_sql_drop_invalidate")
        delete_at = drop.index("DELETE FROM local_cache.mapping AS m")
        forget_at = drop.index("PERFORM local_cache._forget(")
        guard_at = drop.index("cardinality(v_dropped_relations) > 0")
        self.assertLess(guard_at, delete_at)
        self.assertLess(delete_at, forget_at)
        self.assertIn("pg_event_trigger_dropped_objects()", drop)
        self.assertIn("d.objid = m.relation::oid", drop)
        self.assertIn("d.object_type = 'table'", drop)
        ddl = sql_function("_ddl_invalidate")
        self.assertIn("'pg_catalog.pg_namespace'::pg_catalog.regclass", ddl)
        self.assertIn("schema_relation.relnamespace = d.objid", ddl)
        self.assertIn("'pg_catalog.pg_extension'::regclass", ddl)
        self.assertIn("d.objsubid = 0", drop)
        self.assertIn("to_regclass('local_cache.mapping')", drop)

    def test_reconcile_uses_stored_mapping_and_locks_relations_first(self) -> None:
        reconcile_table = sql_function("reconcile_table")
        self.assertLess(
            reconcile_table.index("local_cache._lock_relation(p_relation::oid)"),
            reconcile_table.index(
                "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
            ),
        )
        for field in ("m.namespace", "m.key_columns", "m.value_column", "m.writable"):
            self.assertIn(field, reconcile_table)
        self.assertIn("local_cache._register_mapping(", reconcile_table)
        self.assertIn("local_cache._mapping_result(", reconcile_table)

        reconcile_all = sql_function("reconcile_all")
        prelock = reconcile_all.index("FOREACH v_relation")
        mapping_lock = reconcile_all.index(
            "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
        )
        register = reconcile_all.index("local_cache._register_mapping(")
        self.assertLess(prelock, mapping_lock)
        self.assertLess(mapping_lock, register)
        self.assertIn("ORDER BY m.relation::oid", reconcile_all)
        self.assertIn("ERRCODE = '40001'", reconcile_all)

    def test_schema_qualified_default_namespace_has_oid_fallback(self) -> None:
        helper = sql_function("_default_namespace")
        self.assertIn("n.nspname || '.' || c.relname", helper)
        self.assertIn("'rel_' || c.oid::text", helper)
        self.assertIn("{1,63}", helper)

    def test_attach_result_contains_kvik_crud_templates(self) -> None:
        result = sql_function("_mapping_result")
        for field in (
            "'primary_key_columns'",
            "'whole_row'",
            "'value_column'",
            "'templates'",
            "'key'",
            "'get'",
            "'set'",
            "'del'",
            "'invalidate'",
        ):
            self.assertIn(field, result)
        self.assertIn("'CRUD:' || v_wire_relation", result)
        self.assertIn("'%s:<%s>', p_namespace, p_key_columns[1]", result)
        self.assertIn("ELSE 'INVALIDATE ' || p_namespace", result)
        self.assertIn("ORDER BY key_position", result)
        self.assertIn("' <row-json>'", result)
        self.assertIn("' <value>'", result)
        self.assertNotIn("'primary_key_column'", result)

    def test_all_administrative_functions_are_closed_to_public(self) -> None:
        signatures = (
            "_lock_relation(oid)",
            "_validate_attach_relation(regclass)",
            "_primary_key_columns(regclass)",
            "_mapping_changed()",
            "_default_namespace(regclass)",
            "_mapping_result(text, regclass, name[], name, boolean)",
            "_prepare_trigger_slots(oid, text, name[])",
            "_register_mapping(text, regclass, name[], name, boolean)",
            "_drop_owned_triggers(oid)",
            "unregister_mapping(text)",
            "detach_table(regclass)",
            "attach_table(regclass, boolean, text)",
            "attach_value(regclass, name, text, boolean)",
            "reconcile_table(regclass)",
            "reconcile_all()",
        )
        for signature in signatures:
            self.assertRegex(
                SQL,
                rf"REVOKE ALL ON FUNCTION {re.escape(signature)}\s+FROM PUBLIC;",
            )
        for function_name in (
            "_register_mapping",
            "unregister_mapping",
            "detach_table",
            "attach_table",
            "attach_value",
            "reconcile_table",
            "reconcile_all",
        ):
            function = sql_function(function_name)
            self.assertIn("SECURITY DEFINER", function)
            self.assertIn("SET search_path = pg_catalog, pg_temp", function)

    def test_docker_command_defaults_to_whole_row(self) -> None:
        self.assertIn("Cache one scalar column instead of the whole-row JSON", ATTACH_SCRIPT)
        self.assertIn('scalar_mode="false"', ATTACH_SCRIPT)
        self.assertIn("SELECT local_cache.attach_table(", ATTACH_SCRIPT)
        self.assertIn("SELECT local_cache.attach_value(", ATTACH_SCRIPT)
        self.assertNotIn("exactly one non-PK column", ATTACH_SCRIPT)

    def test_docker_command_serializes_and_rechecks_remaps(self) -> None:
        self.assertIn("BEGIN ISOLATION LEVEL READ COMMITTED;", ATTACH_SCRIPT)
        self.assertIn("local_cache._lock_relation(", ATTACH_SCRIPT)
        sorted_lock = ATTACH_SCRIPT.index("ORDER BY candidate.relation_oid")
        mapping_lock = ATTACH_SCRIPT.index(
            "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
        )
        self.assertLess(sorted_lock, mapping_lock)
        self.assertIn("pg_local_cache_attach_conflicts", ATTACH_SCRIPT)
        self.assertIn("changed_conflicts AS (", ATTACH_SCRIPT)
        self.assertIn("mapping_snapshot_stable", ATTACH_SCRIPT)
        self.assertIn("target.relation_oid::text AS target_relation_oid", ATTACH_SCRIPT)
        self.assertIn("ROLLBACK;", ATTACH_SCRIPT)
        self.assertIn(
            "RAISE EXCEPTION\n"
            "        'pg_local_cache attach: namespace/table mapping is occupied; "
            "pass --replace to remap it'",
            ATTACH_SCRIPT,
        )
        self.assertNotRegex(ATTACH_SCRIPT, r"\\quit\\s+\\d+")


class BaselinePackagingTests(unittest.TestCase):
    def test_legacy_surface_is_absent(self) -> None:
        for token in (
            "_attach_table_1_0_compat",
            "mapping_key_column_projection",
            "CREATE FUNCTION register_mapping(",
            "CREATE FUNCTION register_value_mapping(",
            "CREATE FUNCTION mapping_metrics(",
        ):
            self.assertNotIn(token, SQL)

    def test_default_and_packaged_version_is_one_clean_baseline(self) -> None:
        control = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("default_version = '1.0.0'", control)
        self.assertIn("pg_local_cache--1.0.0.sql", makefile)
        self.assertIn("pg_local_cache--1.0.0.sql", dockerfile)
        self.assertNotIn("--1.1.0", makefile + dockerfile)
        self.assertNotIn("--1.0.0--", makefile + dockerfile)


if __name__ == "__main__":
    unittest.main()
