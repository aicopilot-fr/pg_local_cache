#!/usr/bin/env python3
"""Dependency-free RESP load test for pg_local_cache warm positive reads.

The script creates a temporary mapped table, warms every key through RESP,
then measures pipelined GETs over persistent connections.  Successful
responses, rather than requests merely sent, are used for the throughput
calculation.
"""

from __future__ import annotations

from array import array
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")

NAMESPACE = os.environ.get("PG_LOCAL_CACHE_BENCH_NAMESPACE", f"load{os.getpid()}")
TABLE = os.environ.get("PG_LOCAL_CACHE_BENCH_TABLE", f"pglc_load_{os.getpid()}")
SERVICE_ROLE = os.environ.get(
    "PG_LOCAL_CACHE_BENCH_ROLE", os.environ.get("PG_LOCAL_CACHE_ROLE", "")
)
KEY_COUNT = int(os.environ.get("PG_LOCAL_CACHE_BENCH_KEYS", "1024"))
VALUE_SIZE = int(os.environ.get("PG_LOCAL_CACHE_BENCH_VALUE_SIZE", "128"))
DURATION = float(os.environ.get("PG_LOCAL_CACHE_BENCH_DURATION", "10"))
CONCURRENCY = int(os.environ.get("PG_LOCAL_CACHE_BENCH_CONCURRENCY", "16"))
PIPELINE = int(os.environ.get("PG_LOCAL_CACHE_BENCH_PIPELINE", "32"))
MIN_OPS = float(os.environ.get("PG_LOCAL_CACHE_MIN_OPS", "10000"))
SOCKET_TIMEOUT = float(os.environ.get("PG_LOCAL_CACHE_SOCKET_TIMEOUT", "5"))
MAX_LATENCY_SAMPLES = int(
    os.environ.get("PG_LOCAL_CACHE_BENCH_MAX_LATENCY_SAMPLES", "1000000")
)
KEEP_DATA = os.environ.get("PG_LOCAL_CACHE_BENCH_KEEP_DATA", "") == "1"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")


class RespError(RuntimeError):
    """A RESP error response."""


