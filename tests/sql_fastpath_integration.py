#!/usr/bin/env python3
"""Black-box integration tests for the transparent SQL cache fast path.

The administrative connection is supplied by the Docker smoke-test psql
wrapper.  Every query whose result could be accelerated is executed through a
real LOGIN NOSUPERUSER role over a password-authenticated libpq connection;
using SET ROLE here would miss PostgreSQL's connection and ACL boundary.
"""

from __future__ import annotations

import json
import os
import re
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

# Prefer purpose-specific names, but accept the existing Docker smoke-test
# writer variables so the test can be added to old runners without weakening
# the actual-login requirement.
APP_ROLE = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_ROLE",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_ROLE", ""),
)
APP_PASSWORD = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_PASSWORD",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_PASSWORD", ""),
)
APP_HOST = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_HOST",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_HOST", "127.0.0.1"),
)

SQL_COUNTERS = (
    "sql_cache_hits",
    "sql_cache_misses",
    "sql_cache_fills",
    "sql_cache_bypasses",
)


if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", APP_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_ROLE is required and must be a safe SQL identifier")
if not APP_PASSWORD:
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_PASSWORD is required")
if RESP_PORT != 0 and not AUTH_TOKEN:
    raise ValueError("PG_LOCAL_CACHE_AUTH_TOKEN is required")


class RespError(RuntimeError):
    """An error response returned by the RESP endpoint."""


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def psql_base_args(*, application: bool) -> list[str]:
    arguments = [
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
    ]
    if application:
        # These later libpq options intentionally override the admin defaults
        # embedded in the Docker psql wrapper.
        arguments.extend(("-h", APP_HOST, "-U", APP_ROLE))
    return arguments


