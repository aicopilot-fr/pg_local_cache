#!/usr/bin/env python3
"""Source contracts for the whole-row and legacy scalar SQL APIs."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SQL = ROOT / "sql" / "pg_local_cache--1.1.0.sql"
UPGRADE_SQL = ROOT / "sql" / "pg_local_cache--1.0.0--1.1.0.sql"
SQL = INSTALL_SQL.read_text(encoding="utf-8")
UPGRADE = UPGRADE_SQL.read_text(encoding="utf-8")
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
        self.assertIn("mapping_key_column_projection", mapping)
        self.assertNotIn("key_column name GENERATED ALWAYS", mapping)
        self.assertIn("namespace <> 'CRUD'", mapping)
        expected_order = (
            "namespace text",
            "relation regclass",
            "key_column name",
            "value_column name",
            "writable boolean",
            "key_columns name[]",
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
                attach.index("local_cache._validate_attach_relation(p_relation)"),
                attach.index("local_cache._primary_key_columns(p_relation)"),
            )

    def test_default_attach_is_whole_row_and_has_the_small_api(self) -> None:
        attach = sql_function("attach_table")
        self.assertRegex(
            attach,
            r"p_relation regclass,\s*"
            r"p_row_writable boolean,\s*"
            r"p_row_namespace text DEFAULT NULL",
        )
        self.assertNotIn("p_value_column", attach.partition("DECLARE")[0])
        self.assertIn("local_cache._primary_key_columns(p_relation)", attach)
        self.assertIn("local_cache.register_mapping(", attach)
        self.assertIn("NULL::name", attach)
        self.assertIn("RETURNS jsonb", attach)
        self.assertIsNotNone(
            re.search(
                r"CREATE FUNCTION attach_table\(\s*p_relation regclass\s*\).*?"
                r"SELECT local_cache\.attach_table\(p_relation, false, NULL::text\);",
                SQL,
                flags=re.DOTALL,
            )
        )

    def test_legacy_attach_overload_preserves_two_to_four_arguments(self) -> None:
        legacy = re.search(
            r"CREATE FUNCTION attach_table\(\s*"
            r"p_relation regclass,\s*p_value_column name,\s*"
            r"p_namespace text DEFAULT NULL,\s*"
            r"p_writable boolean DEFAULT false\s*\).*?\n\$function\$;",
            SQL,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(legacy)
        self.assertIn("local_cache.attach_value(", legacy.group(0))

    def test_private_legacy_anchor_exists_for_dump_restore_parity(self) -> None:
        hidden = sql_function("_attach_table_1_0_compat")
        self.assertRegex(
            hidden,
            r"p_relation regclass,\s*"
            r"p_value_column name DEFAULT NULL,\s*"
            r"p_namespace text DEFAULT NULL,\s*"
            r"p_writable boolean DEFAULT false",
        )
        self.assertIn("local_cache.attach_value(", hidden)

    def test_scalar_mode_is_explicit_and_one_key_compatible(self) -> None:
        attach = sql_function("attach_value")
        register = sql_function("register_value_mapping")
        self.assertRegex(
            attach,
            r"p_relation regclass,\s*"
            r"p_value_column name,\s*"
            r"p_namespace text DEFAULT NULL,\s*"
            r"p_writable boolean DEFAULT false",
        )
        self.assertIn("cardinality(v_key_columns) <> 1", attach)
        self.assertIn("IF p_value_column IS NULL", attach)
        self.assertIn("v_value_candidates <> 1", attach)
        self.assertIn("a.attname <> v_key_columns[1]", attach)
        self.assertIn("v_value_column, p_writable", attach)
        self.assertIn("ARRAY[p_key_column]::name[]", register)
        self.assertIn("p_value_column", register)


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

    def test_scalar_index_shape_keeps_the_legacy_actionable_error(self) -> None:
        for source in (SQL, UPGRADE):
            self.assertIn("i.indpred IS NULL", source)
            self.assertIn("i.indexprs IS NULL", source)
            self.assertIn("IF p_value_column IS NOT NULL THEN", source)
            self.assertIn(
                "key column %.% needs a valid single-column UNIQUE index",
                source,
            )

    def test_table_rls_collation_and_writable_rows_fail_closed(self) -> None:
        for token in (
            "c.relpersistence",
            "c.relispartition",
            "inh.inhparent = p_relation",
            "inh.inhrelid = p_relation",
            "c.relrowsecurity",
            "c.relforcerowsecurity",
            "NOT coll.collisdeterministic",
            "writable whole-row mappings do not support generated primary keys",
            "a.attgenerated <> ''",
            "identity columns are supported",
        ):
            self.assertIn(token, self.register)

    def test_namespace_reassignment_requires_an_explicit_unregister(self) -> None:
        self.assertIn("namespace % is already attached to table %", self.register)
        self.assertIn("Call unregister_mapping() first", self.register)
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
            self.register.index("GRANT USAGE ON SCHEMA %I TO %I"),
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
        self.assertIn("GRANT USAGE ON SCHEMA %I TO %I", self.register)
        self.assertIn(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I",
            self.register,
        )
        self.assertIn(
            "REVOKE INSERT, UPDATE, DELETE ON TABLE %I.%I FROM %I",
            self.register,
        )
        self.assertIn("LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE", self.register)
        self.assertIn(">= 128", self.register)

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

    def test_all_administrative_functions_are_closed_to_public(self) -> None:
        signatures = (
            "_validate_attach_relation(regclass)",
            "_primary_key_columns(regclass)",
            "_mapping_changed()",
            "_default_namespace(regclass)",
            "_mapping_result(text, regclass, name[], name, boolean)",
            "_register_mapping(text, regclass, name[], name, boolean)",
            "register_mapping(text, regclass, name[], boolean)",
            "unregister_mapping(text)",
            "attach_table(regclass)",
            "attach_table(regclass, boolean, text)",
            "attach_value(regclass, name, text, boolean)",
        )
        for signature in signatures:
            self.assertRegex(
                SQL,
                rf"REVOKE ALL ON FUNCTION {re.escape(signature)}\s+FROM PUBLIC;",
            )
        self.assertRegex(
            SQL,
            r"REVOKE ALL ON FUNCTION register_value_mapping\(\s*"
            r"text, regclass, name, name, boolean\s*\) FROM PUBLIC;",
        )
        self.assertRegex(
            SQL,
            r"REVOKE ALL ON FUNCTION _attach_table_1_0_compat\(\s*"
            r"regclass, name, text, boolean\s*\) FROM PUBLIC;",
        )
        for function_name in (
            "_register_mapping",
            "register_mapping",
            "register_value_mapping",
            "unregister_mapping",
            "attach_table",
            "attach_value",
            "_attach_table_1_0_compat",
        ):
            function = sql_function(function_name)
            self.assertIn("SECURITY DEFINER", function)
            self.assertIn("SET search_path = pg_catalog, pg_temp", function)

    def test_docker_command_defaults_to_whole_row(self) -> None:
        self.assertIn("legacy scalar-value mode instead of whole-row JSON", ATTACH_SCRIPT)
        self.assertIn('scalar_mode="false"', ATTACH_SCRIPT)
        self.assertIn("SELECT local_cache.attach_table(", ATTACH_SCRIPT)
        self.assertIn("SELECT local_cache.attach_value(", ATTACH_SCRIPT)
        self.assertNotIn("exactly one non-PK column", ATTACH_SCRIPT)

    def test_docker_command_rejects_implicit_remaps_with_nonzero_status(self) -> None:
        self.assertIn("ROLLBACK;", ATTACH_SCRIPT)
        self.assertIn(
            "RAISE EXCEPTION\n"
            "        'pg_local_cache attach: namespace/table mapping is occupied; "
            "pass --replace to remap it'",
            ATTACH_SCRIPT,
        )
        self.assertNotRegex(ATTACH_SCRIPT, r"\\quit\\s+\\d+")


class UpgradeAndPackagingTests(unittest.TestCase):
    def test_upgrade_preserves_scalar_rows_in_value_mode(self) -> None:
        self.assertIn("ADD COLUMN key_columns name[]", UPGRADE)
        self.assertIn("SET key_columns = ARRAY[key_column]::name[]", UPGRADE)
        self.assertIn("ALTER COLUMN key_columns SET NOT NULL", UPGRADE)
        self.assertIn("ALTER COLUMN value_column DROP NOT NULL", UPGRADE)
        self.assertIn("ALTER COLUMN key_column DROP NOT NULL", UPGRADE)
        self.assertNotIn("DROP COLUMN key_column", UPGRADE)
        self.assertIn("mapping_key_column_projection", UPGRADE)
        self.assertIn("CREATE FUNCTION attach_value(", UPGRADE)
        self.assertIn("CREATE FUNCTION register_value_mapping(", UPGRADE)
        self.assertIn("pg_extension_config_dump('local_cache.mapping', '')", UPGRADE)
        self.assertIn("CREATE TRIGGER pg_local_cache_mapping_reload", UPGRADE)
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION attach_table(", UPGRADE
        )
        rename = UPGRADE.index(
            "ALTER FUNCTION local_cache.attach_table(regclass, name, text, boolean)"
        )
        whole_options = UPGRADE.index(
            "CREATE FUNCTION attach_table(\n"
            "    p_relation regclass,\n"
            "    p_row_writable boolean,"
        )
        self.assertIn("RENAME TO _attach_table_1_0_compat", UPGRADE)
        self.assertIn(
            "CREATE OR REPLACE FUNCTION _attach_table_1_0_compat(",
            UPGRADE,
        )
        legacy_attach = UPGRADE.index(
            "CREATE FUNCTION attach_table(\n"
            "    p_relation regclass,\n"
            "    p_value_column name,"
        )
        whole_row_attach = UPGRADE.index(
            "CREATE FUNCTION attach_table(p_relation regclass)"
        )
        self.assertLess(rename, whole_options)
        self.assertLess(rename, legacy_attach)
        self.assertLess(legacy_attach, whole_row_attach)
        self.assertIn("pg_catalog.aclexplode", UPGRADE)
        self.assertIn("acl.is_grantable", UPGRADE)
        self.assertIn("TO %I%s", UPGRADE)
        self.assertNotIn("acl.grantee <> p.proowner", UPGRADE)
        self.assertRegex(
            UPGRADE,
            r"REVOKE ALL ON FUNCTION _attach_table_1_0_compat\(\s*"
            r"regclass, name, text, boolean\s*\) FROM PUBLIC;",
        )
        self.assertTrue(UPGRADE.rstrip().endswith("SELECT local_cache._reload();"))

    def test_whole_row_named_options_cannot_capture_legacy_scalar_names(self) -> None:
        for script in (SQL, UPGRADE):
            whole = re.search(
                r"CREATE FUNCTION attach_table\(\s*"
                r"p_relation regclass,\s*p_row_writable boolean,\s*"
                r"p_row_namespace text DEFAULT NULL\s*\).*?\n\$function\$;",
                script,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(whole)
            signature = whole.group(0).partition("RETURNS")[0]
            self.assertNotIn("p_writable boolean", signature)
            self.assertNotIn("p_namespace text", signature)

    def test_upgrade_rejects_the_newly_reserved_crud_namespace_cleanly(self) -> None:
        self.assertIn("legacy namespace CRUD exists", UPGRADE)
        self.assertIn("Unregister that 1.0 mapping", UPGRADE)
        self.assertLess(
            UPGRADE.index("legacy namespace CRUD exists"),
            UPGRADE.index("ADD COLUMN key_columns name[]"),
        )
        self.assertLess(
            UPGRADE.index(
                "LOCK TABLE local_cache.mapping IN ACCESS EXCLUSIVE MODE"
            ),
            UPGRADE.index("legacy namespace CRUD exists"),
        )

    def test_fresh_and_upgraded_mapping_have_identical_physical_order(self) -> None:
        expected = (
            "namespace",
            "relation",
            "key_column",
            "value_column",
            "writable",
            "key_columns",
        )
        fresh = SQL[SQL.index("CREATE TABLE mapping"):SQL.index(
            "REVOKE ALL ON TABLE mapping"
        )]
        fresh_offsets = [fresh.index(f"{name} ") for name in expected]
        self.assertEqual(fresh_offsets, sorted(fresh_offsets))
        self.assertIn("ADD COLUMN key_columns name[]", UPGRADE)

    def test_default_and_packaged_versions_are_1_1(self) -> None:
        control = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("default_version = '1.1.0'", control)
        for filename in (
            "pg_local_cache--1.0.0.sql",
            "pg_local_cache--1.0.0--1.1.0.sql",
            "pg_local_cache--1.1.0.sql",
        ):
            self.assertIn(filename, makefile)
            self.assertIn(filename, dockerfile)


if __name__ == "__main__":
    unittest.main()
