#!/usr/bin/env python3
"""Shared benchmark primitives for whole-row RESP and PostgreSQL lanes."""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import random
import resource
import socket
import statistics
import subprocess
from typing import Any


PG_HOST = "pg-local-cache"
PLAIN_PG_HOST = "postgres-plain"
PG_PORT = 5432
PG_DATABASE = "benchmark"
PG_USER = "postgres"
PG_APP_USER = "local_cache_benchmark_app"
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
        client_cpus = env_float("PGLC_BENCH_CLIENT_CPUS", 4, 0.1, 1024)
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
                200_000,
                1000,
                10_000_000,
            ),
            client_cpus=client_cpus,
            server_cpus=env_float("PGLC_BENCH_SERVER_CPUS", 4, 0.1, 1024),
            client_memory=os.environ.get("PGLC_BENCH_CLIENT_MEMORY", "3g"),
            server_memory=os.environ.get("PGLC_BENCH_SERVER_MEMORY", "2g"),
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
                "key count must be divisible by the connection count"
            )
        keys_per_connection = self.keys // self.concurrency
        if keys_per_connection % self.pipeline != 0:
            raise ValueError(
                "keys per connection must be divisible by pipeline depth"
            )
        maximum_clients = self.pg_local_cache_workers * 128
        if self.concurrency > maximum_clients:
            raise ValueError(
                f"{self.concurrency} connections exceed pg_local_cache "
                f"capacity {maximum_clients}"
            )


def read_text_if_present(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def discover_runtime_resources() -> dict[str, Any]:
    cpu_model = "unknown"
    cpu_info = read_text_if_present(Path("/proc/cpuinfo")) or ""
    for line in cpu_info.splitlines():
        field, separator, value = line.partition(":")
        if separator and field.strip() in ("model name", "Hardware", "Processor"):
            if value.strip():
                cpu_model = value.strip()
                break

    cpu_max = read_text_if_present(Path("/sys/fs/cgroup/cpu.max"))
    memory_max = read_text_if_present(Path("/sys/fs/cgroup/memory.max"))
    quota_cores: float | None = None
    if cpu_max:
        fields = cpu_max.split()
        if len(fields) == 2 and fields[0] != "max":
            try:
                quota = int(fields[0])
                period = int(fields[1])
                if quota > 0 and period > 0:
                    quota_cores = quota / period
            except ValueError:
                pass

    memory_limit_bytes: int | None = None
    if memory_max and memory_max != "max":
        try:
            value = int(memory_max)
            if value > 0:
                memory_limit_bytes = value
        except ValueError:
            pass

    return {
        "logical_cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
        "cgroup_v2": {
            "cpu.max": cpu_max or "unavailable",
            "cpu_quota_cores": quota_cores,
            "memory.max": memory_max or "unavailable",
            "memory_limit_bytes": memory_limit_bytes,
        },
    }


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
    """Small buffered RESP2 client shared by every RESP target."""

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
    """Add one observation using Algorithm R reservoir sampling."""
    if seen <= 0 or capacity <= 0:
        raise ValueError("reservoir counters must be positive")
    if len(samples) < capacity:
        samples.append(value)
        return
    replacement = generator.randrange(seen)
    if replacement < capacity:
        samples[replacement] = value


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
