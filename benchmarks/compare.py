#!/usr/bin/env python3
"""Reproducible warm-GET comparison for pg_local_cache, Valkey, and Redis.

The three RESP targets use this exact client, request stream, keyspace, value,
connection count, and pipeline depth.  Direct PostgreSQL is reported
separately because it uses libpq/pgbench and different wire semantics.
"""

from __future__ import annotations

from array import array
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import queue
import random
import re
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


NAMESPACE = "comparison"
TABLE = "pg_local_cache_comparison"
PG_HOST = "pg-local-cache"
PLAIN_PG_HOST = "postgres-plain"
PG_PORT = 5432
PG_DATABASE = "benchmark"
PG_USER = "postgres"
PG_WORKER_ROLE = "local_cache_benchmark_worker"
SOCKET_TIMEOUT = 10.0
LATENCY_RESERVOIR_SEED = 0x50474C43


class RespError(RuntimeError):
    """An error returned by a RESP server."""


@dataclass(frozen=True)
class Config:
    duration: float
    warmup_seconds: float
    repetitions: int
    concurrency: int
    pipeline: int
    keys: int
    value_size: int
    max_latency_samples: int
    min_ops: float
    client_cpus: float
    server_cpus: float
    client_memory: str
    server_memory: str
    pg_local_cache_workers: int
    pg_jobs: int
    output_directory: Path
    auth_token: str
    pg_password: str

    @classmethod
    def from_environment(cls) -> "Config":
        concurrency = env_int("PGLC_BENCH_CONCURRENCY", 16, 1, 128)
        client_cpus = env_float(
            "PGLC_BENCH_CLIENT_CPUS", 4, 0.1, 1024
        )
        config = cls(
            duration=env_float("PGLC_BENCH_DURATION", 120, 1, 3600),
            warmup_seconds=env_float(
                "PGLC_BENCH_WARMUP_SECONDS", 15, 0, 600
            ),
            repetitions=env_int("PGLC_BENCH_REPETITIONS", 3, 1, 20),
            concurrency=concurrency,
            pipeline=env_int("PGLC_BENCH_PIPELINE", 32, 1, 256),
            keys=env_int("PGLC_BENCH_KEYS", 16384, 1, 65536),
            value_size=env_int("PGLC_BENCH_VALUE_SIZE", 128, 1, 8192),
            max_latency_samples=env_int(
                "PGLC_BENCH_MAX_LATENCY_SAMPLES",
                1_000_000,
                1000,
                10_000_000,
            ),
            min_ops=env_float("PGLC_BENCH_MIN_OPS", 10_000, 0, 1e12),
            client_cpus=client_cpus,
            server_cpus=env_float("PGLC_BENCH_SERVER_CPUS", 4, 0.1, 1024),
            client_memory=os.environ.get(
                "PGLC_BENCH_CLIENT_MEMORY", "1g"
            ),
            server_memory=os.environ.get(
                "PGLC_BENCH_SERVER_MEMORY", "2g"
            ),
            pg_local_cache_workers=env_int(
                "PGLC_BENCH_PG_LOCAL_CACHE_WORKERS", 4, 1, 32
            ),
            pg_jobs=env_int(
                "PGLC_BENCH_PG_JOBS",
                min(concurrency, max(1, int(client_cpus))),
                1,
                concurrency,
            ),
            output_directory=Path(
                os.environ.get("PGLC_BENCH_OUTPUT_DIR", "/results")
            ),
            auth_token=require_env("PGLC_BENCH_AUTH_TOKEN"),
            pg_password=require_env("PGLC_BENCH_PG_PASSWORD"),
        )
        if "\x00" in config.auth_token or "\r" in config.auth_token:
            raise ValueError("benchmark AUTH token contains an invalid byte")
        config.validate()
        return config

    def validate(self) -> None:
        if self.keys < self.concurrency:
            raise ValueError("key count must be at least the connection count")
        if self.keys % self.concurrency != 0:
            raise ValueError(
                "key count must be divisible by the connection count so "
                "workers cover one disjoint, complete keyspace"
            )
        keys_per_connection = self.keys // self.concurrency
        if keys_per_connection % self.pipeline != 0:
            raise ValueError(
                "keys per connection must be divisible by pipeline depth "
                "so every configured key has equal request weight"
            )
        maximum_clients = self.pg_local_cache_workers * 128
        if self.concurrency > maximum_clients:
            raise ValueError(
                f"{self.concurrency} connections exceed pg_local_cache "
                f"capacity {maximum_clients} for "
                f"{self.pg_local_cache_workers} worker(s)"
            )


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    port: int
    version_field: str


TARGETS = (
    Target("pg_local_cache", "pg-local-cache", 6380, "pg_local_cache_version"),
    Target("valkey", "valkey", 6379, "valkey_version"),
    Target("redis", "redis", 6379, "redis_version"),
)


@dataclass
class WorkerResult:
    worker_index: int
    completed: int = 0
    errors: int = 0
    latencies_ms: array = field(default_factory=lambda: array("d"))
    messages: list[str] = field(default_factory=list)
    finished_at: float = 0.0


