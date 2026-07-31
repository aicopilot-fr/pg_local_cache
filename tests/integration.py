#!/usr/bin/env python3
"""Black-box integration test for a preloaded pg_local_cache instance."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")
AUTH_USERNAME = os.environ.get("PG_LOCAL_CACHE_AUTH_USERNAME", "")
WORKER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_ROLE", "")
REQUIRE_SMALL_CACHE = (
    os.environ.get("PG_LOCAL_CACHE_REQUIRE_SMALL_CACHE", "") == "1"
)
REQUIRE_2PC = os.environ.get("PG_LOCAL_CACHE_REQUIRE_2PC", "") == "1"

if WORKER_ROLE and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", WORKER_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_ROLE is not a safe SQL identifier")


class RespError(RuntimeError):
    pass


class RespClient:
    def __init__(self, authenticate: bool = True) -> None:
        self.socket = socket.create_connection((RESP_HOST, RESP_PORT), timeout=5)
        self.stream = self.socket.makefile("rb")
        if authenticate and AUTH_TOKEN:
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
            length = int(length_line[:-2])
            if length == -1:
                return None
            value = self.stream.read(length)
            if len(value) != length or self.stream.read(2) != b"\r\n":
                raise ValueError("truncated RESP bulk string")
            return value.decode()
        if prefix == b"*":
            length_line = self.stream.readline()
            length = int(length_line[:-2])
            if length == -1:
                return None
            return [self._read_response() for _ in range(length)]
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def psql_args(query: str) -> list[str]:
    return [
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
        query,
    ]


def sql(query: str) -> str:
    return subprocess.check_output(
        psql_args(query), text=True, stderr=subprocess.STDOUT
    ).strip()


def sql_fails(query: str, expected: str) -> None:
    result = subprocess.run(
        psql_args(query), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert result.returncode != 0, result.stdout
    assert expected in result.stdout, result.stdout


def guc_milliseconds(name: str) -> int:
    return int(
        sql(
            "SELECT (extract(epoch FROM "
            f"current_setting('{name}')::interval) * 1000)::integer"
        )
    )


def grant_worker(table: str) -> str:
    if not WORKER_ROLE:
        return ""
    return (
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{table} "
        f"TO {WORKER_ROLE};"
    )


def wait_for_mapping(client: RespClient, key: str) -> object:
    deadline = time.monotonic() + 5
    while True:
        try:
            return client.command("GET", key)
        except RespError as error:
            if "unknown pg_local_cache namespace" not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def wait_for_unavailable_mapping(client: RespClient, key: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            client.command("GET", key)
        except RespError as error:
            if "unknown pg_local_cache namespace" in str(error):
                return
            raise
        if time.monotonic() >= deadline:
            raise AssertionError("mapping remained available with a disabled trigger")
        time.sleep(0.05)


def main() -> None:
    suffix = str(os.getpid())
    table = f"pglc_it_{suffix}"
    second_table = f"pglc_it_second_{suffix}"
    typmod_table = f"pglc_it_typmod_{suffix}"
    enum_table = f"pglc_it_enum_table_{suffix}"
    enum_type = f"pglc_it_enum_type_{suffix}"
    remap_a_table = f"pglc_it_remap_a_{suffix}"
    remap_b_table = f"pglc_it_remap_b_{suffix}"
    unrelated_table = f"pglc_unrelated_{suffix}"
    rls_table = f"pglc_it_rls_{suffix}"
    partial_table = f"pglc_it_partial_{suffix}"
    collation_table = f"pglc_it_collation_{suffix}"
    collation = f"pglc_it_nd_{suffix}"
    namespace = f"it{suffix}"
    second_namespace = f"itsecond{suffix}"
    typmod_namespace = f"ittypmod{suffix}"
    remap_namespace = f"itremap{suffix}"
    client: RespClient | None = None

    sql("CREATE EXTENSION IF NOT EXISTS pg_local_cache")
    sql(
        f"CREATE TABLE public.{table}"
        " (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{table} VALUES (1, 'one');"
        f"{grant_worker(table)}"
        f"SELECT local_cache.register_mapping('{namespace}',"
        f" 'public.{table}', 'id', 'value', true)"
    )

    try:
        if AUTH_TOKEN:
            unauthenticated = RespClient(authenticate=False)
            try:
                try:
                    unauthenticated.command("GET", f"{namespace}:1")
                    raise AssertionError("GET unexpectedly succeeded without AUTH")
                except RespError as error:
                    assert "NOAUTH" in str(error)
                try:
                    unauthenticated.command("AUTH", AUTH_TOKEN + "-wrong")
                    raise AssertionError("invalid AUTH unexpectedly succeeded")
                except RespError as error:
                    assert "WRONGPASS" in str(error)
                try:
                    unauthenticated.command("AUTH", "wrong-user", AUTH_TOKEN)
                    raise AssertionError("invalid AUTH username unexpectedly succeeded")
                except RespError as error:
                    assert "WRONGPASS" in str(error)
                if AUTH_USERNAME:
                    assert (
                        unauthenticated.command(
                            "AUTH", AUTH_USERNAME, AUTH_TOKEN
                        )
                        == "OK"
                    )
                else:
                    assert unauthenticated.command("AUTH", AUTH_TOKEN) == "OK"
            finally:
                unauthenticated.close()

            limited_auth = RespClient(authenticate=False)
            try:
                for _ in range(5):
                    try:
                        limited_auth.command("AUTH", AUTH_TOKEN + "-wrong")
                        raise AssertionError("invalid AUTH unexpectedly succeeded")
                    except RespError as error:
                        assert "WRONGPASS" in str(error)
                try:
                    limited_auth.command("PING")
                    raise AssertionError("connection survived the AUTH failure limit")
                except (BrokenPipeError, ConnectionResetError, EOFError):
                    pass
            finally:
                limited_auth.close()

        client = RespClient()
        assert client.command("PING") == "PONG"
        assert client.command("ECHO", "echo-value") == "echo-value"
        try:
            client.command("ECHO", "x" * 10000)
            raise AssertionError("oversized ECHO unexpectedly succeeded")
        except RespError as error:
            assert "payload is too large" in str(error)
        assert client.command("PING") == "PONG"
        hello = client.command("HELLO", "2")
        assert hello[hello.index("server") + 1] == "pg_local_cache"
        assert hello[hello.index("proto") + 1] == 2
        assert "pg_local_cache_version:1.0.0" in client.command("INFO")
        assert client.command("CLIENT", "GETNAME") is None
        assert isinstance(client.command("CLIENT", "ID"), int)

        protocol_client = RespClient()
        try:
            fragmented = b"*1\r\n$4\r\nPING\r\n"
            for byte in fragmented:
                protocol_client.socket.sendall(bytes((byte,)))
            assert protocol_client._read_response() == "PONG"
            protocol_client.socket.sendall(
                b"*1\r\n$4\r\nPING\r\n"
                b"*2\r\n$4\r\nECHO\r\n$8\r\npipeline\r\n"
            )
            assert protocol_client._read_response() == "PONG"
            assert protocol_client._read_response() == "pipeline"
        finally:
            protocol_client.close()

        malformed_client = RespClient()
        try:
            malformed_client.socket.sendall(b"*x\r\n")
            try:
                malformed_client._read_response()
                raise AssertionError("malformed RESP unexpectedly succeeded")
            except RespError:
                pass
            try:
                malformed_client._read_response()
                raise AssertionError("malformed RESP connection remained open")
            except EOFError:
                pass
        finally:
            malformed_client.close()

        pipeline_limit = int(sql("SHOW pg_local_cache.max_pipeline_commands"))
        assert 1 <= pipeline_limit <= 4096
        # The limit is an event-loop fairness budget, not a RESP wire limit.
        # TCP does not preserve send/recv boundaries, so only ordinary
        # two-command pipelining is asserted above.

        assert wait_for_mapping(client, f"{namespace}:1") == "one"
        assert client.command("GET", f"{namespace}:1") == "one"

        for bogus in range(1050):
            try:
                client.command("INVALIDATE", f"bogus{suffix}_{bogus}")
                raise AssertionError("unknown namespace invalidation succeeded")
            except RespError as error:
                assert "unknown pg_local_cache namespace" in str(error)
        assert client.command("GET", f"{namespace}:1") == "one"

        sql(f"UPDATE public.{table} SET value = 'two' WHERE id = 1")
        assert client.command("GET", f"{namespace}:1") == "two"

        assert client.command("GET", f"{namespace}:99") is None
        sql(f"INSERT INTO public.{table} VALUES (99, 'ninety-nine')")
        assert client.command("GET", f"{namespace}:99") == "ninety-nine"

        assert client.command("GET", f"{namespace}:1") == "two"
        sql(
            f"BEGIN; UPDATE public.{table} SET value = 'rolled-back'"
            " WHERE id = 1; ROLLBACK"
        )
        assert client.command("GET", f"{namespace}:1") == "two"

        assert client.command("SET", f"{namespace}:1", "from-resp") == "OK"
        assert sql(f"SELECT value FROM public.{table} WHERE id = 1") == "from-resp"
        assert client.command("GET", f"{namespace}:1") == "from-resp"

        assert client.command("DEL", f"{namespace}:99") == 1
        assert sql(f"SELECT count(*) FROM public.{table} WHERE id = 99") == "0"
        assert client.command("GET", f"{namespace}:99") is None

        sql(f"INSERT INTO public.{table} VALUES (10, 'moving')")
        assert client.command("GET", f"{namespace}:10") == "moving"
        sql(f"UPDATE public.{table} SET id = 11 WHERE id = 10")
        assert client.command("GET", f"{namespace}:10") is None
        assert client.command("GET", f"{namespace}:11") == "moving"

        assert client.command("GET", f"{namespace}:1") == "from-resp"
        reads_before_unrelated_ddl = json.loads(client.command("STAT"))[
            "database_reads"
        ]
        sql(
            f"CREATE TABLE public.{unrelated_table}"
            " (id bigint, note text);"
            f"ALTER TABLE public.{unrelated_table}"
            " ADD COLUMN extra integer;"
            f"DROP TABLE public.{unrelated_table};"
            f"CREATE TEMP TABLE pglc_temp_{suffix}"
            " (id bigint PRIMARY KEY, note text);"
            f"ALTER TABLE pglc_temp_{suffix} ADD COLUMN extra integer;"
            f"DROP TABLE pglc_temp_{suffix}"
        )
        assert client.command("GET", f"{namespace}:1") == "from-resp"
        reads_after_unrelated_ddl = json.loads(client.command("STAT"))[
            "database_reads"
        ]
        assert reads_after_unrelated_ddl == reads_before_unrelated_ddl

        sql(f"ALTER TABLE public.{table} ADD COLUMN note text")
        assert client.command("GET", f"{namespace}:1") == "from-resp"

        sql(
            f"ALTER TABLE public.{table}"
            " DISABLE TRIGGER pg_local_cache_row_invalidate"
        )
        wait_for_unavailable_mapping(client, f"{namespace}:1")
        sql(
            f"ALTER TABLE public.{table}"
            " ENABLE ALWAYS TRIGGER pg_local_cache_row_invalidate"
        )
        assert wait_for_mapping(client, f"{namespace}:1") == "from-resp"
        sql(
            "DROP TRIGGER pg_local_cache_row_invalidate "
            f"ON public.{table}"
        )
        wait_for_unavailable_mapping(client, f"{namespace}:1")
        sql(
            f"SELECT local_cache.register_mapping('{namespace}',"
            f" 'public.{table}', 'id', 'value', true)"
        )
        assert wait_for_mapping(client, f"{namespace}:1") == "from-resp"

        sql(
            f"CREATE TABLE public.{second_table}"
            " (id uuid PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.{second_table}"
            " VALUES ('00000000-0000-0000-0000-000000000001', 'uuid-value');"
            f"{grant_worker(second_table)}"
            f"SELECT local_cache.register_mapping('{second_namespace}',"
            f" 'public.{second_table}', 'id', 'value', false)"
        )
        assert (
            wait_for_mapping(
                client,
                f"{second_namespace}:00000000-0000-0000-0000-000000000001",
            )
            == "uuid-value"
        )
        sql(f"DROP TABLE public.{second_table}")
        wait_for_unavailable_mapping(
            client,
            f"{second_namespace}:00000000-0000-0000-0000-000000000001",
        )
        sql(f"SELECT local_cache.unregister_mapping('{second_namespace}')")

        sql(
            f"CREATE TABLE public.{typmod_table}"
            " (id character(4) PRIMARY KEY, value character varying(5) NOT NULL);"
            f"INSERT INTO public.{typmod_table} VALUES ('a', 'one');"
            f"{grant_worker(typmod_table)}"
            f"SELECT local_cache.register_mapping('{typmod_namespace}',"
            f" 'public.{typmod_table}', 'id', 'value', true)"
        )
        assert wait_for_mapping(client, f"{typmod_namespace}:a") == "one"
        sql(f"UPDATE public.{typmod_table} SET value = 'two' WHERE id = 'a'")
        assert client.command("GET", f"{typmod_namespace}:a") == "two"
        try:
            client.command("SET", f"{typmod_namespace}:a", "123456")
            raise AssertionError("varchar typmod was not enforced")
        except RespError as error:
            assert "value too long" in str(error)
        statement_timeout_ms = guc_milliseconds(
            "pg_local_cache.statement_timeout_ms"
        )
        time.sleep(statement_timeout_ms / 1000 + 0.1)
        assert client.command("PING") == "PONG"
        assert client.command("GET", f"{typmod_namespace}:a") == "two"
        sql(f"SELECT local_cache.unregister_mapping('{typmod_namespace}')")
        wait_for_unavailable_mapping(client, f"{typmod_namespace}:a")

        sql(
            f"CREATE TYPE public.{enum_type} AS ENUM ('old-label');"
            f"CREATE TABLE public.{enum_table}"
            f" (id bigint PRIMARY KEY, value public.{enum_type} NOT NULL);"
            f"INSERT INTO public.{enum_table} VALUES (1, 'old-label');"
            f"{grant_worker(enum_table)}"
        )
        sql_fails(
            f"SELECT local_cache.register_mapping('itenum{suffix}',"
            f" 'public.{enum_table}', 'id', 'value', false)",
            "unsupported value type",
        )

        sql(f"INSERT INTO public.{table} VALUES (700, 'locked')")
        table_oid = int(sql(f"SELECT 'public.{table}'::regclass::oid"))
        lock_timeout_ms = guc_milliseconds("pg_local_cache.lock_timeout_ms")
        hold_seconds = max(statement_timeout_ms, lock_timeout_ms) / 1000 + 1
        locker = subprocess.Popen(
            psql_args(
                f"BEGIN; LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE;"
                f"SELECT pg_sleep({hold_seconds}); COMMIT"
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while int(
                sql(
                    "SELECT count(*) FROM pg_catalog.pg_locks "
                    f"WHERE relation = {table_oid} "
                    "AND mode = 'AccessExclusiveLock' AND granted"
                )
            ) == 0:
                if time.monotonic() >= deadline:
                    raise AssertionError("table locker did not acquire its lock")
                time.sleep(0.02)
            started = time.monotonic()
            try:
                client.command("GET", f"{namespace}:700")
                raise AssertionError("locked GET unexpectedly succeeded")
            except RespError as error:
                assert "timeout" in str(error)
                if statement_timeout_ms < lock_timeout_ms:
                    assert "statement timeout" in str(error)
            elapsed = time.monotonic() - started
            assert elapsed < min(statement_timeout_ms, lock_timeout_ms) / 1000 + 1
            assert client.command("PING") == "PONG"
        finally:
            if locker.poll() is None:
                locker.terminate()
            locker_output = locker.communicate(timeout=5)[0]
            assert locker.returncode is not None, locker_output
        unlock_deadline = time.monotonic() + hold_seconds + 2
        while int(
            sql(
                "SELECT count(*) FROM pg_catalog.pg_locks "
                f"WHERE relation = {table_oid} "
                "AND mode = 'AccessExclusiveLock' AND granted"
            )
        ) > 0:
            if time.monotonic() >= unlock_deadline:
                raise AssertionError("table locker did not release its lock")
            time.sleep(0.02)
        assert client.command("GET", f"{namespace}:700") == "locked"

        if "--with-icu" in sql(
            "SELECT setting FROM pg_catalog.pg_config "
            "WHERE name = 'CONFIGURE'"
        ):
            sql(
                f"CREATE COLLATION public.{collation} "
                "(provider = icu, locale = 'und-u-ks-level2', "
                "deterministic = false);"
                f"CREATE TABLE public.{collation_table} "
                f"(id text COLLATE public.{collation} PRIMARY KEY, "
                "value text NOT NULL)"
            )
            sql_fails(
                f"SELECT local_cache.register_mapping('itcoll{suffix}',"
                f" 'public.{collation_table}', 'id', 'value', false)",
                "nondeterministic key collations are not supported",
            )

        relation_states_before = json.loads(client.command("STAT"))[
            "relation_states"
        ]
        sql(
            f"CREATE TABLE public.{remap_a_table}"
            " (id bigint PRIMARY KEY, value text NOT NULL);"
            f"CREATE TABLE public.{remap_b_table}"
            " (id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.{remap_a_table} VALUES (1, 'from-a');"
            f"INSERT INTO public.{remap_b_table} VALUES (1, 'from-b');"
            f"{grant_worker(remap_a_table)}"
            f"{grant_worker(remap_b_table)}"
            f"SELECT local_cache.register_mapping('{remap_namespace}',"
            f" 'public.{remap_a_table}', 'id', 'value', false)"
        )
        assert wait_for_mapping(client, f"{remap_namespace}:1") == "from-a"
        for iteration in range(12):
            expected = "from-b" if iteration % 2 == 0 else "from-a"
            relation = (
                f"public.{remap_b_table}"
                if iteration % 2 == 0
                else f"public.{remap_a_table}"
            )
            sql(
                f"SELECT local_cache.register_mapping('{remap_namespace}',"
                f" '{relation}', 'id', 'value', false)"
            )
            assert (
                wait_for_mapping(client, f"{remap_namespace}:1") == expected
            )
        sql(f"SELECT local_cache.unregister_mapping('{remap_namespace}')")
        wait_for_unavailable_mapping(client, f"{remap_namespace}:1")
        post_remap_stats = json.loads(client.command("STAT"))
        assert post_remap_stats["pending_forget"] == 0
        assert post_remap_stats["relation_states"] <= relation_states_before

        configured_workers = int(sql("SHOW pg_local_cache.workers"))
        expected_worker_ids = {
            int(pid)
            for pid in sql(
                "SELECT pid FROM pg_catalog.pg_stat_activity "
                "WHERE backend_type = 'pg_local_cache RESP worker' ORDER BY pid"
            ).splitlines()
            if pid
        }
        assert len(expected_worker_ids) == configured_workers, expected_worker_ids

        assert client.command("GET", f"{namespace}:1") == "from-resp"
        reads_before = json.loads(client.command("STAT"))["database_reads"]
        probes_by_worker: dict[int, RespClient] = {}
        deadline = time.monotonic() + 10
        try:
            while set(probes_by_worker) != expected_worker_ids:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "did not reach every RESP worker: "
                        f"expected={expected_worker_ids}, "
                        f"seen={set(probes_by_worker)}"
                    )
                probe = RespClient()
                worker_id = int(probe.command("CLIENT", "ID"))
                assert worker_id in expected_worker_ids, worker_id
                if worker_id in probes_by_worker:
                    probe.close()
                    continue
                probes_by_worker[worker_id] = probe

            for worker_id, probe in probes_by_worker.items():
                assert (
                    wait_for_mapping(probe, f"{namespace}:1") == "from-resp"
                ), worker_id

            reads_after = json.loads(client.command("STAT"))["database_reads"]
            assert reads_after == reads_before
            current_worker_ids = {
                int(pid)
                for pid in sql(
                    "SELECT pid FROM pg_catalog.pg_stat_activity "
                    "WHERE backend_type = 'pg_local_cache RESP worker' "
                    "ORDER BY pid"
                ).splitlines()
                if pid
            }
            assert current_worker_ids == expected_worker_ids
        finally:
            for probe in probes_by_worker.values():
                probe.close()

        sql(
            f"CREATE TABLE public.{rls_table}"
            " (id bigint PRIMARY KEY, value text NOT NULL);"
            f"ALTER TABLE public.{rls_table} ENABLE ROW LEVEL SECURITY"
        )
        sql_fails(
            f"SELECT local_cache.register_mapping('rls{suffix}',"
            f" 'public.{rls_table}', 'id', 'value', false)",
            "row-level security is not supported",
        )

        sql(
            f"CREATE TABLE public.{partial_table}"
            " (id bigint NOT NULL, value text NOT NULL);"
            f"CREATE UNIQUE INDEX ON public.{partial_table}(id) WHERE id > 0"
        )
        sql_fails(
            f"SELECT local_cache.register_mapping('partial{suffix}',"
            f" 'public.{partial_table}', 'id', 'value', false)",
            "needs a valid single-column UNIQUE index",
        )

        sql(f"INSERT INTO public.{table} VALUES (500, '0')")
        assert client.command("GET", f"{namespace}:500") == "0"
        for value in range(1, 26):
            writer = subprocess.Popen(
                psql_args(
                    f"UPDATE public.{table} SET value = '{value}' WHERE id = 500"
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            while writer.poll() is None:
                observed = int(client.command("GET", f"{namespace}:500"))
                assert observed in (value - 1, value), (value, observed)
            output = writer.communicate()[0]
            assert writer.returncode == 0, output
            assert client.command("GET", f"{namespace}:500") == str(value)

        cache_capacity = int(sql("SHOW pg_local_cache.cache_entries"))
        if REQUIRE_SMALL_CACHE:
            assert cache_capacity <= 512, cache_capacity
        if cache_capacity <= 512:
            sql(
                f"INSERT INTO public.{table} "
                f"SELECT 1000 + g, 'fill-' || g "
                f"FROM generate_series(0, {cache_capacity + 3}) AS g"
            )
            for offset in range(cache_capacity + 4):
                assert (
                    client.command("GET", f"{namespace}:{1000 + offset}")
                    == f"fill-{offset}"
                )
            sql(
                f"INSERT INTO public.{table} VALUES "
                f"({10000 + cache_capacity}, 'full-a'), "
                f"({10001 + cache_capacity}, 'full-b')"
            )
            assert (
                client.command("GET", f"{namespace}:{10000 + cache_capacity}")
                == "full-a"
            )
            assert (
                client.command("GET", f"{namespace}:{10001 + cache_capacity}")
                == "full-b"
            )

        prepared_transactions = int(sql("SHOW max_prepared_transactions"))
        if REQUIRE_2PC:
            assert prepared_transactions > 0, prepared_transactions
        if prepared_transactions > 0:
            gid = f"pglc_it_{suffix}"
            sql_fails(
                f"BEGIN; UPDATE public.{table} SET value = 'prepared'"
                f" WHERE id = 1; PREPARE TRANSACTION '{gid}'",
                "PREPARE TRANSACTION is not supported",
            )

        stats = json.loads(client.command("STAT"))
        assert stats["cache_hits"] > 0
        assert stats["cache_misses"] > 0
        assert stats["database_reads"] > 0
        assert stats["database_writes"] >= 2
        assert stats["invalidations"] > 0
        assert stats["store_size"] == (
            stats["positive_entries"] + stats["negative_entries"]
        )
        assert stats["cache_hit"] == stats["cache_hits"]
        assert stats["cache_miss"] == stats["cache_misses"]
        assert stats["authentication_failures"] >= (7 if AUTH_TOKEN else 0)
        assert stats["protocol_errors"] > 0

        sql(f"TRUNCATE public.{table}")
        assert client.command("GET", f"{namespace}:1") is None

        try:
            client.command("FLUSHALL")
            raise AssertionError("unsupported command unexpectedly succeeded")
        except RespError as error:
            assert "unsupported command" in str(error)

        print(
            "ok: auth, RESP GET/SET/DEL, positive/negative cache, "
            "SQL/DDL/TRUNCATE invalidation, rollback, key moves, "
            "trigger fail-closed, multi-mapping reload, RLS rejection, "
            "index/collation/typmod/value-type validation, "
            "remap fence and state GC, "
            "cache-full multi-row fence, race fence, 2PC rejection, "
            "AUTH/protocol limits, pipeline fairness config, "
            "multi-worker shared hits, stats"
        )
    finally:
        if client is not None:
            client.close()
        subprocess.run(
            psql_args(
                f"SELECT local_cache.unregister_mapping('{second_namespace}');"
                f"SELECT local_cache.unregister_mapping('{typmod_namespace}');"
                f"SELECT local_cache.unregister_mapping('{remap_namespace}');"
                f"SELECT local_cache.unregister_mapping('{namespace}');"
                f"DROP TABLE IF EXISTS public.{second_table};"
                f"DROP TABLE IF EXISTS public.{typmod_table};"
                f"DROP TABLE IF EXISTS public.{enum_table};"
                f"DROP TYPE IF EXISTS public.{enum_type};"
                f"DROP TABLE IF EXISTS public.{remap_a_table};"
                f"DROP TABLE IF EXISTS public.{remap_b_table};"
                f"DROP TABLE IF EXISTS public.{unrelated_table};"
                f"DROP TABLE IF EXISTS public.{rls_table};"
                f"DROP TABLE IF EXISTS public.{partial_table};"
                f"DROP TABLE IF EXISTS public.{collation_table};"
                f"DROP COLLATION IF EXISTS public.{collation};"
                f"DROP TABLE IF EXISTS public.{table}"
            ),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"integration test failed: {error}", file=sys.stderr)
        raise