class RespConnection:
    """Small buffered RESP2 client specialized for this load test."""

    def __init__(self) -> None:
        self.socket = socket.create_connection(
            (RESP_HOST, RESP_PORT), timeout=SOCKET_TIMEOUT
        )
        self.socket.settimeout(SOCKET_TIMEOUT)
        self.buffer = bytearray()
        self.position = 0
        if AUTH_TOKEN:
            response = self.command("AUTH", AUTH_TOKEN)
            if response != "OK":
                raise RespError(f"AUTH returned {response!r}")

    def close(self) -> None:
        self.socket.close()

    @staticmethod
    def encode_command(*arguments: object) -> bytes:
        encoded = [
            argument if isinstance(argument, bytes) else str(argument).encode()
            for argument in arguments
        ]
        parts = [f"*{len(encoded)}\r\n".encode()]
        for argument in encoded:
            parts.extend((f"${len(argument)}\r\n".encode(), argument, b"\r\n"))
        return b"".join(parts)

    def command(self, *arguments: object) -> object:
        self.socket.sendall(self.encode_command(*arguments))
        return self.read_response()

    def _receive(self) -> None:
        chunk = self.socket.recv(65536)
        if not chunk:
            raise EOFError("RESP connection closed")
        if self.position == len(self.buffer):
            self.buffer.clear()
            self.position = 0
        self.buffer.extend(chunk)

    def _compact(self) -> None:
        if self.position == len(self.buffer):
            self.buffer.clear()
            self.position = 0
        elif self.position >= 65536 and self.position * 2 >= len(self.buffer):
            del self.buffer[: self.position]
            self.position = 0

    def _read_exact(self, length: int) -> bytes:
        while len(self.buffer) - self.position < length:
            self._receive()
        start = self.position
        self.position += length
        value = bytes(self.buffer[start : self.position])
        self._compact()
        return value

    def _read_line(self) -> bytes:
        while True:
            end = self.buffer.find(b"\r\n", self.position)
            if end >= 0:
                value = bytes(self.buffer[self.position : end])
                self.position = end + 2
                self._compact()
                return value
            self._receive()

    def read_response(self) -> object:
        prefix = self._read_exact(1)
        if prefix == b"+":
            return self._read_line().decode("utf-8")
        if prefix == b"-":
            raise RespError(self._read_line().decode("utf-8", "replace"))
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"$":
            length = int(self._read_line())
            if length == -1:
                return None
            if length < -1:
                raise ValueError(f"invalid RESP bulk length {length}")
            value = self._read_exact(length)
            if self._read_exact(2) != b"\r\n":
                raise ValueError("bulk response is not terminated by CRLF")
            return value
        if prefix == b"*":
            length = int(self._read_line())
            if length == -1:
                return None
            if length < -1:
                raise ValueError(f"invalid RESP array length {length}")
            return [self.read_response() for _ in range(length)]
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def env_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def validate_configuration() -> None:
    if not IDENTIFIER_RE.fullmatch(TABLE):
        raise ValueError("PG_LOCAL_CACHE_BENCH_TABLE is not a safe SQL identifier")
    if not NAMESPACE_RE.fullmatch(NAMESPACE):
        raise ValueError("PG_LOCAL_CACHE_BENCH_NAMESPACE is invalid")
    if SERVICE_ROLE and not IDENTIFIER_RE.fullmatch(SERVICE_ROLE):
        raise ValueError("PG_LOCAL_CACHE_BENCH_ROLE is not a safe SQL identifier")
    env_int("PG_LOCAL_CACHE_BENCH_KEYS", KEY_COUNT, 1, 65536)
    env_int("PG_LOCAL_CACHE_BENCH_VALUE_SIZE", VALUE_SIZE, 1, 8192)
    env_int("PG_LOCAL_CACHE_BENCH_CONCURRENCY", CONCURRENCY, 1, 512)
    env_int("PG_LOCAL_CACHE_BENCH_PIPELINE", PIPELINE, 1, 1024)
    env_int(
        "PG_LOCAL_CACHE_BENCH_MAX_LATENCY_SAMPLES",
        MAX_LATENCY_SAMPLES,
        1000,
        10_000_000,
    )
    if DURATION <= 0 or DURATION > 3600:
        raise ValueError("PG_LOCAL_CACHE_BENCH_DURATION must be in (0, 3600]")
    if MIN_OPS < 0:
        raise ValueError("PG_LOCAL_CACHE_MIN_OPS must be non-negative")
    if SOCKET_TIMEOUT <= 0 or SOCKET_TIMEOUT > 300:
        raise ValueError("PG_LOCAL_CACHE_SOCKET_TIMEOUT must be in (0, 300]")


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


def setup_data() -> int:
    sql("CREATE EXTENSION IF NOT EXISTS pg_local_cache")
    capacity = int(sql("SHOW pg_local_cache.cache_entries"))
    if KEY_COUNT > capacity:
        raise ValueError(
            f"benchmark key count {KEY_COUNT} exceeds cache capacity {capacity}; "
            "warm-hit measurement would include evictions"
        )
    grant = (
        f"GRANT SELECT ON TABLE public.{TABLE} TO {SERVICE_ROLE};"
        if SERVICE_ROLE
        else ""
    )
    sql(
        f"SELECT local_cache.unregister_mapping('{NAMESPACE}');"
        f"DROP TABLE IF EXISTS public.{TABLE};"
        f"CREATE TABLE public.{TABLE}"
        " (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{TABLE}"
        f" SELECT g, repeat('x', {VALUE_SIZE})"
        f" FROM generate_series(1, {KEY_COUNT}) AS g;"
        f"{grant}"
        f"SELECT local_cache.register_mapping("
        f"'{NAMESPACE}', 'public.{TABLE}', 'id', 'value', false)"
    )
    return capacity


def cleanup_data() -> None:
    sql(
        f"SELECT local_cache.unregister_mapping('{NAMESPACE}');"
        f"DROP TABLE IF EXISTS public.{TABLE}"
    )


def wait_for_mapping() -> None:
    deadline = time.monotonic() + 10
    while True:
        connection: RespConnection | None = None
        try:
            connection = RespConnection()
            response = connection.command("GET", f"{NAMESPACE}:1")
            if not isinstance(response, bytes):
                raise AssertionError("warm-up key is not a positive cache value")
            return
        except (RespError, OSError) as error:
            if (
                isinstance(error, RespError)
                and "unknown pg_local_cache namespace" not in str(error)
            ):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("mapping did not become available") from error
            time.sleep(0.05)
        finally:
            if connection is not None:
                connection.close()