class RespConnection:
    """Small buffered RESP2 client used unchanged for every RESP target."""

    def __init__(self, target: Target, auth_token: str) -> None:
        self.target = target
        self.socket = socket.create_connection(
            (target.host, target.port), timeout=SOCKET_TIMEOUT
        )
        self.socket.settimeout(SOCKET_TIMEOUT)
        self.buffer = bytearray()
        self.position = 0
        response = self.command("AUTH", auth_token)
        if response != "OK":
            self.close()
            raise RespError(f"AUTH returned {response!r}")

    def close(self) -> None:
        self.socket.close()

    @staticmethod
    def encode_command(*arguments: object) -> bytes:
        encoded = [
            item if isinstance(item, bytes) else str(item).encode()
            for item in arguments
        ]
        parts = [f"*{len(encoded)}\r\n".encode()]
        for item in encoded:
            parts.extend((f"${len(item)}\r\n".encode(), item, b"\r\n"))
        return b"".join(parts)

    def command(self, *arguments: object) -> object:
        self.socket.sendall(self.encode_command(*arguments))
        return self.read_response()

    def _receive(self) -> None:
        chunk = self.socket.recv(65536)
        if not chunk:
            raise EOFError(f"{self.target.name} closed the RESP connection")
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
        result = bytes(self.buffer[start : self.position])
        self._compact()
        return result

    def _read_line(self) -> bytes:
        while True:
            end = self.buffer.find(b"\r\n", self.position)
            if end >= 0:
                result = bytes(self.buffer[self.position : end])
                self.position = end + 2
                self._compact()
                return result
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
                raise ValueError("bulk response lacks its terminating CRLF")
            return value
        if prefix == b"*":
            length = int(self._read_line())
            if length == -1:
                return None
            if length < -1:
                raise ValueError(f"invalid RESP array length {length}")
            return [self.read_response() for _ in range(length)]
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name) or str(default)
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def env_float(
    name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = os.environ.get(name) or str(default)
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def psql(config: Config, query: str, host: str = PG_HOST) -> str:
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            str(PG_PORT),
            "-U",
            PG_USER,
            "-d",
            PG_DATABASE,
            "-Atq",
            "-c",
            query,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        timeout=300,
    )
    return result.stdout.strip()


def setup_postgres(config: Config) -> int:
    psql(
        config,
        "CREATE EXTENSION IF NOT EXISTS pg_local_cache;"
        f"SELECT local_cache.unregister_mapping('{NAMESPACE}');"
        f"DROP TABLE IF EXISTS public.{TABLE};"
        f"CREATE TABLE public.{TABLE}"
        " (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{TABLE}"
        f" SELECT g, repeat('x', {config.value_size})"
        f" FROM generate_series(1, {config.keys}) AS g;"
        f"GRANT SELECT ON TABLE public.{TABLE} TO {PG_WORKER_ROLE};"
        f"SELECT local_cache.register_mapping("
        f"'{NAMESPACE}', 'public.{TABLE}', 'id', 'value', false);"
        f"ANALYZE public.{TABLE}",
    )
    capacity = int(psql(config, "SHOW pg_local_cache.cache_entries"))
    if config.keys > capacity:
        raise ValueError(
            f"keyspace {config.keys} exceeds pg_local_cache capacity {capacity}"
        )
    return capacity


def setup_plain_postgres(config: Config) -> None:
    psql(
        config,
        f"DROP TABLE IF EXISTS public.{TABLE};"
        f"CREATE TABLE public.{TABLE}"
        " (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{TABLE}"
        f" SELECT g, repeat('x', {config.value_size})"
        f" FROM generate_series(1, {config.keys}) AS g;"
        f"ANALYZE public.{TABLE}",
        host=PLAIN_PG_HOST,
    )


def wait_for_mapping(
    target: Target, config: Config, expected: bytes
) -> None:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        connection: RespConnection | None = None
        try:
            connection = RespConnection(target, config.auth_token)
            response = connection.command("GET", f"{NAMESPACE}:1")
            if response == expected:
                return
            last_error = AssertionError(
                f"mapping probe returned {response!r}"
            )
        except (OSError, RespError) as error:
            last_error = error
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.05)
    raise TimeoutError("pg_local_cache mapping did not become ready") from last_error


def populate_external_target(
    target: Target, config: Config, keys: list[bytes], expected: bytes
) -> None:
    connection = RespConnection(target, config.auth_token)
    try:
        if connection.command("FLUSHDB") != "OK":
            raise AssertionError(f"{target.name} FLUSHDB did not return OK")
        batch_size = min(256, config.pipeline * 4)
        for start in range(0, len(keys), batch_size):
            batch = keys[start : start + batch_size]
            frames = [
                RespConnection.encode_command("SET", key, expected)
                for key in batch
            ]
            connection.socket.sendall(b"".join(frames))
            for _ in frames:
                if connection.read_response() != "OK":
                    raise AssertionError(f"{target.name} SET did not return OK")
        size = connection.command("DBSIZE")
        if size != config.keys:
            raise AssertionError(
                f"{target.name} contains {size!r} keys, expected {config.keys}"
            )
    finally:
        connection.close()


