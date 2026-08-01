#!/usr/bin/env python3
"""Source-level safety contract for the administrative SQL attach API."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SQL = ROOT / "sql" / "pg_local_cache--1.0.0.sql"
SQL = INSTALL_SQL.read_text(encoding="utf-8")


def attach_function() -> str:
    match = re.search(
        r"CREATE FUNCTION attach_table\(.*?\n\$function\$;",
        SQL,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("attach_table() definition is missing")
    return match.group(0)


class AttachTableSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.function = attach_function()

    def test_public_signature_and_structured_result_are_stable(self) -> None:
        self.assertRegex(
            self.function,
            r"p_relation regclass,\s*"
            r"p_value_column name DEFAULT NULL,\s*"
            r"p_namespace text DEFAULT NULL,\s*"
            r"p_writable boolean DEFAULT false",
        )
        self.assertIn("RETURNS jsonb", self.function)
        for field in (
            "'relation'",
            "'namespace'",
            "'primary_key_column'",
            "'value_column'",
            "'writable'",
            "'worker_role'",
            "'templates'",
            "'key'",
            "'get'",
            "'set'",
            "'del'",
        ):
            self.assertIn(field, self.function)

    def test_security_definer_has_a_pinned_safe_search_path(self) -> None:
        self.assertIn("SECURITY DEFINER", self.function)
        self.assertIn(
            "SET search_path = pg_catalog, pg_temp",
            self.function,
        )
        self.assertIn(
            "REVOKE ALL ON FUNCTION "
            "attach_table(regclass, name, text, boolean) FROM PUBLIC;",
            SQL,
        )

    def test_primary_key_is_discovered_and_composite_keys_fail_closed(self) -> None:
        self.assertIn("i.indisprimary", self.function)
        self.assertIn("i.indnkeyatts", self.function)
        self.assertIn("i.indkey[0]", self.function)
        self.assertIn("has no primary key", self.function)
        self.assertIn("has a composite primary key", self.function)
        self.assertIn(
            "Composite primary keys are not supported yet",
            self.function,
        )
        self.assertIn("v_value_candidates", self.function)
        self.assertIn("exactly one non-primary-key column", self.function)

    def test_inheritance_children_and_partitions_fail_closed(self) -> None:
        self.assertIn("c.relispartition", self.function)
        self.assertIn("v_relispartition OR EXISTS", self.function)
        self.assertIn("inh.inhparent = p_relation", self.function)
        self.assertIn("inh.inhrelid = p_relation", self.function)
        self.assertIn("standalone table", self.function)

    def test_worker_role_must_exist_and_must_not_be_superuser(self) -> None:
        self.assertIn(
            "pg_catalog.current_setting(\n"
            "        'pg_local_cache.role', true",
            self.function,
        )
        self.assertIn("FROM pg_catalog.pg_roles AS r", self.function)
        self.assertIn("SELECT r.rolsuper", self.function)
        self.assertIn("IF v_worker_is_superuser THEN", self.function)
        self.assertIn("must not be a superuser", self.function)
        for attribute in (
            "rolcanlogin",
            "rolinherit",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        ):
            self.assertIn(attribute, self.function)
        self.assertIn("has_database_privilege", self.function)
        self.assertIn("has_schema_privilege", self.function)
        self.assertIn("has_table_privilege", self.function)

    def test_grants_are_identifier_quoted_and_writes_are_opt_in(self) -> None:
        self.assertIn("'GRANT USAGE ON SCHEMA %I TO %I'", self.function)
        self.assertIn("'GRANT SELECT ON TABLE %I.%I TO %I'", self.function)
        self.assertIn(
            "'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I'",
            self.function,
        )
        self.assertIn("CASE WHEN p_writable", self.function)
        self.assertNotRegex(
            self.function,
            r"EXECUTE\s+['\"].*\|\|",
        )

    def test_registration_and_conflict_check_share_one_lock(self) -> None:
        lock_at = self.function.index(
            "LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE"
        )
        grant_at = self.function.index("GRANT USAGE ON SCHEMA")
        register_at = self.function.index("local_cache.register_mapping(")
        self.assertLess(lock_at, grant_at)
        self.assertLess(grant_at, register_at)
        self.assertIn("m.relation <> p_relation", self.function)
        self.assertIn("m.namespace <> v_namespace", self.function)


if __name__ == "__main__":
    unittest.main()