def get_stats() -> dict[str, int]:
    connection = RespConnection()
    try:
        response = connection.command("STAT")
    finally:
        connection.close()
    if not isinstance(response, bytes):
        raise ValueError(f"STAT returned {response!r}")
    value = json.loads(response)
    if not isinstance(value, dict):
        raise ValueError("STAT did not return an object")
    return {str(key): int(item) for key, item in value.items()}


def warm_cache(frames: list[bytes], expected: bytes) -> None:
    for attempt in range(1, 9):
        before = get_stats()
        connection = RespConnection()
        try:
            for start in range(0, len(frames), PIPELINE):
                batch = frames[start : start + PIPELINE]
                connection.socket.sendall(b"".join(batch))
                for _ in batch:
                    response = connection.read_response()
                    if response != expected:
                        raise AssertionError(
                            "warm GET returned an unexpected value: "
                            f"{response!r}"
                        )
        finally:
            connection.close()
        after = get_stats()
        if (
            after.get("database_reads", 0) == before.get("database_reads", 0)
            and after.get("cache_misses", 0) == before.get("cache_misses", 0)
        ):
            return
    raise AssertionError(
        f"warm cache did not converge after {attempt} complete passes"
    )


@dataclass
class WorkerResult:
    completed: int = 0
    errors: int = 0
    latencies_ms: array = field(default_factory=lambda: array("d"))
    messages: list[str] = field(default_factory=list)
    finished_at: float = 0.0


def build_worker_batches(
    frames: list[bytes], worker_index: int, variants: int = 64
) -> list[bytes]:
    batches: list[bytes] = []
    index = worker_index % len(frames)
    for _ in range(variants):
        commands: list[bytes] = []
        for _ in range(PIPELINE):
            commands.append(frames[index])
            index = (index + CONCURRENCY) % len(frames)
        batches.append(b"".join(commands))
    return batches


def load_worker(
    worker_index: int,
    frames: list[bytes],
    expected: bytes,
    barrier: threading.Barrier,
    timing: dict[str, float],
    result: WorkerResult,
) -> None:
    connection: RespConnection | None = None
    try:
        connection = RespConnection()
        batches = build_worker_batches(frames, worker_index)
    except Exception as error:
        result.errors += 1
        result.messages.append(f"connect/setup: {error}")
        batches = []

    try:
        barrier.wait(timeout=SOCKET_TIMEOUT + 30)
    except threading.BrokenBarrierError:
        result.errors += 1
        result.messages.append("start barrier failed")
        if connection is not None:
            connection.close()
        result.finished_at = time.perf_counter()
        return

    if connection is None:
        result.finished_at = time.perf_counter()
        return

    batch_index = 0
    sample_capacity = max(1, math.ceil(MAX_LATENCY_SAMPLES / CONCURRENCY))
    try:
        while time.perf_counter() < timing["deadline"]:
            sent_at = time.perf_counter_ns()
            connection.socket.sendall(batches[batch_index])
            batch_index = (batch_index + 1) % len(batches)
            for _ in range(PIPELINE):
                try:
                    response = connection.read_response()
                    completed_at = time.perf_counter_ns()
                    if response != expected:
                        result.errors += 1
                        if len(result.messages) < 5:
                            result.messages.append(
                                f"unexpected GET response: {response!r}"
                            )
                        continue
                    result.completed += 1
                    latency_ms = (completed_at - sent_at) / 1_000_000
                    if len(result.latencies_ms) < sample_capacity:
                        result.latencies_ms.append(latency_ms)
                    else:
                        result.latencies_ms[
                            result.completed % sample_capacity
                        ] = latency_ms
                except RespError as error:
                    result.errors += 1
                    if len(result.messages) < 5:
                        result.messages.append(str(error))
    except Exception as error:
        result.errors += 1
        if len(result.messages) < 5:
            result.messages.append(str(error))
    finally:
        connection.close()
        result.finished_at = time.perf_counter()