def warm_all(
    target: Target,
    config: Config,
    frames: list[bytes],
    expected: bytes,
) -> None:
    connection = RespConnection(target, config.auth_token)
    batch_size = min(256, max(config.pipeline, 1))
    try:
        for start in range(0, len(frames), batch_size):
            batch = frames[start : start + batch_size]
            connection.socket.sendall(b"".join(batch))
            for _ in batch:
                response = connection.read_response()
                if response != expected:
                    raise AssertionError(
                        f"{target.name} warm GET returned {response!r}"
                    )
    finally:
        connection.close()


def read_info(target: Target, config: Config) -> dict[str, str]:
    connection = RespConnection(target, config.auth_token)
    try:
        response = connection.command("INFO", "server")
    finally:
        connection.close()
    if not isinstance(response, bytes):
        raise ValueError(f"{target.name} INFO returned {response!r}")
    result: dict[str, str] = {}
    for raw_line in response.decode("utf-8", "replace").splitlines():
        if not raw_line or raw_line.startswith("#") or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        result[key] = value
    return result


def read_pglc_stats(target: Target, config: Config) -> dict[str, int]:
    connection = RespConnection(target, config.auth_token)
    try:
        response = connection.command("STAT")
    finally:
        connection.close()
    if not isinstance(response, bytes):
        raise ValueError(f"STAT returned {response!r}")
    parsed = json.loads(response)
    if not isinstance(parsed, dict):
        raise ValueError("STAT did not return an object")
    return {str(key): int(value) for key, value in parsed.items()}


def build_worker_batches(
    frames: list[bytes], worker_index: int, config: Config
) -> list[bytes]:
    batches: list[bytes] = []
    index = worker_index % len(frames)
    cycle_length = len(frames) // math.gcd(
        len(frames), config.concurrency
    )
    batch_count = math.ceil(cycle_length / config.pipeline)
    for _ in range(batch_count):
        commands: list[bytes] = []
        for _ in range(config.pipeline):
            commands.append(frames[index])
            index = (index + config.concurrency) % len(frames)
        batches.append(b"".join(commands))
    return batches


def load_worker(
    worker_index: int,
    target: Target,
    config: Config,
    frames: list[bytes],
    expected: bytes,
    ready: Any,
    start_event: Any,
    deadline_value: Any,
    result_queue: Any,
    collect_latencies: bool,
) -> None:
    result = WorkerResult(worker_index=worker_index)
    connection: RespConnection | None = None
    try:
        connection = RespConnection(target, config.auth_token)
        batches = build_worker_batches(frames, worker_index, config)
    except Exception as error:
        result.errors += 1
        result.messages.append(f"connect/setup: {error}")
        batches = []

    try:
        ready.wait(timeout=SOCKET_TIMEOUT + 30)
        if not start_event.wait(timeout=SOCKET_TIMEOUT + 30):
            raise TimeoutError("start event was not set")
    except Exception as error:
        result.errors += 1
        result.messages.append(f"start synchronization: {error}")
        if connection is not None:
            connection.close()
        result.finished_at = time.perf_counter()
        result_queue.put(result)
        return

    if connection is None:
        result.finished_at = time.perf_counter()
        result_queue.put(result)
        return

    batch_index = 0
    base_capacity, extra_samples = divmod(
        config.max_latency_samples, config.concurrency
    )
    sample_capacity = base_capacity + (
        1 if worker_index < extra_samples else 0
    )
    latency_generator = random.Random(
        LATENCY_RESERVOIR_SEED ^ worker_index
    )
    try:
        while time.perf_counter() < deadline_value.value:
            sent_at = time.perf_counter_ns()
            connection.socket.sendall(batches[batch_index])
            batch_index = (batch_index + 1) % len(batches)
            for _ in range(config.pipeline):
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
                    if collect_latencies:
                        latency = (completed_at - sent_at) / 1_000_000
                        add_reservoir_sample(
                            result.latencies_ms,
                            latency,
                            result.completed,
                            sample_capacity,
                            latency_generator,
                        )
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
        result_queue.put(result)


def usage_seconds(usage: resource.struct_rusage) -> float:
    return usage.ru_utime + usage.ru_stime


def weighted_percentile(
    sorted_values: list[tuple[float, float]], percentage: float
) -> float:
    if not sorted_values:
        return 0.0
    threshold = math.fsum(weight for _, weight in sorted_values) * (
        percentage / 100
    )
    cumulative = 0.0
    for value, weight in sorted_values:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return sorted_values[-1][0]