def run_psql(
    query: str,
    *,
    application: bool,
    script: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = psql_base_args(application=application)
    if not script:
        arguments.extend(("-c", query))
    environment = os.environ.copy()
    if application:
        environment["PGPASSWORD"] = APP_PASSWORD
    return subprocess.run(
        arguments,
        input=query if script else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        timeout=30,
    )


def checked_psql(query: str, *, application: bool, script: bool = False) -> str:
    result = run_psql(query, application=application, script=script)
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


def admin_sql(query: str) -> str:
    return checked_psql(query, application=False)


def app_sql(query: str) -> str:
    return checked_psql(query, application=True)


def app_script(query: str) -> str:
    # Feeding psql one statement per line keeps PREPARE alive in one backend
    # while preserving transaction boundaries between lines.
    return checked_psql(query, application=True, script=True)


def app_sql_fails(query: str, expected: str) -> None:
    result = run_psql(query, application=True)
    assert result.returncode != 0, result.stdout
    assert expected.lower() in result.stdout.lower(), result.stdout


def stats() -> dict[str, int]:
    value = json.loads(admin_sql("SELECT local_cache.stats()::text"))
    for counter in SQL_COUNTERS:
        assert isinstance(value.get(counter), int), (counter, value)
    return value


def assert_counter_delta(
    before: dict[str, int],
    after: dict[str, int],
    **expected: int,
) -> None:
    unknown = set(expected).difference(SQL_COUNTERS)
    assert not unknown, unknown
    actual = {
        counter: after[counter] - before[counter] for counter in SQL_COUNTERS
    }
    wanted = {counter: expected.get(counter, 0) for counter in SQL_COUNTERS}
    assert actual == wanted, {"actual": actual, "expected": wanted}


class RespClient:
    def __init__(self) -> None:
        self.socket = socket.create_connection((RESP_HOST, RESP_PORT), timeout=5)
        self.stream = self.socket.makefile("rb")
        assert self.command("AUTH", AUTH_TOKEN) == "OK"

    def close(self) -> None:
        self.stream.close()
        self.socket.close()

    def command(self, *arguments: object) -> object:
        encoded = [
            argument if isinstance(argument, bytes) else str(argument).encode()
            for argument in arguments
        ]
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
        if prefix in (b"+", b"-", b":"):
            line = self.stream.readline()
            if not line.endswith(b"\r\n"):
                raise ValueError("invalid RESP line")
            value = line[:-2].decode()
            if prefix == b"-":
                raise RespError(value)
            return int(value) if prefix == b":" else value
        if prefix == b"$":
            length_line = self.stream.readline()
            if not length_line.endswith(b"\r\n"):
                raise ValueError("invalid RESP bulk length")
            length = int(length_line[:-2])
            if length == -1:
                return None
            value = self.stream.read(length)
            if len(value) != length or self.stream.read(2) != b"\r\n":
                raise ValueError("truncated RESP bulk string")
            return value.decode()
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def wait_for_negative_entry(client: RespClient, key: str) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            assert client.command("GET", key) is None
            return
        except RespError as error:
            if "unknown pg_local_cache namespace" not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def assert_custom_scan(plan: str) -> None:
    assert "Custom Scan (pg_local_cache_sql)" in plan, plan


def assert_no_custom_scan(plan: str) -> None:
    assert "pg_local_cache_sql" not in plan, plan


def main() -> None:
    suffix = str(os.getpid())
    table = f"pglc_sql_fastpath_{suffix}"
    namespace = f"sqlfast{suffix}"
    missing_id = 9_000_000_000 + os.getpid()
    relation = f"public.{table}"
    quoted_app_role = sql_identifier(APP_ROLE)
    client: RespClient | None = None

    server_version_num = int(admin_sql("SHOW server_version_num"))
    assert server_version_num // 10000 == 16, server_version_num

    identity = app_sql(
        "SELECT current_user, session_user, rolcanlogin, rolsuper "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).split("|")
    assert identity == [APP_ROLE, APP_ROLE, "t", "f"], identity

    try:
        admin_sql(
            f"CREATE TABLE {relation} ("
            "id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO {relation} VALUES (1, 'one'), (-1, 'minus-one');"
            "SELECT local_cache.attach_value("
            f"'{relation}'::regclass, 'value', '{namespace}', false);"
            f"GRANT SELECT, UPDATE ON TABLE {relation} TO {quoted_app_role}"
        )

        # A literal PK lookup is eligible even on a tiny table.  The original
        # unique IndexScan remains the child used for every safe fallback.
        assert_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = 1"
            )
        )

        # The first ordinary SELECT safely self-fills from PostgreSQL; the
        # second one is returned from the shared positive cache.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # bigint = int4 is PostgreSQL's normal parse for uncast integer
        # literals.  Both signs must use a real widening coercion before the
        # key output function; reinterpreting an int4 Datum as int8 is unsafe.
        assert_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = -1"
            )
        )
        before = stats()
        assert app_sql(f"SELECT value FROM {relation} WHERE id = -1") == "minus-one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = -1") == "minus-one"
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # External Params and the common ORM-style literal LIMIT 1 retain the
        # CustomScan and use the already warm cache entry.
        prepared_plan = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_param_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1 LIMIT 1;\n"
            f"EXPLAIN (COSTS OFF) EXECUTE pglc_param_{suffix}(1);\n"
            f"DEALLOCATE pglc_param_{suffix};\n"
        )
        assert_custom_scan(prepared_plan)
        before = stats()
        assert app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_param_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1 LIMIT 1;\n"
            f"EXECUTE pglc_param_{suffix}(1);\n"
            f"DEALLOCATE pglc_param_{suffix};\n"
        ) == "one"
        assert_counter_delta(before, stats(), sql_cache_hits=1)

        # Common drivers explicitly bind a 32-bit parameter even when the PK
        # is bigint.  The btree integer opfamily supports the comparison and
        # the cache expression widens the Param losslessly.
        int4_plan = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_int4_{suffix}(integer) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXPLAIN (COSTS OFF) EXECUTE pglc_int4_{suffix}(-1);\n"
            f"DEALLOCATE pglc_int4_{suffix};\n"
        )
        assert_custom_scan(int4_plan)
        before = stats()
        assert app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_int4_{suffix}(integer) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_int4_{suffix}(-1);\n"
            f"DEALLOCATE pglc_int4_{suffix};\n"
        ) == "minus-one"
        assert_counter_delta(before, stats(), sql_cache_hits=1)

        # The USERSET kill switch produces an ordinary PostgreSQL plan and
        # does not mislabel a planner-ineligible query as a runtime bypass.
        before = stats()
        guc_off_output = app_script(
            "SET pg_local_cache.sql_cache = off;\n"
            f"SELECT value FROM {relation} WHERE id = 1;\n"
            f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = 1;\n"
        )
        guc_off_lines = guc_off_output.splitlines()
        assert guc_off_lines[0] == "one", guc_off_output
        assert_no_custom_scan("\n".join(guc_off_lines[1:]))
        assert_counter_delta(before, stats())

        # Reuse a plan prepared while the transaction is clean, then write.
        # Runtime dirty detection must fall back to the child IndexScan so the
        # transaction reads its own tuple.  ROLLBACK keeps the old cache valid.
        before = stats()
        rollback_values = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_rollback_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_rollback_{suffix}(1);\n"
            "BEGIN;\n"
            f"UPDATE {relation} SET value = 'own-write' WHERE id = 1;\n"
            f"EXECUTE pglc_rollback_{suffix}(1);\n"
            "ROLLBACK;\n"
            f"DEALLOCATE pglc_rollback_{suffix};\n"
        ).splitlines()
        assert rollback_values == ["one", "own-write"], rollback_values
        assert_counter_delta(
            before, stats(), sql_cache_hits=1, sql_cache_bypasses=1
        )
        assert admin_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"

        # A committed writer publishes the invalidation before its new tuple
        # can become visible.  The next SELECT therefore refills, then hits.
        assert (
            app_sql(
                f"UPDATE {relation} SET value = 'committed' WHERE id = 1 "
                "RETURNING value"
            )
            == "committed"
        )
        before = stats()
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "committed"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "committed"
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # A CustomScan planned in READ COMMITTED may be reused inside an older
        # snapshot transaction.  It must runtime-bypass instead of exposing a
        # value that is not justified by the REPEATABLE READ snapshot.
        before = stats()
        repeatable_values = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_rr_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_rr_{suffix}(1);\n"
            "BEGIN ISOLATION LEVEL REPEATABLE READ;\n"
            f"EXECUTE pglc_rr_{suffix}(1);\n"
            "COMMIT;\n"
            f"DEALLOCATE pglc_rr_{suffix};\n"
        ).splitlines()
        assert repeatable_values == ["committed", "committed"], repeatable_values
        assert_counter_delta(
            before, stats(), sql_cache_hits=1, sql_cache_bypasses=1
        )

        if RESP_PORT != 0:
            # RESP negative entries are never authoritative for ordinary SQL.
            client = RespClient()
            wait_for_negative_entry(client, f"{namespace}:{missing_id}")

        # With or without a RESP listener, a missing SQL row must execute
        # PostgreSQL's child plan and remain a cache miss.  In the RESP-enabled
        # profile the preceding probe also proves that a cached negative entry
        # is never authoritative for ordinary SQL.
        before = stats()
        assert (
            app_sql(f"SELECT value FROM {relation} WHERE id = {missing_id}")
            == ""
        )
        assert_counter_delta(before, stats(), sql_cache_misses=1)

        # Broader projections and additional predicates deliberately retain
        # stock PostgreSQL semantics and do not affect SQL-cache counters.
        before = stats()
        unsupported = app_sql(
            f"SELECT * FROM {relation} WHERE id = 1;"
            f"SELECT value FROM {relation} "
            "WHERE id = 1 AND value = 'committed'"
        ).splitlines()
        assert unsupported == ["1|committed", "committed"], unsupported
        assert_counter_delta(before, stats())
        assert_no_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT * FROM {relation} WHERE id = 1;"
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} "
                "WHERE id = 1 AND value = 'committed'"
            )
        )

        # The transparent hit still passes PostgreSQL's normal relation ACL
        # checks.  Revoking SELECT must fail before the custom executor can
        # return an otherwise warm entry.
        admin_sql(f"REVOKE SELECT ON TABLE {relation} FROM {quoted_app_role}")
        before = stats()
        app_sql_fails(
            f"SELECT value FROM {relation} WHERE id = 1",
            "permission denied",
        )
        assert_counter_delta(before, stats())

        print(
            "ok: transparent SQL cold fill/hit, Param and LIMIT 1, EXPLAIN, "
            "GUC fallback, transactional read-your-writes/rollback, "
            "commit invalidation, old-snapshot bypass, negative fallback, "
            "unsupported-shape fallback, and NOSUPERUSER ACL enforcement"
        )
    finally:
        if client is not None:
            client.close()
        # unregister_mapping() is idempotent.  Revoke the test role's table
        # grants before dropping the table so cleanup also covers ACL state.
        subprocess.run(
            psql_base_args(application=False),
            input=(
                f"SELECT local_cache.unregister_mapping('{namespace}');\n"
                "DO $cleanup$\n"
                "BEGIN\n"
                f"  IF pg_catalog.to_regclass('{relation}') IS NOT NULL THEN\n"
                f"    EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {relation} "
                f"FROM {quoted_app_role}';\n"
                "  END IF;\n"
                "END\n"
                "$cleanup$;\n"
                f"DROP TABLE IF EXISTS {relation};\n"
            ),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )


if __name__ == "__main__":
    main()
