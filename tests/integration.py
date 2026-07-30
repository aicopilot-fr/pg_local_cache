#!/usr/bin/env python3
"""Black-box integration test for a preloaded pg_kvik instance."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time


PSQL = os.environ.get("PGKVIK_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PGKVIK_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PGKVIK_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PGKVIK_AUTH_TOKEN", "")


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


def wait_for_mapping(client: RespClient, key: str) -> object:
    deadline = time.monotonic() + 5
    while True:
        try:
            return client.command("GET", key)
        except RespError as error:
            if "unknown pg_kvik namespace" not in str(error):
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
            if "unknown pg_kvik namespace" in str(error):
                return
            raise
        if time.monotonic() >= deadline:
            raise AssertionError("mapping remained available with a disabled trigger")
        time.sleep(0.05)


def main() -> None:
    suffix = str(os.getpid())
    table = f"pgk_it_{suffix}"
    second_table = f"pgk_it_second_{suffix}"
    rls_table = f"pgk_it_rls_{suffix}"
    partial_table = f"pgk_it_partial_{suffix}"
    namespace = f"it{suffix}"
    second_namespace = f"itsecond{suffix}"
    client: RespClient | None = None

    sql("CREATE EXTENSION IF NOT EXISTS pg_kvik")
    sql(
        f"CREATE TABLE public.{table}"
        " (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{table} VALUES (1, 'one');"
        f"SELECT kvik.register_mapping('{namespace}',"
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
                assert (
                    unauthenticated.command("AUTH", "default", AUTH_TOKEN) == "OK"
                )
            finally:
                unauthenticated.close()

        client = RespClient()
        assert client.command("PING") == "PONG"
        assert client.command("ECHO", "echo-value") == "echo-value"
        hello = client.command("HELLO", "2")
        assert hello[hello.index("server") + 1] == "pg_kvik"
        assert hello[hello.index("proto") + 1] == 2
        assert "pg_kvik_version:0.1.0" in client.command("INFO")
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

        assert wait_for_mapping(client, f"{namespace}:1") == "one"
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

        sql(f"ALTER TABLE public.{table} ADD COLUMN note text")
        assert client.command("GET", f"{namespace}:1") == "from-resp"

        sql(
            f"ALTER TABLE public.{table}"
            " DISABLE TRIGGER pg_kvik_row_invalidate"
        )
        wait_for_unavailable_mapping(client, f"{namespace}:1")
        sql(
            f"ALTER TABLE public.{table}"
            " ENABLE ALWAYS TRIGGER pg_kvik_row_invalidate"
        )
        assert wait_for_mapping(client, f"{namespace}:1") == "from-resp"

        sql(
            f"CREATE TABLE public.{second_table}"
            " (id uuid PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.{second_table}"
            " VALUES ('00000000-0000-0000-0000-000000000001', 'uuid-value');"
            f"SELECT kvik.register_mapping('{second_namespace}',"
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
        sql(f"SELECT kvik.unregister_mapping('{second_namespace}')")

        sql(
            f"CREATE TABLE public.{rls_table}"
            " (id bigint PRIMARY KEY, value text NOT NULL);"
            f"ALTER TABLE public.{rls_table} ENABLE ROW LEVEL SECURITY"
        )
        sql_fails(
            f"SELECT kvik.register_mapping('rls{suffix}',"
            f" 'public.{rls_table}', 'id', 'value', false)",
            "row-level security is not supported",
        )

        sql(
            f"CREATE TABLE public.{partial_table}"
            " (id bigint NOT NULL, value text NOT NULL);"
            f"CREATE UNIQUE INDEX ON public.{partial_table}(id) WHERE id > 0"
        )
        sql_fails(
            f"SELECT kvik.register_mapping('partial{suffix}',"
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

        cache_capacity = int(sql("SHOW pg_kvik.cache_entries"))
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

        if int(sql("SHOW max_prepared_transactions")) > 0:
            gid = f"pgk_it_{suffix}"
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
            "index validation, cache-full multi-row fence, race fence, "
            "2PC rejection, client handshake, stats"
        )
    finally:
        if client is not None:
            client.close()
        subprocess.run(
            psql_args(
                f"SELECT kvik.unregister_mapping('{second_namespace}');"
                f"SELECT kvik.unregister_mapping('{namespace}');"
                f"DROP TABLE IF EXISTS public.{second_table};"
                f"DROP TABLE IF EXISTS public.{rls_table};"
                f"DROP TABLE IF EXISTS public.{partial_table};"
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