def add_reservoir_sample(
    samples: array,
    value: float,
    seen: int,
    capacity: int,
    generator: random.Random,
) -> None:
    """Add one observation using Algorithm R uniform reservoir sampling."""
    if seen <= 0 or capacity <= 0:
        raise ValueError("reservoir counters must be positive")
    if len(samples) < capacity:
        samples.append(value)
        return
    replacement = generator.randrange(seen)
    if replacement < capacity:
        samples[replacement] = value


def run_resp_load(
    target: Target,
    config: Config,
    frames: list[bytes],
    expected: bytes,
    duration: float,
    collect_latencies: bool,
) -> dict[str, Any]:
    context = multiprocessing.get_context("fork")
    ready = context.Barrier(config.concurrency + 1)
    start_event = context.Event()
    deadline_value = context.Value("d", 0.0, lock=False)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=load_worker,
            args=(
                index,
                target,
                config,
                frames,
                expected,
                ready,
                start_event,
                deadline_value,
                result_queue,
                collect_latencies,
            ),
            name=f"{target.name}-{index}",
        )
        for index in range(config.concurrency)
    ]
    child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    for process in processes:
        process.start()

    try:
        ready.wait(timeout=SOCKET_TIMEOUT + 30)
    except Exception as error:
        for process in processes:
            process.terminate()
        for process in processes:
            process.join(timeout=5)
        raise RuntimeError(
            f"{target.name} workers did not become ready"
        ) from error

    started_at = time.perf_counter()
    deadline_value.value = started_at + duration
    start_event.set()
    results: list[WorkerResult] = []
    receive_deadline = time.monotonic() + duration + SOCKET_TIMEOUT + 30
    while len(results) < len(processes):
        remaining = receive_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            results.append(result_queue.get(timeout=remaining))
        except queue.Empty:
            break

    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.exitcode not in (0, None):
            results.append(
                WorkerResult(
                    worker_index=-1,
                    errors=1,
                    messages=[
                        f"worker {process.name} exited with {process.exitcode}"
                    ],
                    finished_at=time.perf_counter(),
                )
            )
    result_queue.close()
    result_queue.join_thread()

    missing = config.concurrency - sum(
        1 for result in results if result.worker_index >= 0
    )
    if missing > 0:
        results.append(
            WorkerResult(
                worker_index=-1,
                errors=missing,
                messages=[f"{missing} worker results were not returned"],
                finished_at=time.perf_counter(),
            )
        )

    finished_at = max(
        (result.finished_at for result in results), default=started_at
    )
    elapsed = max(finished_at - started_at, 1e-9)
    completed = sum(result.completed for result in results)
    errors = sum(result.errors for result in results)
    weighted_latencies = sorted(
        (
            latency,
            result.completed / len(result.latencies_ms),
        )
        for result in results
        if result.latencies_ms
        for latency in result.latencies_ms
    )
    child_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    client_cpu_seconds = (
        usage_seconds(child_usage_after) - usage_seconds(child_usage_before)
    )
    return {
        "successful_operations": completed,
        "elapsed_seconds": elapsed,
        "operations_per_second": completed / elapsed,
        "p50_ms": weighted_percentile(weighted_latencies, 50),
        "p95_ms": weighted_percentile(weighted_latencies, 95),
        "p99_ms": weighted_percentile(weighted_latencies, 99),
        "latency_samples": len(weighted_latencies),
        "errors": errors,
        "error_messages": [
            message
            for result in results
            for message in result.messages
        ][:20],
        "client_cpu_seconds": max(client_cpu_seconds, 0.0),
        "client_cpu_quota_utilization_percent": (
            max(client_cpu_seconds, 0.0)
            / elapsed
            / config.client_cpus
            * 100
        ),
    }


def summarize_throughput_runs(
    runs: list[dict[str, Any]],
) -> dict[str, float]:
    rates = [float(run["operations_per_second"]) for run in runs]
    mean = statistics.fmean(rates)
    return {
        "median_operations_per_second": statistics.median(rates),
        "mean_operations_per_second": mean,
        "minimum_operations_per_second": min(rates),
        "maximum_operations_per_second": max(rates),
        "coefficient_of_variation_percent": (
            statistics.pstdev(rates) / mean * 100 if mean else 0.0
        ),
    }


def summarize_resp_runs(
    runs: list[dict[str, Any]],
) -> dict[str, float]:
    summary = summarize_throughput_runs(runs)
    summary.update(
        {
            "median_p50_ms": statistics.median(
                float(run["p50_ms"]) for run in runs
            ),
            "median_p95_ms": statistics.median(
                float(run["p95_ms"]) for run in runs
            ),
            "median_p99_ms": statistics.median(
                float(run["p99_ms"]) for run in runs
            ),
        }
    )
    return summary


def pgbench_script(config: Config) -> str:
    lines = ["\\startpipeline"]
    for index in range(config.pipeline):
        lines.extend(
            (
                f"\\set key_{index} random(1, {config.keys})",
                f"SELECT value FROM public.{TABLE} WHERE id = :key_{index};",
            )
        )
    lines.append("\\endpipeline")
    return "\n".join(lines) + "\n"