def percentile(sorted_values: list[float], percentage: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(0, math.ceil(percentage / 100 * len(sorted_values)) - 1)
    return sorted_values[rank]


def run_load(frames: list[bytes], expected: bytes) -> dict[str, object]:
    barrier = threading.Barrier(CONCURRENCY + 1)
    timing: dict[str, float] = {}
    results = [WorkerResult() for _ in range(CONCURRENCY)]
    threads = [
        threading.Thread(
            target=load_worker,
            args=(index, frames, expected, barrier, timing, results[index]),
            name=f"load-{index}",
        )
        for index in range(CONCURRENCY)
    ]
    for thread in threads:
        thread.start()

    start = time.perf_counter()
    timing["start"] = start
    timing["deadline"] = start + DURATION
    try:
        barrier.wait(timeout=SOCKET_TIMEOUT + 30)
    except threading.BrokenBarrierError as error:
        raise RuntimeError("load workers did not become ready") from error

    for thread in threads:
        thread.join(DURATION + SOCKET_TIMEOUT + 30)
        if thread.is_alive():
            raise RuntimeError(f"load worker {thread.name} did not stop")

    finished = max((result.finished_at for result in results), default=start)
    elapsed = max(finished - start, 1e-9)
    completed = sum(result.completed for result in results)
    errors = sum(result.errors for result in results)
    latencies = sorted(
        latency
        for result in results
        for latency in result.latencies_ms
    )
    messages = [
        message
        for result in results
        for message in result.messages
    ][:20]
    return {
        "successful_operations": completed,
        "elapsed_seconds": elapsed,
        "operations_per_second": completed / elapsed,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "latency_samples": len(latencies),
        "errors": errors,
        "error_messages": messages,
    }


def main() -> int:
    validate_configuration()
    frames = [
        RespConnection.encode_command("GET", f"{NAMESPACE}:{key}")
        for key in range(1, KEY_COUNT + 1)
    ]
    expected = b"x" * VALUE_SIZE
    capacity = 0
    setup_complete = False

    try:
        capacity = setup_data()
        setup_complete = True
        wait_for_mapping()
        warm_cache(frames, expected)
        before = get_stats()
        result = run_load(frames, expected)
        after = get_stats()

        database_reads_delta = (
            after.get("database_reads", 0) - before.get("database_reads", 0)
        )
        cache_misses_delta = (
            after.get("cache_misses", 0) - before.get("cache_misses", 0)
        )
        result.update(
            {
                "host": RESP_HOST,
                "port": RESP_PORT,
                "duration_requested_seconds": DURATION,
                "concurrency": CONCURRENCY,
                "pipeline": PIPELINE,
                "keys": KEY_COUNT,
                "value_size": VALUE_SIZE,
                "cache_capacity": capacity,
                "minimum_operations_per_second": MIN_OPS,
                "database_reads_during_measurement": database_reads_delta,
                "cache_misses_during_measurement": cache_misses_delta,
            }
        )
        print(json.dumps(result, sort_keys=True))
        print(
            "result: "
            f"{result['operations_per_second']:.0f} ops/s, "
            f"p50={result['p50_ms']:.3f} ms, "
            f"p95={result['p95_ms']:.3f} ms, "
            f"p99={result['p99_ms']:.3f} ms, "
            f"errors={result['errors']}"
        )

        failures: list[str] = []
        if result["operations_per_second"] < MIN_OPS:
            failures.append(
                f"throughput is below {MIN_OPS:.0f} ops/s"
            )
        if result["errors"] != 0:
            failures.append("RESP errors were observed")
        if database_reads_delta != 0 or cache_misses_delta != 0:
            failures.append(
                "measurement was not entirely served by the warm cache"
            )
        if failures:
            print("FAIL: " + "; ".join(failures), file=sys.stderr)
            return 1
        print(f"PASS: warm-cache throughput >= {MIN_OPS:.0f} ops/s")
        return 0
    finally:
        if setup_complete and not KEEP_DATA:
            try:
                cleanup_data()
            except Exception as error:
                print(f"warning: benchmark cleanup failed: {error}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("load test interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"load test setup failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.output:
            print(error.output, file=sys.stderr)
        raise SystemExit(2)
