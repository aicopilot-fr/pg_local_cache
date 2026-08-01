#!/usr/bin/env python3
"""Whole-row/KVik benchmarks kept separate from the scalar comparison.

The RESP comparison sends the same GET frames and validates the exact,
per-key full-row JSON bytes on pg_local_cache, Valkey, and Redis.  Ordinary
SQL lanes use the same SELECT text against mapped and stock PostgreSQL.  No
number emitted here is pooled with the historical scalar ``comparison.json``.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import queue
import random
import resource
import statistics
import sys
import tempfile
import time
import traceback
from typing import Any

import compare
import scenarios


ROW_NAMESPACE = "whole_row_comparison"
ROW_TABLE = "pg_local_cache_whole_row_comparison"
ROW_TENANT = 7
ROW_VALUE_SIZE_ENV = "PGLC_BENCH_ROW_VALUE_SIZE"
ROW_PAYLOAD_SIZES_ENV = "PGLC_BENCH_ROW_PAYLOAD_SIZES"
ROW_RESP_MIN_OPS_ENV = "PGLC_BENCH_ROW_RESP_MIN_OPS"
ROW_SQL_MIN_OPS_ENV = "PGLC_BENCH_ROW_SQL_MIN_OPS"
ROW_WIDTH_MIN_OPS_ENV = "PGLC_BENCH_ROW_WIDTH_MIN_OPS"
MAX_CACHEABLE_TEXT_SIZE = 3000

SQL_LANES = {
    "select_star": (
        f"SELECT * FROM public.{ROW_TABLE} "
        "WHERE tenant_id = 7 AND id = :key;"
    ),
    "reordered_projection": (
        "SELECT metadata, payload, enabled, amount, note, id, tenant_id "
        f"FROM public.{ROW_TABLE} "
        "WHERE tenant_id = 7 AND id = :key;"
    ),
    "composite_predicate_reordered": (
        "SELECT payload, metadata, id, tenant_id "
        f"FROM public.{ROW_TABLE} "
        "WHERE id = :key AND tenant_id = 7;"
    ),
}


@dataclass(frozen=True)
class WholeRowConfig:
    base: compare.Config
    duration: float
    repetitions: int
    value_size: int
    payload_sizes: tuple[int, ...]
    resp_min_ops: float
    sql_min_ops: float
    width_min_ops: float

    @classmethod
    def from_environment(cls) -> "WholeRowConfig":
        base = compare.Config.from_environment()
        value_size = compare.env_int(
            ROW_VALUE_SIZE_ENV,
            min(base.value_size, 2048),
            1,
            MAX_CACHEABLE_TEXT_SIZE,
        )
        payload_sizes = parse_payload_sizes(
            os.environ.get(ROW_PAYLOAD_SIZES_ENV, "64,512,2048")
        )
        return cls(
            base=base,
            duration=compare.env_float(
                "PGLC_BENCH_ROW_DURATION",
                min(base.duration, 30.0),
                1,
                3600,
            ),
            repetitions=compare.env_int(
                "PGLC_BENCH_ROW_REPETITIONS", base.repetitions, 1, 20
            ),
            value_size=value_size,
            payload_sizes=payload_sizes,
            resp_min_ops=compare.env_float(
                ROW_RESP_MIN_OPS_ENV, 10_000, 0, 1e12
            ),
            sql_min_ops=compare.env_float(
                ROW_SQL_MIN_OPS_ENV, 10_000, 0, 1e12
            ),
            width_min_ops=compare.env_float(
                ROW_WIDTH_MIN_OPS_ENV, 0, 0, 1e12
            ),
        )

    def load_config(self) -> compare.Config:
        return replace(
            self.base,
            duration=self.duration,
            repetitions=self.repetitions,
            warmup_seconds=min(self.base.warmup_seconds, 5.0),
        )


def parse_payload_sizes(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            raise ValueError(f"{ROW_PAYLOAD_SIZES_ENV} contains an empty item")
        try:
            value = int(item)
        except ValueError as error:
            raise ValueError(
                f"{ROW_PAYLOAD_SIZES_ENV} must be comma-separated integers"
            ) from error
        if not 1 <= value <= MAX_CACHEABLE_TEXT_SIZE:
            raise ValueError(
                f"{ROW_PAYLOAD_SIZES_ENV} values must be between 1 and "
                f"{MAX_CACHEABLE_TEXT_SIZE}"
            )
        if value not in values:
            values.append(value)
    if not values:
        raise ValueError(f"{ROW_PAYLOAD_SIZES_ENV} must not be empty")
    return tuple(values)


def row_key(row_id: int) -> bytes:
    # Deliberately reverse JSON fields relative to the primary-key definition.
    return (
        f"CRUD:{compare.PG_DATABASE}.public.{ROW_TABLE}:"
        f'{{"id":{row_id},"tenant_id":{ROW_TENANT}}}'
    ).encode()


def setup_role(config: compare.Config, host: str) -> None:
    password = config.pg_password.replace("'", "''")
    compare.psql(
        config,
        "DO $pglc$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = "
        f"'{compare.PG_APP_USER}') THEN "
        f"EXECUTE 'CREATE ROLE {compare.PG_APP_USER} LOGIN PASSWORD "
        f"''{password}'' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
        "NOREPLICATION NOBYPASSRLS'; END IF; END $pglc$;",
        host=host,
    )


def row_table_sql(keys: int, payload_size: int) -> str:
    return (
        f"DROP TABLE IF EXISTS public.{ROW_TABLE};"
        f"CREATE TABLE public.{ROW_TABLE} ("
        "tenant_id bigint NOT NULL, id bigint NOT NULL, payload text NOT NULL, "
        "amount numeric(18,2), enabled boolean NOT NULL, metadata jsonb NOT NULL, "
        "note text, PRIMARY KEY (tenant_id, id));"
        f"INSERT INTO public.{ROW_TABLE} "
        "SELECT 7, g, repeat('x', "
        f"{payload_size}), (g % 10000)::numeric / 100, (g % 2 = 0), "
        "pg_catalog.jsonb_build_object('bucket', g % 16, 'active', g % 2 = 0), "
        "CASE WHEN g % 3 = 0 THEN NULL ELSE 'note-' || g::text END "
        f"FROM pg_catalog.generate_series(1, {keys}) AS g;"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{ROW_TABLE} "
        f"TO {compare.PG_APP_USER};"
        f"ANALYZE public.{ROW_TABLE};"
    )


def setup_mapped_postgres(
    config: compare.Config, payload_size: int
) -> int:
    setup_role(config, compare.PG_HOST)
    compare.psql(
        config,
        f"SELECT local_cache.unregister_mapping('{ROW_NAMESPACE}');"
        + row_table_sql(config.keys, payload_size)
        + f"SELECT local_cache.attach_table("
        f"'public.{ROW_TABLE}'::regclass, false, '{ROW_NAMESPACE}');",
    )
    capacity = int(compare.psql(config, "SHOW pg_local_cache.cache_entries"))
    if config.keys > capacity:
        raise ValueError(
            f"whole-row keyspace {config.keys} exceeds cache capacity {capacity}"
        )
    return capacity


def setup_plain_postgres(config: compare.Config, payload_size: int) -> None:
    setup_role(config, compare.PLAIN_PG_HOST)
    compare.psql(
        config,
        row_table_sql(config.keys, payload_size),
        host=compare.PLAIN_PG_HOST,
    )


def fetch_expected_rows(config: compare.Config) -> list[bytes]:
    output = compare.psql(
        config,
        f"SELECT id::text || E'\\t' || pg_catalog.row_to_json(r)::text "
        f"FROM public.{ROW_TABLE} AS r ORDER BY id",
    )
    expected: list[bytes] = [b""] * config.keys
    for line in output.splitlines():
        raw_id, raw_json = line.split("\t", 1)
        row_id = int(raw_id)
        expected[row_id - 1] = raw_json.encode()
    if any(not value for value in expected):
        raise RuntimeError("PostgreSQL did not return every benchmark row")
    return expected


def wait_for_mapping(
    target: compare.Target,
    config: compare.Config,
    expected: list[bytes],
) -> None:
    deadline = time.monotonic() + 30
    last: object = None
    while time.monotonic() < deadline:
        connection: compare.RespConnection | None = None
        try:
            connection = compare.RespConnection(target, config.auth_token)
            last = connection.command("GET", row_key(1))
            if last == expected[0]:
                return
        except (OSError, compare.RespError) as error:
            last = error
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.05)
    raise TimeoutError(f"whole-row mapping did not become ready: {last!r}")


def populate_external_target(
    target: compare.Target,
    config: compare.Config,
    expected: list[bytes],
) -> None:
    connection = compare.RespConnection(target, config.auth_token)
    try:
        if connection.command("FLUSHDB") != "OK":
            raise AssertionError(f"{target.name} FLUSHDB failed")
        batch_size = min(256, config.pipeline * 4)
        for start in range(0, config.keys, batch_size):
            stop = min(start + batch_size, config.keys)
            frames = [
                compare.RespConnection.encode_command(
                    "SET", row_key(index + 1), expected[index]
                )
                for index in range(start, stop)
            ]
            connection.socket.sendall(b"".join(frames))
            for _ in frames:
                if connection.read_response() != "OK":
                    raise AssertionError(f"{target.name} SET failed")
        if connection.command("DBSIZE") != config.keys:
            raise AssertionError(f"{target.name} row count is incomplete")
    finally:
        connection.close()


def get_frames(config: compare.Config) -> list[bytes]:
    return [
        compare.RespConnection.encode_command("GET", row_key(row_id))
        for row_id in range(1, config.keys + 1)
    ]


def build_variable_batches(
    frames: list[bytes], worker_index: int, config: compare.Config
) -> list[tuple[bytes, tuple[int, ...]]]:
    batches: list[tuple[bytes, tuple[int, ...]]] = []
    index = worker_index % len(frames)
    cycle_length = len(frames) // math.gcd(len(frames), config.concurrency)
    batch_count = math.ceil(cycle_length / config.pipeline)
    for _ in range(batch_count):
        indexes: list[int] = []
        commands: list[bytes] = []
        for _ in range(config.pipeline):
            indexes.append(index)
            commands.append(frames[index])
            index = (index + config.concurrency) % len(frames)
        batches.append((b"".join(commands), tuple(indexes)))
    return batches


def variable_worker(
    worker_index: int,
    target: compare.Target,
    config: compare.Config,
    frames: list[bytes],
    expected: list[bytes],
    ready: Any,
    start_event: Any,
    deadline_value: Any,
    result_queue: Any,
    collect_latencies: bool,
) -> None:
    result = compare.WorkerResult(worker_index=worker_index)
    connection: compare.RespConnection | None = None
    batches: list[tuple[bytes, tuple[int, ...]]] = []
    try:
        connection = compare.RespConnection(target, config.auth_token)
        batches = build_variable_batches(frames, worker_index, config)
    except Exception as error:
        result.errors += 1
        result.messages.append(f"connect/setup: {error}")

    try:
        ready.wait(timeout=compare.SOCKET_TIMEOUT + 30)
        if not start_event.wait(timeout=compare.SOCKET_TIMEOUT + 30):
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

    capacity, extra = divmod(config.max_latency_samples, config.concurrency)
    sample_capacity = capacity + (1 if worker_index < extra else 0)
    generator = random.Random(compare.LATENCY_RESERVOIR_SEED ^ worker_index)
    batch_index = 0
    try:
        while time.perf_counter() < deadline_value.value:
            frame, indexes = batches[batch_index]
            batch_index = (batch_index + 1) % len(batches)
            sent_at = time.perf_counter_ns()
            connection.socket.sendall(frame)
            for index in indexes:
                response = connection.read_response()
                completed_at = time.perf_counter_ns()
                if response != expected[index]:
                    result.errors += 1
                    if len(result.messages) < 5:
                        result.messages.append(
                            f"key {index + 1} returned {response!r}, "
                            f"expected {expected[index]!r}"
                        )
                    continue
                result.completed += 1
                if collect_latencies:
                    compare.add_reservoir_sample(
                        result.latencies_ms,
                        (completed_at - sent_at) / 1_000_000,
                        result.completed,
                        sample_capacity,
                        generator,
                    )
    except Exception as error:
        result.errors += 1
        if len(result.messages) < 5:
            result.messages.append(str(error))
    finally:
        connection.close()
        result.finished_at = time.perf_counter()
        result_queue.put(result)


def run_variable_resp_load(
    target: compare.Target,
    config: compare.Config,
    frames: list[bytes],
    expected: list[bytes],
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
            target=variable_worker,
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
            name=f"{target.name}-whole-row-{index}",
        )
        for index in range(config.concurrency)
    ]
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    for process in processes:
        process.start()
    try:
        ready.wait(timeout=compare.SOCKET_TIMEOUT + 30)
    except Exception as error:
        for process in processes:
            process.terminate()
            process.join(timeout=5)
        raise RuntimeError("whole-row workers did not become ready") from error

    started_at = time.perf_counter()
    deadline_value.value = started_at + duration
    start_event.set()
    results: list[compare.WorkerResult] = []
    receive_deadline = time.monotonic() + duration + compare.SOCKET_TIMEOUT + 30
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
                compare.WorkerResult(
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
    returned = sum(result.worker_index >= 0 for result in results)
    if returned < config.concurrency:
        results.append(
            compare.WorkerResult(
                worker_index=-1,
                errors=config.concurrency - returned,
                messages=["one or more worker results were not returned"],
                finished_at=time.perf_counter(),
            )
        )

    finished_at = max(
        (result.finished_at for result in results), default=started_at
    )
    elapsed = max(finished_at - started_at, 1e-9)
    completed = sum(result.completed for result in results)
    weighted = sorted(
        (
            latency,
            result.completed / len(result.latencies_ms),
        )
        for result in results
        if result.latencies_ms
        for latency in result.latencies_ms
    )
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = max(
        compare.usage_seconds(child_after) - compare.usage_seconds(child_before),
        0.0,
    )
    return {
        "successful_operations": completed,
        "elapsed_seconds": elapsed,
        "operations_per_second": completed / elapsed,
        "p50_ms": compare.weighted_percentile(weighted, 50),
        "p95_ms": compare.weighted_percentile(weighted, 95),
        "p99_ms": compare.weighted_percentile(weighted, 99),
        "latency_samples": len(weighted),
        "errors": sum(result.errors for result in results),
        "error_messages": [
            message for result in results for message in result.messages
        ][:20],
        "client_cpu_seconds": cpu_seconds,
        "client_cpu_quota_utilization_percent": (
            cpu_seconds / elapsed / config.client_cpus * 100
        ),
    }


def warm_variable(
    target: compare.Target,
    config: compare.Config,
    frames: list[bytes],
    expected: list[bytes],
) -> None:
    connection = compare.RespConnection(target, config.auth_token)
    try:
        for start in range(0, config.keys, min(256, config.pipeline * 4)):
            stop = min(start + min(256, config.pipeline * 4), config.keys)
            connection.socket.sendall(b"".join(frames[start:stop]))
            for index in range(start, stop):
                response = connection.read_response()
                if response != expected[index]:
                    raise AssertionError(
                        f"{target.name} warm key {index + 1} returned {response!r}"
                    )
    finally:
        connection.close()


def reset_pg_local_cache(config: compare.Config) -> int:
    """Remove entries left by earlier benchmark lanes before the warm pass."""
    target = compare.TARGETS[0]
    connection = compare.RespConnection(target, config.auth_token)
    try:
        removed = connection.command("INVALIDATE", "CRUD")
        if not isinstance(removed, int) or isinstance(removed, bool) or removed < 0:
            raise RuntimeError(
                f"{target.name} global invalidation returned {removed!r}"
            )
        return removed
    finally:
        connection.close()


def stabilize_pg_local_cache(
    config: compare.Config,
    frames: list[bytes],
    expected: list[bytes],
    max_passes: int = 16,
) -> dict[str, int]:
    """Warm until a complete verification pass performs no database reads."""
    if max_passes <= 0:
        raise ValueError("max_passes must be positive")
    target = compare.TARGETS[0]
    total_misses = 0
    total_reads = 0
    last_misses = 0
    last_reads = 0
    for pass_number in range(1, max_passes + 1):
        before = compare.read_pglc_stats(target, config)
        warm_variable(target, config, frames, expected)
        after = compare.read_pglc_stats(target, config)
        last_misses = after.get("cache_misses", 0) - before.get(
            "cache_misses", 0
        )
        last_reads = after.get("database_reads", 0) - before.get(
            "database_reads", 0
        )
        total_misses += last_misses
        total_reads += last_reads
        if last_misses == 0 and last_reads == 0:
            return {
                "passes": pass_number,
                "cache_misses_before_stable": total_misses,
                "database_reads_before_stable": total_reads,
            }
    raise RuntimeError(
        "pg_local_cache did not reach a fully warm verification pass after "
        f"{max_passes} passes (last misses={last_misses}, reads={last_reads})"
    )


def resp_comparison(
    whole: WholeRowConfig,
    config: compare.Config,
    expected: list[bytes],
) -> tuple[dict[str, Any], list[str]]:
    frames = get_frames(config)
    for target in compare.TARGETS[1:]:
        populate_external_target(target, config, expected)
    reset_pg_local_cache(config)
    stabilization = stabilize_pg_local_cache(config, frames, expected)
    for target in compare.TARGETS[1:]:
        warm_variable(target, config, frames, expected)

    versions: dict[str, str] = {}
    for target in compare.TARGETS:
        versions[target.name] = compare.read_info(target, config).get(
            target.version_field, "unknown"
        )
    runs: dict[str, list[dict[str, Any]]] = {
        target.name: [] for target in compare.TARGETS
    }
    failures: list[str] = []
    for repetition in range(config.repetitions):
        rotated = compare.TARGETS[repetition % len(compare.TARGETS) :] + compare.TARGETS[
            : repetition % len(compare.TARGETS)
        ]
        for target in rotated:
            if config.warmup_seconds > 0:
                warmup = run_variable_resp_load(
                    target,
                    config,
                    frames,
                    expected,
                    config.warmup_seconds,
                    False,
                )
                if warmup["errors"]:
                    raise RuntimeError(f"{target.name} row warmup failed: {warmup}")
            before = (
                compare.read_pglc_stats(target, config)
                if target.name == "pg_local_cache"
                else {}
            )
            run = run_variable_resp_load(
                target, config, frames, expected, config.duration, True
            )
            if target.name == "pg_local_cache":
                after = compare.read_pglc_stats(target, config)
                run["cache_misses_during_measurement"] = (
                    after.get("cache_misses", 0) - before.get("cache_misses", 0)
                )
                run["database_reads_during_measurement"] = (
                    after.get("database_reads", 0) - before.get("database_reads", 0)
                )
                if (
                    run["cache_misses_during_measurement"] != 0
                    or run["database_reads_during_measurement"] != 0
                ):
                    failures.append(
                        f"whole-row RESP run {repetition + 1} was not fully warm"
                    )
            runs[target.name].append(run)
            if run["errors"]:
                failures.append(
                    f"{target.name} whole-row run {repetition + 1} returned "
                    f"{run['errors']} errors"
                )
    targets = {
        target.name: {
            "version": versions[target.name],
            "runs": runs[target.name],
            "summary": compare.summarize_resp_runs(runs[target.name]),
        }
        for target in compare.TARGETS
    }
    median = targets["pg_local_cache"]["summary"][
        "median_operations_per_second"
    ]
    if median < whole.resp_min_ops:
        failures.append(
            f"whole-row RESP median {median:.0f} ops/s is below independent "
            f"gate {whole.resp_min_ops:.0f} ops/s"
        )
    return (
        {
            "payload_text_bytes": whole.value_size,
            "response_bytes_min": min(map(len, expected)),
            "response_bytes_max": max(map(len, expected)),
            "key_format": (
                "CRUD:database.schema.table:{primary-key-json}; JSON object "
                "order deliberately differs from PK order"
            ),
            "pg_local_cache_warm_stabilization": stabilization,
            "targets": targets,
            "gate": {
                "minimum_pg_local_cache_ops_per_second": whole.resp_min_ops,
                "status": "PASS" if not failures else "FAIL",
            },
        },
        failures,
    )


def sql_lookup_script(query: str, config: compare.Config) -> str:
    return scenarios.lookup_script(query, config.keys, config.pipeline)


def validate_sql_value(
    config: compare.Config, host: str, query: str, key: int = 1
) -> str:
    return compare.psql(config, query.replace(":key", str(key)), host=host)


def sql_lanes(
    config: compare.Config, minimum_ops: float
) -> tuple[dict[str, Any], list[str]]:
    lanes: dict[str, Any] = {}
    failures: list[str] = []
    for index, (name, query) in enumerate(SQL_LANES.items()):
        mapped_value = validate_sql_value(config, compare.PG_HOST, query)
        plain_value = validate_sql_value(config, compare.PLAIN_PG_HOST, query)
        if mapped_value != plain_value:
            raise RuntimeError(
                f"SQL lane {name} returned different mapped/plain values"
            )
        script = sql_lookup_script(query, config)
        before = compare.read_pglc_stats(compare.TARGETS[0], config)
        mapped = scenarios.run_pgbench_repetitions(
            config,
            compare.PG_HOST,
            script,
            31000 + index * 100,
        )
        after = compare.read_pglc_stats(compare.TARGETS[0], config)
        stock = scenarios.run_pgbench_repetitions(
            config,
            compare.PLAIN_PG_HOST,
            script,
            41000 + index * 100,
        )
        mapped.update(
            scenarios.counter_delta(
                before,
                after,
                "sql_cache_hits",
                "sql_cache_misses",
                "sql_cache_fills",
                "sql_cache_bypasses",
            )
        )
        lanes[name] = {
            "query": query,
            "validated_sample": mapped_value,
            "mapped_postgres": mapped,
            "stock_postgres": stock,
        }
        if any(int(run["failed_batches"]) for run in mapped["runs"]):
            failures.append(f"mapped SQL lane {name} had failed batches")
        if any(int(run["failed_batches"]) for run in stock["runs"]):
            failures.append(f"stock SQL lane {name} had failed batches")
        measured_operations = sum(
            int(run["successful_operations"]) for run in mapped["runs"]
        )
        if mapped["sql_cache_hits_during_measurement"] < measured_operations:
            failures.append(
                f"mapped SQL lane {name} did not hit for every measured lookup"
            )
        for counter in (
            "sql_cache_misses_during_measurement",
            "sql_cache_fills_during_measurement",
            "sql_cache_bypasses_during_measurement",
        ):
            if mapped[counter] != 0:
                failures.append(
                    f"mapped SQL lane {name} reported {counter}={mapped[counter]}"
                )
        median = mapped["summary"]["median_operations_per_second"]
        if median < minimum_ops:
            failures.append(
                f"mapped SQL lane {name} median {median:.0f} ops/s is below "
                f"independent gate {minimum_ops:.0f}"
            )
    return lanes, failures


def width_sweep(
    whole: WholeRowConfig, config: compare.Config
) -> tuple[dict[str, Any], list[str]]:
    target = compare.TARGETS[0]
    lanes: dict[str, Any] = {}
    failures: list[str] = []
    for payload_size in whole.payload_sizes:
        setup_mapped_postgres(config, payload_size)
        expected = fetch_expected_rows(config)
        wait_for_mapping(target, config, expected)
        frames = get_frames(config)
        warm_variable(target, config, frames, expected)
        runs: list[dict[str, Any]] = []
        for repetition in range(config.repetitions):
            if config.warmup_seconds > 0:
                warmup = run_variable_resp_load(
                    target,
                    config,
                    frames,
                    expected,
                    config.warmup_seconds,
                    False,
                )
                if warmup["errors"]:
                    raise RuntimeError(
                        f"width {payload_size} warmup failed: {warmup}"
                    )
            before = compare.read_pglc_stats(target, config)
            run = run_variable_resp_load(
                target, config, frames, expected, config.duration, True
            )
            after = compare.read_pglc_stats(target, config)
            run.update(
                scenarios.counter_delta(
                    before, after, "cache_misses", "database_reads"
                )
            )
            run["repetition"] = repetition + 1
            runs.append(run)
            if run["errors"]:
                failures.append(
                    f"width {payload_size} run {repetition + 1} had errors"
                )
            if (
                run["cache_misses_during_measurement"] != 0
                or run["database_reads_during_measurement"] != 0
            ):
                failures.append(
                    f"width {payload_size} run {repetition + 1} was not warm"
                )
        summary = compare.summarize_resp_runs(runs)
        lanes[str(payload_size)] = {
            "payload_text_bytes": payload_size,
            "response_bytes_min": min(map(len, expected)),
            "response_bytes_max": max(map(len, expected)),
            "runs": runs,
            "summary": summary,
        }
        if summary["median_operations_per_second"] < whole.width_min_ops:
            failures.append(
                f"width {payload_size} median "
                f"{summary['median_operations_per_second']:.0f} ops/s is below "
                f"width gate {whole.width_min_ops:.0f}"
            )
    return lanes, failures


def fmt(value: object, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_markdown(report: dict[str, Any]) -> str:
    resp = report["resp_full_row"]
    lines = [
        "# pg_local_cache whole-row benchmark",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "These results are deliberately separate from the historical scalar "
        "benchmark. Every RESP target receives the same key stream and the "
        "same byte-identical, per-key full-row JSON values.",
        "",
        "## Full-row RESP GET",
        "",
        "| Target | Median ops/s | Min-max ops/s | p99 | Errors |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("pg_local_cache", "valkey", "redis"):
        target = resp["targets"][name]
        summary = target["summary"]
        errors = sum(int(run["errors"]) for run in target["runs"])
        lines.append(
            f"| {name} | {fmt(summary['median_operations_per_second'])} | "
            f"{fmt(summary['minimum_operations_per_second'])}-"
            f"{fmt(summary['maximum_operations_per_second'])} | "
            f"{fmt(summary['median_p99_ms'], 3)} ms | {errors} |"
        )
    lines.extend(
        (
            "",
            "## Ordinary SQL whole-row/projection lanes",
            "",
            "| Lane | Mapped PostgreSQL median ops/s | Stock PostgreSQL median ops/s |",
            "|---|---:|---:|",
        )
    )
    for name, lane in report["ordinary_sql"].items():
        lines.append(
            f"| {name} | "
            f"{fmt(lane['mapped_postgres']['summary']['median_operations_per_second'])} | "
            f"{fmt(lane['stock_postgres']['summary']['median_operations_per_second'])} |"
        )
    lines.extend(
        (
            "",
            "## pg_local_cache full-row response-width sweep",
            "",
            "| Text payload bytes | JSON response bytes | Median ops/s | p99 |",
            "|---:|---:|---:|---:|",
        )
    )
    for lane in report["resp_payload_width_sweep"].values():
        summary = lane["summary"]
        lines.append(
            f"| {lane['payload_text_bytes']} | "
            f"{lane['response_bytes_min']}-{lane['response_bytes_max']} | "
            f"{fmt(summary['median_operations_per_second'])} | "
            f"{fmt(summary['median_p99_ms'], 3)} ms |"
        )
    lines.extend(
        (
            "",
            f"Overall gate: **{report['gate']['status']}** — "
            f"{report['gate']['message']}",
            "",
            "RESP values are PostgreSQL `row_to_json` bytes. Valkey and Redis "
            "store exactly those bytes; pg_local_cache derives them from the "
            "authoritative table and maintains transactional invalidation.",
            "SQL values are validated before timing. Prepared/pipelined pgbench "
            "uses identical SELECT text against mapped and stock PostgreSQL.",
            "",
        )
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "whole-row-failure.json").unlink(missing_ok=True)
    (output / "whole-row-failure.md").unlink(missing_ok=True)
    json_tmp = output / ".whole-row.json.tmp"
    markdown_tmp = output / ".whole-row.md.tmp"
    json_tmp.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_tmp.write_text(render_markdown(report), encoding="utf-8")
    os.replace(json_tmp, output / "whole-row.json")
    os.replace(markdown_tmp, output / "whole-row.md")


def write_failure_report(error: BaseException, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "whole-row.json").unlink(missing_ok=True)
    (output / "whole-row.md").unlink(missing_ok=True)
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
            "PGLC_BENCH_WHOLE_ROW_HARNESS_SHA256", "unknown"
        ),
    }
    temporary = output / ".whole-row-failure.json.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output / "whole-row-failure.json")
    markdown = output / ".whole-row-failure.md.tmp"
    markdown.write_text(
        "# Whole-row benchmark failed\n\n"
        f"- Error: `{type(error).__name__}: {error}`\n",
        encoding="utf-8",
    )
    os.replace(markdown, output / "whole-row-failure.md")


def main() -> int:
    whole = WholeRowConfig.from_environment()
    config = whole.load_config()
    if sys.platform != "linux":
        raise RuntimeError("whole-row benchmark requires Linux/fork")

    capacity = setup_mapped_postgres(config, whole.value_size)
    setup_plain_postgres(config, whole.value_size)
    expected = fetch_expected_rows(config)
    wait_for_mapping(compare.TARGETS[0], config, expected)

    resp, resp_failures = resp_comparison(whole, config, expected)
    sql, sql_failures = sql_lanes(config, whole.sql_min_ops)
    widths, width_failures = width_sweep(whole, config)
    failures = resp_failures + sql_failures + width_failures
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workload": {
            "duration_seconds": config.duration,
            "warmup_seconds": config.warmup_seconds,
            "repetitions": config.repetitions,
            "concurrency": config.concurrency,
            "pipeline": config.pipeline,
            "keys": config.keys,
            "cache_capacity": capacity,
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "source_revision": os.environ.get(
                "PGLC_BENCH_SOURCE_REVISION", "unknown"
            ),
            "harness_sha256": os.environ.get(
                "PGLC_BENCH_WHOLE_ROW_HARNESS_SHA256", "unknown"
            ),
        },
        "methodology": {
            "scalar_results_reused": False,
            "resp_client": "same stdlib multiprocess RESP2 client for all targets",
            "resp_reply_validation": "every response against exact per-key bytes",
            "external_payload_source": "PostgreSQL row_to_json output",
            "pg_local_cache_warm_reset": (
                "global invalidation followed by counter-verified full-keyspace "
                "passes until one pass has zero misses and database reads"
            ),
            "ordinary_sql_protocol": "pgbench prepared extended protocol + pipeline",
            "ordinary_sql_comparison": "identical SELECT text, mapped vs stock PostgreSQL",
        },
        "resp_full_row": resp,
        "ordinary_sql": sql,
        "ordinary_sql_gate": {
            "minimum_mapped_ops_per_second": whole.sql_min_ops,
            "status": "PASS" if not sql_failures else "FAIL",
        },
        "resp_payload_width_sweep": widths,
        "width_gate": {
            "minimum_ops_per_second": whole.width_min_ops,
            "status": "PASS" if not width_failures else "FAIL",
        },
        "gate": {
            "status": "PASS" if not failures else "FAIL",
            "message": "all independent whole-row gates and integrity checks passed"
            if not failures
            else "; ".join(failures),
        },
    }
    write_report(report, config.output_directory)
    print(render_markdown(report), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    output_directory = Path(
        os.environ.get("PGLC_BENCH_OUTPUT_DIR", "/results")
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"whole-row benchmark failed: {error}", file=sys.stderr)
        write_failure_report(error, output_directory)
        raise