def run_pgbench_once(
    config: Config, script_path: Path, duration: float, seed: int
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    result = subprocess.run(
        [
            "pgbench",
            "-h",
            PLAIN_PG_HOST,
            "-p",
            str(PG_PORT),
            "-U",
            PG_USER,
            "-d",
            PG_DATABASE,
            "-n",
            "-M",
            "prepared",
            "-c",
            str(config.concurrency),
            "-j",
            str(config.pg_jobs),
            "-T",
            str(max(1, math.ceil(duration))),
            "--random-seed",
            str(seed),
            "-f",
            str(script_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        timeout=max(90, math.ceil(duration) + 60),
    )
    output = result.stdout
    tps_match = re.search(
        r"^tps = ([0-9]+(?:\.[0-9]+)?) "
        r"\(without initial connection time\)$",
        output,
        re.MULTILINE,
    )
    latency_match = re.search(
        r"^latency average = ([0-9]+(?:\.[0-9]+)?) ms$",
        output,
        re.MULTILINE,
    )
    transactions_match = re.search(
        r"^number of transactions actually processed: ([0-9]+)",
        output,
        re.MULTILINE,
    )
    failures_match = re.search(
        r"^number of failed transactions: ([0-9]+)", output, re.MULTILINE
    )
    if not tps_match or not latency_match or not transactions_match:
        raise ValueError(f"could not parse pgbench output:\n{output}")
    transactions_per_second = float(tps_match.group(1))
    transactions = int(transactions_match.group(1))
    failures = int(failures_match.group(1)) if failures_match else 0
    return {
        "successful_batches": transactions,
        "successful_operations": transactions * config.pipeline,
        "batch_transactions_per_second": transactions_per_second,
        "operations_per_second": transactions_per_second * config.pipeline,
        "batch_latency_average_ms": float(latency_match.group(1)),
        "failed_batches": failures,
    }


def run_postgres_reference(config: Config) -> dict[str, Any]:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="pg_local_cache_pgbench_", suffix=".sql"
    )
    script_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(pgbench_script(config))
        if config.warmup_seconds > 0:
            run_pgbench_once(config, script_path, config.warmup_seconds, 9001)
        runs = [
            run_pgbench_once(
                config, script_path, config.duration, 10000 + repetition
            )
            for repetition in range(config.repetitions)
        ]
    finally:
        script_path.unlink(missing_ok=True)
    summary = summarize_throughput_runs(runs)
    return {
        "client": "pgbench",
        "protocol": (
            "stock PostgreSQL extended protocol, prepared + pipeline"
        ),
        "server": "plain postgres:16.14 (pg_local_cache not loaded)",
        "operations_per_batch": config.pipeline,
        "runs": runs,
        "summary": summary,
    }


def read_cgroup(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"


def image_metadata() -> dict[str, dict[str, str]]:
    return {
        "pg_local_cache": {
            "image": os.environ.get(
                "PGLC_BENCH_PGLC_IMAGE", "pg_local_cache:benchmark"
            ),
            "base_image": os.environ.get(
                "PGLC_BENCH_POSTGRES_IMAGE", "postgres:16.14-bookworm"
            ),
            "identity": os.environ.get(
                "PGLC_BENCH_PG_LOCAL_CACHE_IMAGE_IDENTITY", "unknown"
            ),
            "base_identity": os.environ.get(
                "PGLC_BENCH_POSTGRES_IMAGE_IDENTITY", "unknown"
            ),
        },
        "postgres_plain": {
            "image": os.environ.get(
                "PGLC_BENCH_POSTGRES_IMAGE", "postgres:16.14-bookworm"
            ),
            "identity": os.environ.get(
                "PGLC_BENCH_POSTGRES_IMAGE_IDENTITY", "unknown"
            ),
        },
        "valkey": {
            "image": os.environ.get(
                "PGLC_BENCH_VALKEY_IMAGE", "valkey/valkey:9.1.1-trixie"
            ),
            "identity": os.environ.get(
                "PGLC_BENCH_VALKEY_IMAGE_IDENTITY", "unknown"
            ),
        },
        "redis": {
            "image": os.environ.get(
                "PGLC_BENCH_REDIS_IMAGE", "redis:8.8.1-trixie"
            ),
            "identity": os.environ.get(
                "PGLC_BENCH_REDIS_IMAGE_IDENTITY", "unknown"
            ),
        },
        "benchmark_client": {
            "image": os.environ.get(
                "PGLC_BENCH_RUNNER_IMAGE",
                "pg_local_cache-benchmark-runner:local",
            ),
            "identity": os.environ.get(
                "PGLC_BENCH_RUNNER_IMAGE_IDENTITY", "unknown"
            ),
            "source_revision": os.environ.get(
                "PGLC_BENCH_SOURCE_REVISION", "unknown"
            ),
            "harness_sha256": os.environ.get(
                "PGLC_BENCH_HARNESS_SHA256", "unknown"
            ),
        },
    }


def fmt_number(value: object, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_markdown(report: dict[str, Any]) -> str:
    workload = report["workload"]
    lines = [
        "# pg_local_cache comparative benchmark",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "This is a warm positive GET comparison. The RESP table uses one "
        "byte-identical Python/multiprocess client for all three targets. "
        "It is not a durability or transactional-invalidation comparison.",
        "",
        "## Workload",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        f"| Measured duration per run | {workload['duration_seconds']} s |",
        f"| Untimed warmup per run | {workload['warmup_seconds']} s |",
        f"| Repetitions | {workload['repetitions']} |",
        f"| Persistent connections | {workload['concurrency']} |",
        f"| Pipeline | {workload['pipeline']} |",
        f"| Keys | {workload['keys']} |",
        f"| Value bytes | {workload['value_size']} |",
        f"| Whole-run latency reservoir | "
        f"{workload['max_latency_samples']} samples |",
        f"| Server CPU quota | {workload['server_cpus']} |",
        f"| Client CPU quota | {workload['client_cpus']} |",
        f"| Client memory limit | {workload['client_memory']} |",
        f"| pg_local_cache workers | {workload['pg_local_cache_workers']} |",
        "",
        "## RESP warm GET",
        "",
    ]
    warnings = report.get("comparison_warnings", [])
    if warnings:
        lines.extend(
            (
                "> **Comparability warning:** " + " ".join(warnings),
                "",
            )
        )
    lines.extend(
        (
            "| Target | Version | Median ops/s | Min–max ops/s | CV | "
            "Pipeline-completion p50 | Pipeline-completion p95 | "
            "Pipeline-completion p99 | Errors |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    for name in ("pg_local_cache", "valkey", "redis"):
        target = report["resp_targets"][name]
        summary = target["summary"]
        errors = sum(int(run["errors"]) for run in target["runs"])
        lines.append(
            f"| {name} | {target['version']} | "
            f"{fmt_number(summary['median_operations_per_second'])} | "
            f"{fmt_number(summary['minimum_operations_per_second'])}–"
            f"{fmt_number(summary['maximum_operations_per_second'])} | "
            f"{fmt_number(summary['coefficient_of_variation_percent'], 2)}% | "
            f"{fmt_number(summary['median_p50_ms'], 3)} ms | "
            f"{fmt_number(summary['median_p95_ms'], 3)} ms | "
            f"{fmt_number(summary['median_p99_ms'], 3)} ms | {errors} |"
        )

    lines.extend(
        (
            "",
            "Individual RESP runs:",
            "",
            "| Target | Run | ops/s | p50 | p95 | p99 | "
            "Client quota CPU | Cache misses | SQL reads |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    for name in ("pg_local_cache", "valkey", "redis"):
        for index, run in enumerate(
            report["resp_targets"][name]["runs"], start=1
        ):
            lines.append(
                f"| {name} | {index} | "
                f"{fmt_number(run['operations_per_second'])} | "
                f"{fmt_number(run['p50_ms'], 3)} ms | "
                f"{fmt_number(run['p95_ms'], 3)} ms | "
                f"{fmt_number(run['p99_ms'], 3)} ms | "
                f"{fmt_number(run['client_cpu_quota_utilization_percent'], 1)}% | "
                f"{run.get('cache_misses_during_measurement', '—')} | "
                f"{run.get('database_reads_during_measurement', '—')} |"
            )

    postgres = report["postgres_reference"]
    lines.extend(
        (
            "",
            "## Direct stock PostgreSQL reference",
            "",
            "This section is deliberately separate: pgbench uses the "
            "PostgreSQL extended protocol against a separate stock PostgreSQL "
            "container and validates SQL errors, while the RESP harness "
            "validates every returned value. Statements in one pgbench "
            "pipeline share an implicit transaction/snapshot, so this "
            "amortizes more SQL overhead than independent transactions.",
            "",
            "| Client | Median value lookups/s | Min–max lookups/s | CV | "
            "Operations per pipeline batch |",
            "|---|---:|---:|---:|---:|",
            f"| pgbench prepared | "
            f"{fmt_number(postgres['summary']['median_operations_per_second'])} | "
            f"{fmt_number(postgres['summary']['minimum_operations_per_second'])}–"
            f"{fmt_number(postgres['summary']['maximum_operations_per_second'])} | "
            f"{fmt_number(postgres['summary']['coefficient_of_variation_percent'], 2)}% | "
            f"{postgres['operations_per_batch']} |",
            "",
            "## Reproducibility and interpretation",
            "",
        )
    )
    for name, metadata in report["images"].items():
        identity = metadata["identity"]
        lines.append(
            f"- `{name}`: `{metadata['image']}`, identity `{identity}`."
        )
        if name == "benchmark_client":
            lines.append(
                f"  Source `{metadata['source_revision']}`, harness SHA-256 "
                f"`{metadata['harness_sha256']}`."
            )
    lines.extend(
        (
            f"- Gate: **{report['gate']['status']}** — "
            f"{report['gate']['message']}",
            "- Valkey and Redis persistence is disabled for this cache-only "
            "read workload.",
            "- `pg_local_cache` uses the reported worker count; Valkey/Redis "
            "have different execution topologies. Set "
            "`PGLC_BENCH_PG_LOCAL_CACHE_WORKERS=1` for a one-worker lane.",
            "- CPU quotas are limits, not CPU affinity. All containers share "
            "the host, so hosted-runner results are noisy; use isolated, "
            "pinned CPUs for publication-quality claims.",
            "- RESP p50/p95/p99 measure time from sending a pipeline batch "
            "until each response completes, including queueing behind earlier "
            "responses. Deterministic per-connection Algorithm R reservoirs "
            "sample the entire measured interval; their merge is weighted by "
            "each connection's completed operations. These are not "
            "per-command server service times.",
            "- Valkey/Redis store an application-managed copy. "
            "`pg_local_cache` serves a PostgreSQL-owned row with transactional "
            "invalidation; this semantic difference is not represented by "
            "warm GET throughput.",
            "",
        )
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "failure.json").unlink(missing_ok=True)
    (output_directory / "failure.md").unlink(missing_ok=True)
    json_path = output_directory / "comparison.json"
    markdown_path = output_directory / "comparison.md"
    temporary_json = output_directory / ".comparison.json.tmp"
    temporary_markdown = output_directory / ".comparison.md.tmp"
    temporary_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_markdown.write_text(
        render_markdown(report), encoding="utf-8"
    )
    os.replace(temporary_json, json_path)
    os.replace(temporary_markdown, markdown_path)


def write_failure_report(error: BaseException) -> None:
    try:
        output_directory = Path(
            os.environ.get("PGLC_BENCH_OUTPUT_DIR", "/results")
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / "comparison.json").unlink(missing_ok=True)
        (output_directory / "comparison.md").unlink(missing_ok=True)
        payload = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "FAIL",
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "source_revision": os.environ.get(
                "PGLC_BENCH_SOURCE_REVISION", "unknown"
            ),
            "harness_sha256": os.environ.get(
                "PGLC_BENCH_HARNESS_SHA256", "unknown"
            ),
        }
        temporary = output_directory / ".failure.json.tmp"
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output_directory / "failure.json")
        markdown = (
            "# pg_local_cache comparative benchmark failed\n\n"
            f"- Error: `{type(error).__name__}: {error}`\n"
            f"- Source: `{payload['source_revision']}`\n"
            f"- Harness SHA-256: `{payload['harness_sha256']}`\n"
        )
        temporary_markdown = output_directory / ".failure.md.tmp"
        temporary_markdown.write_text(markdown, encoding="utf-8")
        os.replace(
            temporary_markdown, output_directory / "failure.md"
        )
    except Exception as report_error:
        print(
            f"could not write benchmark failure artifact: {report_error}",
            file=sys.stderr,
        )


def main() -> int:
    config = Config.from_environment()
    config.validate()
    if sys.platform != "linux":
        raise RuntimeError("comparative benchmark requires Linux/fork")

    expected = b"x" * config.value_size
    keys = [
        f"{NAMESPACE}:{key}".encode() for key in range(1, config.keys + 1)
    ]
    frames = [
        RespConnection.encode_command("GET", key) for key in keys
    ]

    print("setting up PostgreSQL mapping", flush=True)
    cache_capacity = setup_postgres(config)
    print("setting up plain PostgreSQL reference table", flush=True)
    setup_plain_postgres(config)
    wait_for_mapping(TARGETS[0], config, expected)
    for target in TARGETS[1:]:
        print(f"populating {target.name}", flush=True)
        populate_external_target(target, config, keys, expected)
    for target in TARGETS:
        print(f"warming all {config.keys} keys in {target.name}", flush=True)
        warm_all(target, config, frames, expected)

    versions: dict[str, str] = {}
    for target in TARGETS:
        info = read_info(target, config)
        versions[target.name] = info.get(target.version_field, "unknown")

    runs_by_target: dict[str, list[dict[str, Any]]] = {
        target.name: [] for target in TARGETS
    }
    integrity_failures: list[str] = []
    for repetition in range(config.repetitions):
        rotated = TARGETS[repetition % len(TARGETS) :] + TARGETS[
            : repetition % len(TARGETS)
        ]
        for target in rotated:
            print(
                f"run {repetition + 1}/{config.repetitions}: "
                f"{target.name} warmup",
                flush=True,
            )
            if config.warmup_seconds > 0:
                warmup = run_resp_load(
                    target,
                    config,
                    frames,
                    expected,
                    config.warmup_seconds,
                    False,
                )
                if warmup["errors"]:
                    raise RuntimeError(
                        f"{target.name} warmup errors: "
                        f"{warmup['error_messages']}"
                    )

            before = (
                read_pglc_stats(target, config)
                if target.name == "pg_local_cache"
                else {}
            )
            print(
                f"run {repetition + 1}/{config.repetitions}: "
                f"{target.name} measuring {config.duration:g}s",
                flush=True,
            )
            run = run_resp_load(
                target,
                config,
                frames,
                expected,
                config.duration,
                True,
            )
            if target.name == "pg_local_cache":
                after = read_pglc_stats(target, config)
                run["cache_misses_during_measurement"] = (
                    after.get("cache_misses", 0)
                    - before.get("cache_misses", 0)
                )
                run["database_reads_during_measurement"] = (
                    after.get("database_reads", 0)
                    - before.get("database_reads", 0)
                )
            runs_by_target[target.name].append(run)
            if run["errors"]:
                integrity_failures.append(
                    f"{target.name} run {repetition + 1} returned "
                    f"{run['errors']} errors"
                )
            if (
                run.get("cache_misses_during_measurement", 0) != 0
                or run.get("database_reads_during_measurement", 0) != 0
            ):
                integrity_failures.append(
                    f"pg_local_cache run {repetition + 1} was not fully warm"
                )

    resp_targets = {
        target.name: {
            "version": versions[target.name],
            "runs": runs_by_target[target.name],
            "summary": summarize_resp_runs(runs_by_target[target.name]),
        }
        for target in TARGETS
    }
    comparison_warnings: list[str] = []
    for target in TARGETS:
        peak_client_cpu = max(
            float(run["client_cpu_quota_utilization_percent"])
            for run in runs_by_target[target.name]
        )
        if peak_client_cpu >= 90:
            comparison_warnings.append(
                f"{target.name} reached {peak_client_cpu:.1f}% of the "
                "benchmark-client CPU quota; its throughput is a lower bound "
                "and must not be used for an engine ranking."
            )
    pglc_median = resp_targets["pg_local_cache"]["summary"][
        "median_operations_per_second"
    ]
    if pglc_median < config.min_ops:
        integrity_failures.append(
            f"pg_local_cache median {pglc_median:.0f} ops/s is below "
            f"the {config.min_ops:.0f} ops/s gate"
        )

    print("running separate direct PostgreSQL reference", flush=True)
    postgres_reference = run_postgres_reference(config)
    if any(
        int(run["failed_batches"]) != 0
        for run in postgres_reference["runs"]
    ):
        integrity_failures.append("direct PostgreSQL reference had failures")

    gate_status = "PASS" if not integrity_failures else "FAIL"
    gate_message = (
        f"pg_local_cache median >= {config.min_ops:.0f} ops/s, "
        "zero RESP errors, misses, and SQL reads"
        if not integrity_failures
        else "; ".join(integrity_failures)
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workload": {
            "operation": "warm positive GET",
            "duration_seconds": config.duration,
            "warmup_seconds": config.warmup_seconds,
            "repetitions": config.repetitions,
            "concurrency": config.concurrency,
            "pipeline": config.pipeline,
            "keys": config.keys,
            "value_size": config.value_size,
            "max_latency_samples": config.max_latency_samples,
            "server_cpus": config.server_cpus,
            "client_cpus": config.client_cpus,
            "client_memory": config.client_memory,
            "server_memory": config.server_memory,
            "pg_local_cache_workers": config.pg_local_cache_workers,
            "pg_local_cache_capacity": cache_capacity,
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "docker": os.environ.get(
                "PGLC_BENCH_DOCKER_VERSION", "unknown"
            ),
            "docker_compose": os.environ.get(
                "PGLC_BENCH_COMPOSE_VERSION", "unknown"
            ),
            "host_visible_cpu_count": os.cpu_count(),
            "cgroup_cpu_max": read_cgroup("/sys/fs/cgroup/cpu.max"),
            "cgroup_memory_max": read_cgroup(
                "/sys/fs/cgroup/memory.max"
            ),
            "postgres_server_version": psql(config, "SHOW server_version"),
            "plain_postgres_server_version": psql(
                config, "SHOW server_version", host=PLAIN_PG_HOST
            ),
        },
        "images": image_metadata(),
        "resp_methodology": {
            "client": "same stdlib Python multiprocess RESP2 client",
            "transport": "Docker bridge TCP",
            "persistent_connections": True,
            "authentication_before_timer": True,
            "preload_and_warm_outside_timer": True,
            "reply_validation": "every RESP value",
            "latency_definition": (
                "time from pipeline send until each response completion"
            ),
            "latency_sampling": (
                "deterministic per-connection Algorithm R reservoirs over the "
                "entire measured interval, operation-weighted when merged"
            ),
            "target_order": "rotated each repetition",
            "valkey_redis_persistence": "disabled",
        },
        "resp_targets": resp_targets,
        "comparison_warnings": comparison_warnings,
        "postgres_reference": postgres_reference,
        "gate": {"status": gate_status, "message": gate_message},
    }
    write_report(report, config.output_directory)
    print(render_markdown(report), flush=True)
    return 0 if not integrity_failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("benchmark interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"benchmark failed: {error}", file=sys.stderr)
        write_failure_report(error)
        raise
