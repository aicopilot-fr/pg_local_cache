#!/usr/bin/env python3
"""Live extension-upgrade smoke test for 1.0.0 -> 1.1.0."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")
WORKER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_ROLE", "")


class RespError(RuntimeError):
    pass


class RespClient:
    def __init__(self) -> None:
        self.socket = socket.create_connection((RESP_HOST, RESP_PORT), timeout=5)
        self.stream = self.socket.makefile("rb")
        assert self.command("AUTH", AUTH_TOKEN) == "OK"

    def close(self) -> None:
        self.stream.close()
        self.socket.close()

    def command(self, *arguments: object) -> object:
        encoded = [str(argument).encode() for argument in arguments]
        request = [f"*{len(encoded)}\r\n".encode()]
        for argument in encoded:
            request.extend(
                (f"${len(argument)}\r\n".encode(), argument, b"\r\n")
            )
        self.socket.sendall(b"".join(request))
        return self._read_response()

    def _read_response(self) -> object:
        prefix = self.stream.read(1)
        if not prefix:
            raise EOFError("RESP connection closed")
        line = self.stream.readline()
        if not line.endswith(b"\r\n"):
            raise ValueError("invalid RESP response")
        if prefix == b"-":
            raise RespError(line[:-2].decode())
        if prefix == b"+":
            return line[:-2].decode()
        if prefix == b":":
            return int(line[:-2])
        if prefix == b"$":
            length = int(line[:-2])
            if length == -1:
                return None
            value = self.stream.read(length)
            if self.stream.read(2) != b"\r\n":
                raise ValueError("truncated RESP response")
            return value.decode()
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def sql(statement: str) -> str:
    try:
        return subprocess.check_output(
            [
                PSQL,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                PGHOST,
                "-p",
                PGPORT,
                "-d",
                PGDATABASE,
                "-Atq",
                "-c",
                statement,
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=45,
        ).strip()
    except subprocess.CalledProcessError as error:
        if error.output:
            print(error.output, end="" if error.output.endswith("\n") else "\n")
        raise


def sql_json(statement: str) -> dict[str, object]:
    """Run an administrative statement without NOTICE lines corrupting JSON."""
    output = sql("SET client_min_messages = warning;" + statement)
    if not output:
        raise AssertionError("SQL JSON statement returned no rows")
    candidate = output.splitlines()[-1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise AssertionError(f"invalid SQL JSON output: {output!r}") from error
    if not isinstance(value, dict):
        raise AssertionError(f"SQL JSON statement returned {value!r}")
    return value


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def wait_get(client: RespClient, key: str, expected: str) -> None:
    deadline = time.monotonic() + 10
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = client.command("GET", key)
            if last == expected:
                return
        except RespError as error:
            last = error
        time.sleep(0.02)
    raise AssertionError(f"GET {key!r} did not become {expected!r}: {last!r}")


def wait_row(client: RespClient, key: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = client.command("GET", key)
            if isinstance(last, str):
                value = json.loads(last)
                if isinstance(value, dict):
                    return value
        except RespError as error:
            last = error
        time.sleep(0.02)
    raise AssertionError(f"whole-row GET did not become ready: {last!r}")


def main() -> None:
    suffix = str(os.getpid())
    scalar_table = f"pglc_upgrade_scalar_{suffix}"
    row_table = f"pglc_upgrade_row_{suffix}"
    compatibility_view = f"pglc_upgrade_mapping_view_{suffix}"
    attach_dependency_view = f"pglc_upgrade_attach_view_{suffix}"
    monitor_role = f"pglc_upgrade_monitor_{suffix}"
    scalar_namespace = f"upgrade{suffix}"
    row_namespace = f"public.{row_table}"
    worker = sql_identifier(WORKER_ROLE)

    # Exercise the actual packaged SQL scripts, not just source parsing.
    sql(
        "DROP EXTENSION IF EXISTS pg_local_cache CASCADE;"
        "CREATE EXTENSION pg_local_cache VERSION '1.0.0';"
        f"GRANT USAGE ON SCHEMA local_cache TO {worker};"
        f"GRANT SELECT ON TABLE local_cache.mapping TO {worker};"
        f"CREATE TABLE public.{scalar_table} ("
        "id bigint NOT NULL UNIQUE, value text NOT NULL);"
        f"INSERT INTO public.{scalar_table} VALUES (1, 'before-upgrade');"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{scalar_table} TO {worker};"
        "SELECT local_cache.register_mapping("
        f"'{scalar_namespace}', 'public.{scalar_table}', "
        "'id', 'value', true);"
        f"CREATE VIEW public.{compatibility_view} AS "
        "SELECT namespace, key_column FROM local_cache.mapping;"
        f"CREATE VIEW public.{attach_dependency_view} AS "
        "SELECT local_cache.attach_table("
        f"'public.{scalar_table}'::regclass, 'value'::name, "
        "'upgrade-dependency'::text, false) AS result;"
        "COMMENT ON COLUMN local_cache.mapping.key_column IS "
        "'pg_local_cache 1.0 compatibility column';"
        f"CREATE ROLE {sql_identifier(monitor_role)} NOLOGIN NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;"
        f"GRANT USAGE ON SCHEMA local_cache TO {sql_identifier(monitor_role)};"
        "GRANT EXECUTE ON FUNCTION local_cache.metrics(), "
        "local_cache.health(), local_cache.stats() "
        f"TO {sql_identifier(monitor_role)};"
        "GRANT EXECUTE ON FUNCTION "
        "local_cache.attach_table(regclass, name, text, boolean) "
        f"TO {sql_identifier(monitor_role)} WITH GRANT OPTION"
    )
    legacy_attach_oid = sql(
        "SELECT "
        "'local_cache.attach_table(regclass,name,text,boolean)'::regprocedure::oid"
    )
    assert sql(
        "SELECT extversion FROM pg_catalog.pg_extension "
        "WHERE extname = 'pg_local_cache'"
    ) == "1.0.0"

    client = RespClient()
    try:
        wait_get(client, f"{scalar_namespace}:1", "before-upgrade")
        sql("ALTER EXTENSION pg_local_cache UPDATE TO '1.1.0'")
        assert sql(
            "SELECT extversion FROM pg_catalog.pg_extension "
            "WHERE extname = 'pg_local_cache'"
        ) == "1.1.0"

        mapping_columns = sql(
            "SELECT pg_catalog.string_agg(a.attname, ',' ORDER BY a.attnum) "
            "FROM pg_catalog.pg_attribute AS a "
            "WHERE a.attrelid = 'local_cache.mapping'::regclass "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        )
        assert mapping_columns == (
            "namespace,relation,key_column,value_column,writable,key_columns"
        ), mapping_columns
        assert sql(
            "SELECT p.proname FROM pg_catalog.pg_proc AS p "
            f"WHERE p.oid = {legacy_attach_oid}::oid"
        ) == "_attach_table_1_0_compat"
        assert "_attach_table_1_0_compat" in sql(
            "SELECT pg_catalog.pg_get_viewdef("
            f"'public.{attach_dependency_view}'::regclass, true)"
        )
        attach_grants = sql(
            "SELECT pg_catalog.has_function_privilege("
            f"{monitor_role!r}, "
            "'local_cache._attach_table_1_0_compat(regclass,name,text,boolean)', "
            "'EXECUTE'), "
            "pg_catalog.has_function_privilege("
            f"{monitor_role!r}, "
            "'local_cache.attach_table(regclass,name,text,boolean)', "
            "'EXECUTE')"
        )
        assert attach_grants == "t|t", attach_grants
        assert sql(
            "SELECT acl.is_grantable "
            "FROM pg_catalog.pg_proc AS p "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) AS acl "
            "JOIN pg_catalog.pg_roles AS r ON r.oid = acl.grantee "
            "WHERE p.oid = "
            "'local_cache.attach_table(regclass,name,text,boolean)'::regprocedure "
            f"  AND r.rolname = {monitor_role!r} "
            "  AND acl.privilege_type = 'EXECUTE'"
        ) == "t"

        migrated = sql(
            "SELECT key_columns::text, key_column::text, "
            "value_column::text, writable "
            "FROM local_cache.mapping "
            f"WHERE namespace = '{scalar_namespace}'"
        )
        assert migrated == "{id}|id|value|t", migrated
        assert sql(
            f"SELECT key_column FROM public.{compatibility_view} "
            f"WHERE namespace = '{scalar_namespace}'"
        ) == "id"
        assert sql(
            "SELECT pg_catalog.col_description('local_cache.mapping'::regclass, "
            "       a.attnum) "
            "FROM pg_catalog.pg_attribute AS a "
            "WHERE a.attrelid = 'local_cache.mapping'::regclass "
            "  AND a.attname = 'key_column'"
        ) == "pg_local_cache 1.0 compatibility column"
        deadline = time.monotonic() + 10
        while True:
            upgraded_monitor = sql(
                f"SET ROLE {sql_identifier(monitor_role)};"
                "SELECT (h.payload ->> 'workers_with_incomplete_mappings')::bigint, "
                "       (h.payload ->> 'mapping_reload_incomplete_retries_total')::bigint "
                "FROM local_cache.metrics() AS m "
                "CROSS JOIN LATERAL (SELECT local_cache.health() AS payload) AS h;"
                "RESET ROLE"
            ).splitlines()[0]
            if upgraded_monitor == "0|0":
                break
            if time.monotonic() >= deadline:
                raise AssertionError(upgraded_monitor)
            time.sleep(0.05)
        assert sql(
            "SELECT pg_catalog.has_function_privilege("
            f"{monitor_role!r}, 'local_cache.mapping_metrics()', 'EXECUTE')"
        ) == "f"
        sql(
            f"UPDATE public.{scalar_table} SET value = 'migrated-live' "
            "WHERE id = 1"
        )
        wait_get(client, f"{scalar_namespace}:1", "migrated-live")

        # The original function OID keeps the 1.0 two-argument scalar call;
        # it must not silently resolve as a whole-row namespace argument.
        sql(f"SELECT local_cache.unregister_mapping('{scalar_namespace}')")
        sql(f"ALTER TABLE public.{scalar_table} ADD PRIMARY KEY (id)")
        dependency_attached = sql_json(
            f"SELECT result FROM public.{attach_dependency_view}"
        )
        assert dependency_attached["whole_row"] is False, dependency_attached
        assert dependency_attached["value_column"] == "value", dependency_attached
        sql("SELECT local_cache.unregister_mapping('upgrade-dependency')")
        inferred_attached = sql_json(
            f"SELECT local_cache.attach_table("
            f"'public.{scalar_table}'::regclass, NULL, "
            f"'inferred-{suffix}')"
        )
        assert inferred_attached["whole_row"] is False, inferred_attached
        assert inferred_attached["value_column"] == "value", inferred_attached
        sql(f"SELECT local_cache.unregister_mapping('inferred-{suffix}')")
        legacy_attached = sql_json(
            f"SELECT local_cache.attach_table("
            f"'public.{scalar_table}'::regclass, 'value')"
        )
        assert legacy_attached["whole_row"] is False, legacy_attached
        assert legacy_attached["value_column"] == "value", legacy_attached
        assert legacy_attached["templates"]["get"] == (
            f"GET public.{scalar_table}:<id>"
        ), legacy_attached
        scalar_namespace = f"public.{scalar_table}"
        sql(
            f"UPDATE public.{scalar_table} SET value = 'after-upgrade' "
            "WHERE id = 1"
        )
        wait_get(client, f"{scalar_namespace}:1", "after-upgrade")

        # The exact one-argument overload must resolve to the new whole-row API
        # while the migrated scalar call shape remains available.
        attached = sql_json(
            f"CREATE TABLE public.{row_table} ("
            "tenant_id bigint NOT NULL, id bigint NOT NULL, value text, "
            "PRIMARY KEY (tenant_id, id));"
            f"INSERT INTO public.{row_table} VALUES (4, 5, 'whole-row');"
            f"SELECT local_cache.attach_table("
            f"'public.{row_table}'::regclass)"
        )
        assert attached["whole_row"] is True, attached
        assert attached["primary_key_columns"] == ["tenant_id", "id"], attached
        row_key = (
            f"CRUD:{PGDATABASE}.public.{row_table}:"
            '{"id":5,"tenant_id":4}'
        )
        row = wait_row(client, row_key)
        assert row == {"tenant_id": 4, "id": 5, "value": "whole-row"}, row
        wait_get(client, f"{scalar_namespace}:1", "after-upgrade")
    finally:
        client.close()
        sql(
            f"SELECT local_cache.unregister_mapping('{scalar_namespace}');"
            f"SELECT local_cache.unregister_mapping('{row_namespace}');"
            f"DROP VIEW IF EXISTS public.{attach_dependency_view}, "
            f"public.{compatibility_view};"
            f"DROP TABLE IF EXISTS public.{scalar_table}, public.{row_table};"
            f"DROP OWNED BY {sql_identifier(monitor_role)};"
            f"DROP ROLE IF EXISTS {sql_identifier(monitor_role)}"
        )

    print("live 1.0.0 -> 1.1.0 upgrade test passed")


if __name__ == "__main__":
    main()
