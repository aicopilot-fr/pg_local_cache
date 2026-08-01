#!/usr/bin/env python3
"""Extended, semantics-aware pg_local_cache benchmark scenarios.

The existing ``compare.py`` suite remains the publication-quality warm GET
comparison.  This companion suite measures paths which must not be folded
into that number: cold fills, same-key fan-in, mutations, SQL reads, and SQL
write/invalidation.  Every scenario validates replies and records the cache
counter deltas needed to explain what was actually measured.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field, replace
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import queue
import re
import resource
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from typing import Any

import compare


FAST_PATH_ENV = "PGLC_BENCH_SQL_FAST_PATH_SETUP"
DIRECT_MODE_ENV = "PGLC_BENCH_SQL_DIRECT_SETUP"
EXTENDED_SQL_MIN_OPS_ENV = "PGLC_BENCH_SQL_EXTENDED_MIN_OPS"
SQL_QUERY_PROTOCOLS = frozenset(("prepared", "extended"))
ORDINARY_LOOKUP = (
    f"SELECT value FROM public.{compare.TABLE} WHERE id = :key;"
)


@dataclass(frozen=True)
class ScenarioConfig:
    base: compare.Config
    duration: float
    repetitions: int
    stampede_rounds: int
    require_single_flight: bool
    sql_min_ops: float
    sql_extended_min_ops: float
    sql_fast_path_setup: str | None
    sql_direct_setup: str | None

    @classmethod
    def from_environment(cls) -> "ScenarioConfig":
        base = compare.Config.from_environment()
        default_duration = min(base.duration, 30.0)
        fast_setup = normalized_setup_sql(os.environ.get(FAST_PATH_ENV, ""))
        direct_setup = normalized_setup_sql(
            os.environ.get(DIRECT_MODE_ENV, "")
        )
        setup_sql_to_pgoptions(fast_setup)
        setup_sql_to_pgoptions(direct_setup)
        sql_min_ops = compare.env_float(
            "PGLC_BENCH_SQL_MIN_OPS", 10_000, 0, 1e12
        )
        return cls(
            base=base,
            duration=compare.env_float(
                "PGLC_BENCH_SCENARIO_DURATION",
                default_duration,
                1,
                3600,
            ),
            repetitions=compare.env_int(
                "PGLC_BENCH_SCENARIO_REPETITIONS",
                base.repetitions,
                1,
                20,
            ),
            stampede_rounds=compare.env_int(
                "PGLC_BENCH_STAMPEDE_ROUNDS", 5, 1, 100
            ),
            require_single_flight=env_bool(
                "PGLC_BENCH_REQUIRE_SINGLE_FLIGHT", False
            ),
            sql_min_ops=sql_min_ops,
            sql_extended_min_ops=compare.env_float(
                EXTENDED_SQL_MIN_OPS_ENV, sql_min_ops, 0, 1e12
            ),
            sql_fast_path_setup=fast_setup,
            sql_direct_setup=direct_setup,
        )

    def load_config(self) -> compare.Config:
        return replace(
            self.base,
            duration=self.duration,
            repetitions=self.repetitions,
            warmup_seconds=min(self.base.warmup_seconds, 5.0),
        )


@dataclass
class FixedWorkerResult:
    worker_index: int
    completed: int = 0
    errors: int = 0
    latencies_ms: array = field(default_factory=lambda: array("d"))
    messages: list[str] = field(default_factory=list)
    finished_at: float = 0.0


def normalized_setup_sql(raw: str) -> str | None:
    statement = raw.strip()
    if not statement:
        return None
    if (
        "\x00" in statement
        or "\\" in statement
        or "\n" in statement
        or "\r" in statement
    ):
        raise ValueError(
            "SQL mode setup must be one statement without psql meta commands"
        )
    if statement.count(";") > 1 or (
        ";" in statement and not statement.endswith(";")
    ):
        raise ValueError("SQL mode setup must contain one SQL statement")
    return statement if statement.endswith(";") else statement + ";"


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def setup_sql_to_pgoptions(statement: str | None) -> str | None:
    if statement is None:
        return None
    match = re.fullmatch(
        r"SET(?:\s+SESSION)?\s+"
        r"([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
        r"([A-Za-z0-9_.-]+)\s*;",
        statement,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError(
            "SQL mode setup must be a simple SET guc.name = value statement"
        )
    return f"-c {match.group(1)}={match.group(2)}"


def substitute_key_variable(query: str, variable: str) -> str:
    return re.sub(
        r"(?<![A-Za-z0-9_]):key(?![A-Za-z0-9_])",
        f":{variable}",
        query,
    )


def lookup_script(
    query: str,
    keys: int,
    pipeline: int,
) -> str:
    lines = ["\\startpipeline"]
    for index in range(pipeline):
        variable = f"key_{index}"
        lines.append(f"\\set {variable} random(1, {keys})")
        lines.append(substitute_key_variable(query, variable))
    lines.append("\\endpipeline")
    return "\n".join(lines) + "\n"


def write_script(
    keys: int, value_size: int, pipeline: int, concurrency: int
) -> str:
    keys_per_client = keys // concurrency
    if keys % concurrency != 0 or pipeline > keys_per_client:
        raise ValueError(
            "write workload needs a disjoint key range per pgbench client"
        )
    lines = ["\\startpipeline"]
    for index in range(pipeline):
        # Each pgbench client owns a disjoint range and visits keys in the same
        # order.  This measures trigger/invalidation cost without introducing
        # cross-client row-lock deadlocks into either PostgreSQL lane.
        key = f"(:client_id * {keys_per_client}) + {index + 1}"
        lines.extend(
            (
                f"UPDATE public.{compare.TABLE} "
                f"SET value = repeat('y', {value_size}) "
                f"WHERE id = {key};",
            )
        )
    lines.append("\\endpipeline")
    return "\n".join(lines) + "\n"


def parse_pgbench_output(
    output: str, operations_per_batch: int
) -> dict[str, Any]:
    patterns = {
        "tps": (
            r"^tps = ([0-9]+(?:\.[0-9]+)?) "
            r"\(without initial connection time\)$"
        ),
        "latency": r"^latency average = ([0-9]+(?:\.[0-9]+)?) ms$",
        "transactions": (
            r"^number of transactions actually processed: ([0-9]+)"
        ),
        "failures": r"^number of failed transactions: ([0-9]+)",
    }
    matches = {
        name: re.search(pattern, output, re.MULTILINE)
        for name, pattern in patterns.items()
    }
    if not all(matches[name] for name in ("tps", "latency", "transactions")):
        raise ValueError(f"could not parse pgbench output:\n{output}")
    tps = float(matches["tps"].group(1))  # type: ignore[union-attr]
    transactions = int(
        matches["transactions"].group(1)  # type: ignore[union-attr]
    )
    failures = (
        int(matches["failures"].group(1))
        if matches["failures"] is not None
        else 0
    )
    return {
        "successful_batches": transactions,
        "successful_operations": transactions * operations_per_batch,
        "failed_batches": failures,
        "batch_transactions_per_second": tps,
        "operations_per_second": tps * operations_per_batch,
        "batch_latency_average_ms": float(
            matches["latency"].group(1)  # type: ignore[union-attr]
        ),
        "operations_per_batch": operations_per_batch,
    }


def run_pgbench_once(
    config: compare.Config,
    host: str,
    script_path: Path,
    duration: float,
    seed: int,
    setup_sql: str | None = None,
    query_protocol: str = "prepared",
) -> dict[str, Any]:
    if query_protocol not in SQL_QUERY_PROTOCOLS:
        raise ValueError(
            "SQL query protocol must be 'prepared' or 'extended'"
        )
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    pgoptions = setup_sql_to_pgoptions(setup_sql)
    if pgoptions is not None:
        environment["PGOPTIONS"] = pgoptions
    result = subprocess.run(
        [
            "pgbench",
            "-h",
            host,
            "-p",
            str(compare.PG_PORT),
            "-U",
            compare.PG_APP_USER,
            "-n",
            "-M",
            query_protocol,
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
            compare.PG_DATABASE,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        timeout=max(90, math.ceil(duration) + 60),
    )
    parsed = parse_pgbench_output(result.stdout, config.pipeline)
    parsed["query_protocol"] = query_protocol
    return parsed


def prepared_lookup_probe(
    config: compare.Config,
    host: str,
    setup_sql: str | None,
    sample_count: int = 32,
) -> dict[str, Any]:
    """Validate exact prepared lookup results outside the timed window."""
    count = min(config.keys, sample_count)
    if count == 1:
        keys = [1]
    else:
        keys = sorted(
            {
                1 + (index * (config.keys - 1)) // (count - 1)
                for index in range(count)
            }
        )
    statements = [
        "SELECT current_user, rolsuper, "
        "pg_catalog.has_schema_privilege(current_user, 'local_cache', 'USAGE') "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user;",
        f"PREPARE pglc_lookup_probe(bigint) AS {ORDINARY_LOOKUP.replace(':key', '$1')}",
        *(f"EXECUTE pglc_lookup_probe({key});" for key in keys),
        "DEALLOCATE pglc_lookup_probe;",
    ]
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    pgoptions = setup_sql_to_pgoptions(setup_sql)
    if pgoptions is not None:
        environment["PGOPTIONS"] = pgoptions
    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            str(compare.PG_PORT),
            "-U",
            compare.PG_APP_USER,
            "-d",
            compare.PG_DATABASE,
            "-Atq",
            "-c",
            "".join(statements),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        timeout=60,
    )
    lines = result.stdout.splitlines()
    identity = f"{compare.PG_APP_USER}|f|f"
    expected = "x" * config.value_size
    if not lines or lines[0] != identity:
        raise RuntimeError(f"ordinary SQL probe used unexpected role: {lines!r}")
    values = lines[1:]
    if values != [expected] * len(keys):
        raise RuntimeError(
            "ordinary prepared SQL returned wrong values: "
            f"expected {len(keys)} values, got {values[:10]!r}"
        )
    return {
        "status": "PASS",
        "query_protocol": "prepared",
        "application_role": compare.PG_APP_USER,
        "superuser": False,
        "local_cache_schema_usage": False,
        "validated_keys": keys,
        "prepared_statement": ORDINARY_LOOKUP.replace(":key", "$1"),
    }


def extended_lookup_probe(
    config: compare.Config,
    host: str,
    setup_sql: str | None,
    sample_count: int = 32,
) -> dict[str, Any]:
    """Validate exact values through psql's unnamed extended protocol."""
    count = min(config.keys, sample_count)
    if count == 1:
        keys = [1]
    else:
        keys = sorted(
            {
                1 + (index * (config.keys - 1)) // (count - 1)
                for index in range(count)
            }
        )
    identity_query = (
        "SELECT current_user, rolsuper, "
        "pg_catalog.has_schema_privilege(current_user, "
        "'local_cache', 'USAGE') "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user;\n"
    )
    statement = ORDINARY_LOOKUP.replace(":key", "$1").rstrip(";")
    script = identity_query + "".join(
        f"{statement}\n\\bind {key}\n\\g\n" for key in keys
    )
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    pgoptions = setup_sql_to_pgoptions(setup_sql)
    if pgoptions is not None:
        environment["PGOPTIONS"] = pgoptions
    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            str(compare.PG_PORT),
            "-U",
            compare.PG_APP_USER,
            "-d",
            compare.PG_DATABASE,
            "-Atq",
            "-f",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        input=script,
        env=environment,
        timeout=60,
    )
    lines = result.stdout.splitlines()
    identity = f"{compare.PG_APP_USER}|f|f"
    expected = "x" * config.value_size
    if not lines or lines[0] != identity:
        raise RuntimeError(
            f"ordinary extended SQL probe used unexpected role: {lines!r}"
        )
    values = lines[1:]
    if values != [expected] * len(keys):
        raise RuntimeError(
            "ordinary extended SQL returned wrong values: "
            f"expected {len(keys)} values, got {values[:10]!r}"
        )
    return {
        "status": "PASS",
        "query_protocol": "extended",
        "application_role": compare.PG_APP_USER,
        "superuser": False,
        "local_cache_schema_usage": False,
        "validated_keys": keys,
        "parameterized_statement": statement,
        "psql_execution": "unnamed extended query via \\bind and \\g",
    }


def sql_cold_fill_probe(
    config: compare.Config,
    target: compare.Target,
    setup_sql: str,
    query_protocol: str = "prepared",
) -> dict[str, Any]:
    """Prove that one ordinary SQL miss self-fills and the next read hits."""
    if query_protocol not in SQL_QUERY_PROTOCOLS:
        raise ValueError(
            "SQL query protocol must be 'prepared' or 'extended'"
        )
    controller = target_controller(target, config)
    try:
        invalidated_entries = invalidate_namespace(controller)
    finally:
        controller.close()

    before = compare.read_pglc_stats(target, config)
    statement = ORDINARY_LOOKUP.replace(":key", "$1")
    if query_protocol == "prepared":
        commands = (
            f"PREPARE pglc_cold_fill_probe(bigint) AS {statement}"
            "EXECUTE pglc_cold_fill_probe(1);"
            "EXECUTE pglc_cold_fill_probe(1);"
            "DEALLOCATE pglc_cold_fill_probe;"
        )
    else:
        extended_statement = statement.rstrip(";")
        commands = (
            f"{extended_statement}\n\\bind 1\n\\g\n"
            f"{extended_statement}\n\\bind 1\n\\g\n"
        )
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.pg_password
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    pgoptions = setup_sql_to_pgoptions(setup_sql)
    if pgoptions is not None:
        environment["PGOPTIONS"] = pgoptions
    psql_arguments = [
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        compare.PG_HOST,
        "-p",
        str(compare.PG_PORT),
        "-U",
        compare.PG_APP_USER,
        "-d",
        compare.PG_DATABASE,
        "-Atq",
    ]
    run_input: dict[str, str] = {}
    if query_protocol == "prepared":
        psql_arguments.extend(("-c", commands))
    else:
        psql_arguments.extend(("-f", "-"))
        run_input["input"] = commands
    result = subprocess.run(
        psql_arguments,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **run_input,
        env=environment,
        timeout=60,
    )
    values = result.stdout.splitlines()
    expected = "x" * config.value_size
    if values != [expected, expected]:
        raise RuntimeError(
            "ordinary SQL cold-fill probe returned wrong values: "
            f"{values!r}"
        )
    after = compare.read_pglc_stats(target, config)
    deltas = counter_delta(
        before,
        after,
        "sql_cache_hits",
        "sql_cache_misses",
        "sql_cache_fills",
        "sql_cache_bypasses",
    )
    expected_deltas = {
        "sql_cache_hits_during_measurement": 1,
        "sql_cache_misses_during_measurement": 1,
        "sql_cache_fills_during_measurement": 1,
        "sql_cache_bypasses_during_measurement": 0,
    }
    if deltas != expected_deltas:
        raise RuntimeError(
            "ordinary SQL cold-fill probe did not produce miss/fill/hit: "
            f"{deltas!r}"
        )
    return {
        "status": "PASS",
        "query_protocol": query_protocol,
        "query": statement,
        "application_role": compare.PG_APP_USER,
        "invalidated_entries": invalidated_entries,
        "validated_key": 1,
        **deltas,
    }


def run_pgbench_repetitions(
    config: compare.Config,
    host: str,
    script: str,
    seed_base: int,
    setup_sql: str | None = None,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="pglc_scenario_", suffix=".sql", delete=False
    ) as stream:
        stream.write(script)
        path = Path(stream.name)
    try:
        if config.warmup_seconds > 0:
            run_pgbench_once(
                config,
                host,
                path,
                config.warmup_seconds,
                seed_base - 1,
                setup_sql,
            )
        runs = [
            run_pgbench_once(
                config,
                host,
                path,
                config.duration,
                seed_base + index,
                setup_sql,
            )
            for index in range(config.repetitions)
        ]
    finally:
        path.unlink(missing_ok=True)
    return {
        "runs": runs,
        "summary": compare.summarize_throughput_runs(runs),
        "operations_per_batch": config.pipeline,
    }


def setup(config: compare.Config) -> int:
    capacity = compare.setup_postgres(config)
    compare.psql(
        config,
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"public.{compare.TABLE} TO {compare.PG_WORKER_ROLE};"
        f"SELECT local_cache.register_mapping("
        f"'{compare.NAMESPACE}', 'public.{compare.TABLE}', "
        "'id', 'value', true);",
    )
    compare.setup_plain_postgres(config)
    return capacity


def restore_rows(config: compare.Config, *, plain: bool = False) -> None:
    host = compare.PLAIN_PG_HOST if plain else compare.PG_HOST
    compare.psql(
        config,
        f"TRUNCATE public.{compare.TABLE};"
        f"INSERT INTO public.{compare.TABLE} "
        f"SELECT g, repeat('x', {config.value_size}) "
        f"FROM generate_series(1, {config.keys}) AS g;"
        f"ANALYZE public.{compare.TABLE};",
        host=host,
    )


def target_controller(
    target: compare.Target, config: compare.Config
) -> compare.RespConnection:
    return compare.RespConnection(target, config.auth_token)


def partition_frames(
    frames: list[bytes], concurrency: int, pipeline: int
) -> list[list[bytes]]:
    if len(frames) % concurrency != 0:
        raise ValueError("frame count must be divisible by concurrency")
    per_worker = len(frames) // concurrency
    if per_worker % pipeline != 0:
        raise ValueError("frames per worker must be divisible by pipeline")
    result: list[list[bytes]] = []
    for worker in range(concurrency):
        worker_frames = frames[worker::concurrency]
        result.append(
            [
                b"".join(worker_frames[start : start + pipeline])
                for start in range(0, len(worker_frames), pipeline)
            ]
        )
    return result


def fixed_worker(
    worker_index: int,
    target: compare.Target,
    auth_token: str,
    batches: list[bytes],
    responses_per_batch: int,
    expected: object,
    ready: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    result = FixedWorkerResult(worker_index)
    connection: compare.RespConnection | None = None
    try:
        connection = compare.RespConnection(target, auth_token)
    except Exception as error:
        result.errors += 1
        result.messages.append(f"connect/setup: {error}")

    try:
        ready.wait(timeout=compare.SOCKET_TIMEOUT + 30)
        if not start_event.wait(timeout=compare.SOCKET_TIMEOUT + 30):
            raise TimeoutError("start event was not set")
        if connection is not None:
            for batch in batches:
                sent_at = time.perf_counter_ns()
                connection.socket.sendall(batch)
                for _ in range(responses_per_batch):
                    response = connection.read_response()
                    completed_at = time.perf_counter_ns()
                    if response != expected:
                        result.errors += 1
                        if len(result.messages) < 5:
                            result.messages.append(
                                f"unexpected response: {response!r}"
                            )
                        continue
                    result.completed += 1
                    result.latencies_ms.append(
                        (completed_at - sent_at) / 1_000_000
                    )
    except Exception as error:
        result.errors += 1
        if len(result.messages) < 5:
            result.messages.append(str(error))
    finally:
        if connection is not None:
            connection.close()
        result.finished_at = time.perf_counter()
        result_queue.put(result)


def percentile(values: list[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentage / 100 * len(ordered)) - 1)
    return ordered[rank]


def run_fixed_resp(
    target: compare.Target,
    config: compare.Config,
    frames: list[bytes],
    expected: object,
    *,
    replicate_each_worker: bool = False,
) -> dict[str, Any]:
    if replicate_each_worker:
        if len(frames) != 1:
            raise ValueError("replicated waves require exactly one frame")
        batches_by_worker = [[frames[0]] for _ in range(config.concurrency)]
        responses_per_batch = 1
    else:
        batches_by_worker = partition_frames(
            frames, config.concurrency, config.pipeline
        )
        responses_per_batch = config.pipeline

    context = multiprocessing.get_context("fork")
    ready = context.Barrier(config.concurrency + 1)
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=fixed_worker,
            args=(
                index,
                target,
                config.auth_token,
                batches_by_worker[index],
                responses_per_batch,
                expected,
                ready,
                start_event,
                result_queue,
            ),
            name=f"{target.name}-fixed-{index}",
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
        raise RuntimeError("fixed-load workers did not become ready") from error

    started_at = time.perf_counter()
    start_event.set()
    results: list[FixedWorkerResult] = []
    deadline = time.monotonic() + compare.SOCKET_TIMEOUT + 60
    while len(results) < len(processes):
        remaining = deadline - time.monotonic()
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
                FixedWorkerResult(
                    -1,
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
            FixedWorkerResult(
                -1,
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
    latencies = [
        latency
        for result in results
        for latency in result.latencies_ms
    ]
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = max(
        compare.usage_seconds(child_after) - compare.usage_seconds(child_before),
        0.0,
    )
    return {
        "successful_operations": completed,
        "elapsed_seconds": elapsed,
        "operations_per_second": completed / elapsed,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "latency_samples": len(latencies),
        "errors": sum(result.errors for result in results),
        "error_messages": [
            message for result in results for message in result.messages
        ][:20],
        "client_cpu_seconds": cpu_seconds,
        "client_cpu_quota_utilization_percent": (
            cpu_seconds / elapsed / config.client_cpus * 100
        ),
    }


def counter_delta(
    before: dict[str, int], after: dict[str, int], *names: str
) -> dict[str, int]:
    return {
        f"{name}_during_measurement": after.get(name, 0) - before.get(name, 0)
        for name in names
    }


def invalidate_namespace(
    connection: compare.RespConnection,
) -> int:
    result = connection.command("INVALIDATE", compare.NAMESPACE)
    if not isinstance(result, int):
        raise ValueError(f"INVALIDATE returned {result!r}")
    return result


def cold_runs(
    config: compare.Config, target: compare.Target
) -> dict[str, Any]:
    expected = b"x" * config.value_size
    frames = [
        compare.RespConnection.encode_command(
            "GET", f"{compare.NAMESPACE}:{key}"
        )
        for key in range(1, config.keys + 1)
    ]
    runs = []
    controller = target_controller(target, config)
    try:
        for _ in range(config.repetitions):
            invalidate_namespace(controller)
            before = compare.read_pglc_stats(target, config)
            run = run_fixed_resp(target, config, frames, expected)
            after = compare.read_pglc_stats(target, config)
            run.update(
                counter_delta(
                    before,
                    after,
                    "cache_hits",
                    "cache_misses",
                    "database_reads",
                )
            )
            runs.append(run)
    finally:
        controller.close()
    return {
        "runs": runs,
        "summary": compare.summarize_resp_runs(runs),
        "expected_database_reads_per_run": config.keys,
    }


def warm_runs(
    config: compare.Config, target: compare.Target
) -> dict[str, Any]:
    expected = b"x" * config.value_size
    frames = [
        compare.RespConnection.encode_command(
            "GET", f"{compare.NAMESPACE}:{key}"
        )
        for key in range(1, config.keys + 1)
    ]
    compare.warm_all(target, config, frames, expected)
    runs = []
    for _ in range(config.repetitions):
        if config.warmup_seconds > 0:
            warmup = compare.run_resp_load(
                target,
                config,
                frames,
                expected,
                config.warmup_seconds,
                False,
            )
            if warmup["errors"]:
                raise RuntimeError(f"warm GET warmup failed: {warmup}")
        before = compare.read_pglc_stats(target, config)
        run = compare.run_resp_load(
            target, config, frames, expected, config.duration, True
        )
        after = compare.read_pglc_stats(target, config)
        run.update(
            counter_delta(before, after, "cache_misses", "database_reads")
        )
        runs.append(run)
    return {
        "runs": runs,
        "summary": compare.summarize_resp_runs(runs),
    }


def stampede_runs(
    config: compare.Config,
    target: compare.Target,
    rounds: int,
) -> dict[str, Any]:
    expected = b"x" * config.value_size
    frame = compare.RespConnection.encode_command(
        "GET", f"{compare.NAMESPACE}:1"
    )
    controller = target_controller(target, config)
    runs = []
    try:
        for round_index in range(rounds):
            invalidate_namespace(controller)
            before = compare.read_pglc_stats(target, config)
            run = run_fixed_resp(
                target,
                config,
                [frame],
                expected,
                replicate_each_worker=True,
            )
            after = compare.read_pglc_stats(target, config)
            run["round"] = round_index + 1
            run.update(
                counter_delta(
                    before,
                    after,
                    "cache_hits",
                    "cache_misses",
                    "database_reads",
                )
            )
            runs.append(run)
    finally:
        controller.close()
    total_reads = sum(
        run["database_reads_during_measurement"] for run in runs
    )
    total_requests = sum(run["successful_operations"] for run in runs)
    return {
        "rounds": runs,
        "concurrent_requests_per_round": config.concurrency,
        "database_reads_total": total_reads,
        "database_reads_per_round": total_reads / rounds,
        "database_reads_per_request": (
            total_reads / total_requests if total_requests else 0.0
        ),
        "single_flight_ideal_database_reads_per_round": 1,
    }


def prepare_mutation_target(
    target: compare.Target, config: compare.Config
) -> None:
    if target.name == "pg_local_cache":
        compare.psql(config, f"TRUNCATE public.{compare.TABLE};")
        return
    controller = target_controller(target, config)
    try:
        if controller.command("FLUSHDB") != "OK":
            raise AssertionError(f"{target.name} FLUSHDB failed")
    finally:
        controller.close()


def mutation_runs(config: compare.Config) -> dict[str, Any]:
    targets = compare.TARGETS
    set_frames = [
        compare.RespConnection.encode_command(
            "SET",
            f"{compare.NAMESPACE}:{key}",
            b"m" * config.value_size,
        )
        for key in range(1, config.keys + 1)
    ]
    del_frames = [
        compare.RespConnection.encode_command(
            "DEL", f"{compare.NAMESPACE}:{key}"
        )
        for key in range(1, config.keys + 1)
    ]
    by_target: dict[str, dict[str, list[dict[str, Any]]]] = {
        target.name: {"set": [], "del": []} for target in targets
    }
    for repetition in range(config.repetitions):
        rotated = targets[repetition % len(targets) :] + targets[
            : repetition % len(targets)
        ]
        for target in rotated:
            prepare_mutation_target(target, config)
            before = (
                compare.read_pglc_stats(target, config)
                if target.name == "pg_local_cache"
                else {}
            )
            set_run = run_fixed_resp(target, config, set_frames, "OK")
            middle = (
                compare.read_pglc_stats(target, config)
                if target.name == "pg_local_cache"
                else {}
            )
            del_run = run_fixed_resp(target, config, del_frames, 1)
            after = (
                compare.read_pglc_stats(target, config)
                if target.name == "pg_local_cache"
                else {}
            )
            if target.name == "pg_local_cache":
                set_run.update(
                    counter_delta(before, middle, "database_writes")
                )
                del_run.update(
                    counter_delta(middle, after, "database_writes")
                )
                remaining = int(
                    compare.psql(
                        config,
                        f"SELECT count(*) FROM public.{compare.TABLE};",
                    )
                )
            else:
                controller = target_controller(target, config)
                try:
                    remaining = controller.command("DBSIZE")
                finally:
                    controller.close()
            set_run["repetition"] = repetition + 1
            del_run["repetition"] = repetition + 1
            del_run["remaining_keys_after_del"] = remaining
            by_target[target.name]["set"].append(set_run)
            by_target[target.name]["del"].append(del_run)

    return {
        target.name: {
            "set_runs": by_target[target.name]["set"],
            "set_summary": compare.summarize_resp_runs(
                by_target[target.name]["set"]
            ),
            "del_runs": by_target[target.name]["del"],
            "del_summary": compare.summarize_resp_runs(
                by_target[target.name]["del"]
            ),
        }
        for target in targets
    }


def verify_cache_matches_table(
    config: compare.Config, target: compare.Target
) -> dict[str, Any]:
    changed_output = compare.psql(
        config,
        f"SELECT id FROM public.{compare.TABLE} "
        f"WHERE value = repeat('y', {config.value_size}) ORDER BY id;",
    )
    changed = {
        int(line) for line in changed_output.splitlines() if line.strip()
    }
    controller = target_controller(target, config)
    mismatches: list[str] = []
    mismatch_count = 0
    before = compare.read_pglc_stats(target, config)
    try:
        batch_size = min(config.pipeline, 256)
        for start in range(1, config.keys + 1, batch_size):
            keys = list(range(start, min(start + batch_size, config.keys + 1)))
            frames = [
                compare.RespConnection.encode_command(
                    "GET", f"{compare.NAMESPACE}:{key}"
                )
                for key in keys
            ]
            controller.socket.sendall(b"".join(frames))
            for key in keys:
                value = controller.read_response()
                expected = (
                    b"y" * config.value_size
                    if key in changed
                    else b"x" * config.value_size
                )
                if value != expected:
                    mismatch_count += 1
                    if len(mismatches) < 20:
                        mismatches.append(
                            f"key {key}: got {value!r}, expected {expected!r}"
                        )
    finally:
        controller.close()
    after = compare.read_pglc_stats(target, config)
    return {
        "validated_keys": config.keys,
        "changed_rows": len(changed),
        "stale_or_wrong_values": mismatch_count,
        "mismatch_examples": mismatches,
        **counter_delta(before, after, "cache_misses", "database_reads"),
    }


def write_invalidation_runs(
    config: compare.Config, target: compare.Target
) -> dict[str, Any]:
    script = write_script(
        config.keys, config.value_size, config.pipeline, config.concurrency
    )
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="pglc_write_", suffix=".sql", delete=False
    ) as stream:
        stream.write(script)
        path = Path(stream.name)
    mapped_runs = []
    plain_runs = []
    try:
        for repetition in range(config.repetitions):
            order = (
                ("mapped", "plain")
                if repetition % 2 == 0
                else ("plain", "mapped")
            )
            for mode in order:
                if mode == "plain":
                    restore_rows(config, plain=True)
                    plain = run_pgbench_once(
                        config,
                        compare.PLAIN_PG_HOST,
                        path,
                        config.duration,
                        50_000 + repetition,
                    )
                    plain["repetition"] = repetition + 1
                    plain_runs.append(plain)
                    continue

                restore_rows(config)
                expected = b"x" * config.value_size
                frames = [
                    compare.RespConnection.encode_command(
                        "GET", f"{compare.NAMESPACE}:{key}"
                    )
                    for key in range(1, config.keys + 1)
                ]
                compare.warm_all(target, config, frames, expected)
                before = compare.read_pglc_stats(target, config)
                mapped = run_pgbench_once(
                    config,
                    compare.PG_HOST,
                    path,
                    config.duration,
                    50_000 + repetition,
                )
                after = compare.read_pglc_stats(target, config)
                mapped.update(
                    counter_delta(
                        before,
                        after,
                        "invalidations",
                    )
                )
                mapped["cache_validation"] = verify_cache_matches_table(
                    config, target
                )
                mapped["repetition"] = repetition + 1
                mapped_runs.append(mapped)
    finally:
        path.unlink(missing_ok=True)
    mapped_summary = compare.summarize_throughput_runs(mapped_runs)
    plain_summary = compare.summarize_throughput_runs(plain_runs)
    mapped_median = mapped_summary["median_operations_per_second"]
    plain_median = plain_summary["median_operations_per_second"]
    return {
        "mapped_postgres": {"runs": mapped_runs, "summary": mapped_summary},
        "stock_postgres": {"runs": plain_runs, "summary": plain_summary},
        "cache_population": (
            "all keys are warm before each run; the fixed disjoint update "
            "set is not re-warmed after its first commit-time invalidation"
        ),
        "active_cache_invalidation_throughput": False,
        "mapped_to_stock_throughput_ratio": (
            mapped_median / plain_median if plain_median else 0.0
        ),
    }


def sql_mode_pair(
    config: compare.Config,
    fast_setup: str | None,
    direct_setup: str | None,
    target: compare.Target,
    query_protocol: str = "prepared",
    minimum_cached_ops: float = 10_000,
) -> dict[str, Any]:
    if query_protocol not in SQL_QUERY_PROTOCOLS:
        raise ValueError(
            "SQL query protocol must be 'prepared' or 'extended'"
        )
    if fast_setup is None:
        return {
            "status": "SKIPPED",
            "query_protocol": query_protocol,
            "reason": f"{FAST_PATH_ENV} is not configured",
        }
    if direct_setup is None:
        direct_setup = "SET pg_local_cache.sql_cache = off;"
    restore_rows(config)
    cold_fill = sql_cold_fill_probe(
        config, target, fast_setup, query_protocol
    )
    expected = b"x" * config.value_size
    frames = [
        compare.RespConnection.encode_command(
            "GET", f"{compare.NAMESPACE}:{key}"
        )
        for key in range(1, config.keys + 1)
    ]
    compare.warm_all(target, config, frames, expected)
    script = lookup_script(ORDINARY_LOOKUP, config.keys, config.pipeline)
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="pglc_sql_mode_", suffix=".sql", delete=False
    ) as stream:
        stream.write(script)
        path = Path(stream.name)
    runs: dict[str, list[dict[str, Any]]] = {
        "direct": [],
        "cached": [],
    }
    setup_by_mode = {"direct": direct_setup, "cached": fast_setup}
    try:
        if config.warmup_seconds > 0:
            for mode in ("direct", "cached"):
                run_pgbench_once(
                    config,
                    compare.PG_HOST,
                    path,
                    config.warmup_seconds,
                    39_999,
                    setup_by_mode[mode],
                    query_protocol,
                )
        for repetition in range(config.repetitions):
            order = (
                ("direct", "cached")
                if repetition % 2 == 0
                else ("cached", "direct")
            )
            for mode in order:
                before = compare.read_pglc_stats(target, config)
                run = run_pgbench_once(
                    config,
                    compare.PG_HOST,
                    path,
                    config.duration,
                    40_000 + repetition,
                    setup_by_mode[mode],
                    query_protocol,
                )
                after = compare.read_pglc_stats(target, config)
                run.update(
                    counter_delta(
                        before,
                        after,
                        "sql_cache_hits",
                        "sql_cache_misses",
                        "sql_cache_fills",
                        "sql_cache_bypasses",
                    )
                )
                run["repetition"] = repetition + 1
                runs[mode].append(run)
    finally:
        path.unlink(missing_ok=True)
    direct = {
        "runs": runs["direct"],
        "summary": compare.summarize_throughput_runs(runs["direct"]),
        **{
            f"{name}_during_measurement": sum(
                run[f"{name}_during_measurement"] for run in runs["direct"]
            )
            for name in (
                "sql_cache_hits",
                "sql_cache_misses",
                "sql_cache_fills",
                "sql_cache_bypasses",
            )
        },
    }
    cached = {
        "runs": runs["cached"],
        "summary": compare.summarize_throughput_runs(runs["cached"]),
        **{
            f"{name}_during_measurement": sum(
                run[f"{name}_during_measurement"] for run in runs["cached"]
            )
            for name in (
                "sql_cache_hits",
                "sql_cache_misses",
                "sql_cache_fills",
                "sql_cache_bypasses",
            )
        },
    }
    direct_median = direct["summary"]["median_operations_per_second"]
    cached_median = cached["summary"]["median_operations_per_second"]
    probe = (
        prepared_lookup_probe
        if query_protocol == "prepared"
        else extended_lookup_probe
    )
    correctness = {
        mode: probe(config, compare.PG_HOST, setup_by_mode[mode])
        for mode in ("direct", "cached")
    }
    return {
        "status": "MEASURED",
        "query_protocol": query_protocol,
        "protocol_semantics": (
            "extended protocol with server-side prepared statement reuse"
            if query_protocol == "prepared"
            else "unnamed extended protocol; Parse/Bind/Execute per query"
        ),
        "query": ORDINARY_LOOKUP,
        "direct_setup": direct_setup,
        "cached_setup": fast_setup,
        "cold_fill_proof": cold_fill,
        "direct_mode": direct,
        "cached_mode": cached,
        "untimed_correctness": correctness,
        "throughput_gate": {
            "scope": f"{query_protocol} cached-mode median only",
            "minimum_cached_operations_per_second": minimum_cached_ops,
            "measured_cached_operations_per_second": cached_median,
            "status": (
                "PASS"
                if math.isfinite(cached_median)
                and cached_median >= minimum_cached_ops
                else "FAIL"
            ),
        },
        "cached_to_direct_throughput_ratio": (
            cached_median / direct_median if direct_median else 0.0
        ),
    }


def validate_sql_mode_lane(
    lane: dict[str, Any],
    query_protocol: str,
    minimum_cached_ops: float,
) -> list[str]:
    """Validate one protocol independently; never pool protocol results."""
    if lane["status"] != "MEASURED":
        return []
    failures: list[str] = []
    label = (
        "ordinary SQL"
        if query_protocol == "prepared"
        else "ordinary SQL extended-protocol"
    )
    if lane.get("query_protocol") != query_protocol:
        failures.append(
            f"{label} lane was labelled with the wrong query protocol"
        )
    for mode in ("direct_mode", "cached_mode"):
        for index, run in enumerate(lane[mode]["runs"], start=1):
            if run["failed_batches"]:
                failures.append(f"{label} {mode} run {index} failed")
            if run.get("query_protocol") != query_protocol:
                failures.append(
                    f"{label} {mode} run {index} used a different protocol"
                )
    cached = lane["cached_mode"]
    cached_operations = sum(
        run["successful_operations"] for run in cached["runs"]
    )
    if cached["sql_cache_hits_during_measurement"] != cached_operations:
        failures.append(
            f"{label} cached mode did not serve every successful lookup "
            "as a cache hit"
        )
    if (
        cached["sql_cache_misses_during_measurement"] != 0
        or cached["sql_cache_fills_during_measurement"] != 0
        or cached["sql_cache_bypasses_during_measurement"] != 0
    ):
        failures.append(
            f"{label} cached mode had misses, fills, or safety bypasses"
        )
    direct = lane["direct_mode"]
    if any(
        direct[f"{name}_during_measurement"] != 0
        for name in (
            "sql_cache_hits",
            "sql_cache_misses",
            "sql_cache_fills",
            "sql_cache_bypasses",
        )
    ):
        failures.append(f"{label} direct mode unexpectedly touched the cache")
    cached_median = float(
        cached["summary"]["median_operations_per_second"]
    )
    if (
        not math.isfinite(cached_median)
        or cached_median < minimum_cached_ops
    ):
        failures.append(
            f"{label} cached median {cached_median:.0f} ops/s is below the "
            f"{minimum_cached_ops:.0f} ops/s gate"
        )
    return failures


def validate_report(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    cold = report["scenarios"]["resp_cold_get"]
    expected_reads = cold["expected_database_reads_per_run"]
    for index, run in enumerate(cold["runs"], start=1):
        if run["errors"]:
            failures.append(f"cold GET run {index} returned errors")
        if run["database_reads_during_measurement"] != expected_reads:
            failures.append(
                f"cold GET run {index} performed "
                f"{run['database_reads_during_measurement']} SQL reads, "
                f"expected {expected_reads}"
            )
    for index, run in enumerate(
        report["scenarios"]["resp_warm_get"]["runs"], start=1
    ):
        if run["errors"] or run["cache_misses_during_measurement"] or run[
            "database_reads_during_measurement"
        ]:
            failures.append(f"warm GET run {index} was not fully warm")
    if report["workload"].get("require_single_flight"):
        for run in report["scenarios"]["same_key_stampede"]["rounds"]:
            if run["database_reads_during_measurement"] != 1:
                failures.append(
                    f"stampede round {run['round']} performed "
                    f"{run['database_reads_during_measurement']} SQL reads; "
                    "single-flight requires exactly 1"
                )
    stampede = report["scenarios"]["same_key_stampede"]
    for run in stampede["rounds"]:
        if run["errors"] or run["successful_operations"] != stampede[
            "concurrent_requests_per_round"
        ]:
            failures.append(
                f"stampede round {run['round']} did not validate every reply"
            )
    for target, result in report["scenarios"]["resp_mutations"].items():
        for operation in ("set", "del"):
            for index, run in enumerate(result[f"{operation}_runs"], start=1):
                if run["errors"]:
                    failures.append(
                        f"{target} {operation.upper()} run {index} returned errors"
                    )
                if (
                    target == "pg_local_cache"
                    and run["database_writes_during_measurement"]
                    != run["successful_operations"]
                ):
                    failures.append(
                        f"pg_local_cache {operation.upper()} run {index} "
                        "database-write counter did not match replies"
                    )
                if operation == "del" and run["remaining_keys_after_del"] != 0:
                    failures.append(
                        f"{target} DEL run {index} left keys behind"
                    )
    for index, run in enumerate(
        report["scenarios"]["sql_write_invalidation"]["mapped_postgres"]["runs"],
        start=1,
    ):
        if run["failed_batches"]:
            failures.append(f"mapped SQL write run {index} had failed batches")
        if run["cache_validation"]["stale_or_wrong_values"]:
            failures.append(
                f"mapped SQL write run {index} returned stale cache values"
            )
    for index, run in enumerate(
        report["scenarios"]["sql_write_invalidation"]["stock_postgres"]["runs"],
        start=1,
    ):
        if run["failed_batches"]:
            failures.append(f"stock SQL write run {index} had failed batches")
    for index, run in enumerate(
        report["scenarios"]["sql_direct_read"]["runs"], start=1
    ):
        if run["failed_batches"]:
            failures.append(f"direct SQL read run {index} had failed batches")
    failures.extend(
        validate_sql_mode_lane(
            report["scenarios"]["sql_cached_fast_path"],
            "prepared",
            float(report["workload"]["sql_min_ops"]),
        )
    )
    failures.extend(
        validate_sql_mode_lane(
            report["scenarios"]["sql_cached_extended_protocol"],
            "extended",
            float(report["workload"]["sql_extended_min_ops"]),
        )
    )
    return failures


def fmt(value: object, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_markdown(report: dict[str, Any]) -> str:
    scenarios = report["scenarios"]
    warm = scenarios["resp_warm_get"]["summary"]
    cold = scenarios["resp_cold_get"]["summary"]
    stampede = scenarios["same_key_stampede"]
    lines = [
        "# pg_local_cache extended benchmark scenarios",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "These numbers describe different semantics and must not be merged into "
        "one ranking. Latencies are client-observed pipeline-completion times.",
        "",
        "## Read paths",
        "",
        "| Scenario | Median ops/s | Median p50 | Median p95 | Median p99 |",
        "|---|---:|---:|---:|---:|",
        f"| RESP warm GET | {fmt(warm['median_operations_per_second'])} | "
        f"{fmt(warm['median_p50_ms'], 3)} ms | "
        f"{fmt(warm['median_p95_ms'], 3)} ms | "
        f"{fmt(warm['median_p99_ms'], 3)} ms |",
        f"| RESP one-shot cold GET | "
        f"{fmt(cold['median_operations_per_second'])} | "
        f"{fmt(cold['median_p50_ms'], 3)} ms | "
        f"{fmt(cold['median_p95_ms'], 3)} ms | "
        f"{fmt(cold['median_p99_ms'], 3)} ms |",
        "",
        "Cold GET uses each existing key exactly once after namespace "
        "invalidation; expected SQL reads equal the key count.",
        "",
        "## Same-key cold stampede",
        "",
        f"- Concurrent GETs per wave: {stampede['concurrent_requests_per_round']}.",
        f"- Waves: {len(stampede['rounds'])}.",
        f"- SQL reads per wave: {fmt(stampede['database_reads_per_round'], 2)} "
        "(single-flight ideal: 1).",
        f"- SQL reads per request: "
        f"{fmt(stampede['database_reads_per_request'], 4)}.",
        "",
        "## RESP SET and DEL",
        "",
        "| Target | SET median ops/s | DEL median ops/s |",
        "|---|---:|---:|",
    ]
    for target in ("pg_local_cache", "valkey", "redis"):
        result = scenarios["resp_mutations"][target]
        lines.append(
            f"| {target} | "
            f"{fmt(result['set_summary']['median_operations_per_second'])} | "
            f"{fmt(result['del_summary']['median_operations_per_second'])} |"
        )
    direct = scenarios["sql_direct_read"]["summary"]
    writes = scenarios["sql_write_invalidation"]
    mapped_write = writes["mapped_postgres"]["summary"]
    plain_write = writes["stock_postgres"]["summary"]
    lines.extend(
        (
            "",
            "Valkey and Redis persistence is disabled. pg_local_cache SET/DEL "
            "includes a PostgreSQL transaction and therefore is not a "
            "durability-equivalent engine comparison.",
            "",
            "## SQL paths",
            "",
            "| Scenario | Median operations/s |",
            "|---|---:|",
            f"| Direct stock PostgreSQL SELECT | "
            f"{fmt(direct['median_operations_per_second'])} |",
            f"| Mapped PostgreSQL repeated UPDATE bookkeeping | "
            f"{fmt(mapped_write['median_operations_per_second'])} |",
            f"| Stock PostgreSQL UPDATE | "
            f"{fmt(plain_write['median_operations_per_second'])} |",
            f"| Mapped/stock UPDATE ratio | "
            f"{fmt(writes['mapped_to_stock_throughput_ratio'], 3)} |",
            "",
        )
    )
    for key, skipped_label, measured_label in (
        (
            "sql_cached_fast_path",
            "SQL cached fast-path",
            "SQL prepared ordinary-query cache pair",
        ),
        (
            "sql_cached_extended_protocol",
            "SQL cached extended-protocol",
            "SQL unnamed extended-protocol ordinary-query cache pair",
        ),
    ):
        lane = scenarios[key]
        if lane["status"] == "SKIPPED":
            lines.extend(
                (
                    f"{skipped_label}: **SKIPPED** — {lane['reason']}.",
                    "",
                )
            )
            continue
        lane_direct = lane["direct_mode"]
        lane_cached = lane["cached_mode"]
        throughput_gate = lane["throughput_gate"]
        lines.extend(
            (
                f"{measured_label}: **MEASURED**.",
                "",
                f"- Protocol: `{lane['query_protocol']}` — "
                f"{lane['protocol_semantics']}.",
                f"- Query: `{lane['query']}`",
                f"- Direct setup: `{lane['direct_setup']}`",
                f"- Cached setup: `{lane['cached_setup']}`",
                f"- Direct median operations/s: "
                f"{fmt(lane_direct['summary']['median_operations_per_second'])}.",
                f"- Cached median operations/s: "
                f"{fmt(lane_cached['summary']['median_operations_per_second'])}.",
                f"- Cached/direct ratio: "
                f"{fmt(lane['cached_to_direct_throughput_ratio'], 3)}.",
                f"- Protocol-specific throughput gate: "
                f"**{throughput_gate['status']}** at "
                f"{fmt(throughput_gate['minimum_cached_operations_per_second'])} "
                "cached operations/s.",
                "- Cold ordinary-SQL proof: one miss, one fill, then one hit.",
                f"- Cached-mode SQL cache hits: "
                f"{lane_cached['sql_cache_hits_during_measurement']}.",
                f"- Cached-mode SQL safety bypasses: "
                f"{lane_cached['sql_cache_bypasses_during_measurement']}.",
                "",
            )
        )
    lines.extend(
        (
            "## Result semantics",
            "",
            "- `operations_per_second` counts validated commands/statements, "
            "not pipeline batches.",
            "- Fixed cold/mutation runs execute every configured key exactly "
            "once per repetition; process startup and AUTH are outside the timer.",
            "- Stampede time starts only after all authenticated clients are "
            "at the barrier. `database_reads_per_round` is the coalescing metric.",
            "- SQL write validation reads the authoritative table after commit "
            "and checks every RESP value; stale results fail the suite.",
            "- Timed SQL UPDATEs repeatedly touch one disjoint key set. Those "
            "keys start warm but are not re-warmed after their first "
            "invalidation, so this is not an active-cache invalidation "
            "throughput claim.",
            "- The optional ordinary-SQL fast-path is measured only when "
            f"`{FAST_PATH_ENV}` is explicitly supplied. Direct and cached "
            "lanes use the same PostgreSQL container, query text, parameters, "
            "seeds, clients and pipeline; only their one-time session setup "
            "differs. Prepared and unnamed-extended results and gates remain "
            "separate and are never averaged. An absent mode is "
            "reported as SKIPPED, never as zero throughput.",
            "- CPU quotas are limits rather than affinity. Use pinned isolated "
            "CPUs for publication-quality comparisons.",
            "",
            f"Gate: **{report['gate']['status']}** — {report['gate']['message']}",
            "",
        )
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "scenarios-failure.json").unlink(missing_ok=True)
    (output_directory / "scenarios-failure.md").unlink(missing_ok=True)
    json_path = output_directory / "scenarios.json"
    markdown_path = output_directory / "scenarios.md"
    temporary_json = output_directory / ".scenarios.json.tmp"
    temporary_markdown = output_directory / ".scenarios.md.tmp"
    temporary_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_markdown.write_text(render_markdown(report), encoding="utf-8")
    os.replace(temporary_json, json_path)
    os.replace(temporary_markdown, markdown_path)


def write_failure_report(error: BaseException, output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "scenarios.json").unlink(missing_ok=True)
    (output_directory / "scenarios.md").unlink(missing_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL",
        "error_type": type(error).__name__,
        "error": str(error),
        "traceback": traceback.format_exc(),
    }
    temporary = output_directory / ".scenarios-failure.json.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output_directory / "scenarios-failure.json")
    markdown = (
        "# pg_local_cache extended benchmark failed\n\n"
        f"- Error: `{type(error).__name__}: {error}`\n"
        f"- Source: `{os.environ.get('PGLC_BENCH_SOURCE_REVISION', 'unknown')}`\n"
    )
    temporary_markdown = output_directory / ".scenarios-failure.md.tmp"
    temporary_markdown.write_text(markdown, encoding="utf-8")
    os.replace(
        temporary_markdown, output_directory / "scenarios-failure.md"
    )


def main() -> int:
    if sys.platform != "linux":
        raise RuntimeError("extended benchmark requires Linux/fork")
    scenario = ScenarioConfig.from_environment()
    config = scenario.load_config()
    config.validate()
    target = compare.TARGETS[0]

    print("extended scenarios: setting up tables and writable mapping", flush=True)
    capacity = setup(config)
    expected = b"x" * config.value_size
    compare.wait_for_mapping(target, config, expected)

    print("extended scenarios: cold GET", flush=True)
    cold = cold_runs(config, target)
    print("extended scenarios: warm GET", flush=True)
    warm = warm_runs(config, target)
    print("extended scenarios: same-key stampede", flush=True)
    stampede = stampede_runs(config, target, scenario.stampede_rounds)
    print("extended scenarios: RESP SET/DEL comparison", flush=True)
    mutations = mutation_runs(config)

    restore_rows(config)
    restore_rows(config, plain=True)
    print("extended scenarios: direct SQL read", flush=True)
    direct_read = compare.run_postgres_reference(config)
    print(
        "extended scenarios: optional prepared SQL cached fast path",
        flush=True,
    )
    fast_path = sql_mode_pair(
        config,
        scenario.sql_fast_path_setup,
        scenario.sql_direct_setup,
        target,
        "prepared",
        scenario.sql_min_ops,
    )
    print(
        "extended scenarios: optional unnamed extended-protocol SQL cache",
        flush=True,
    )
    extended_fast_path = sql_mode_pair(
        config,
        scenario.sql_fast_path_setup,
        scenario.sql_direct_setup,
        target,
        "extended",
        scenario.sql_extended_min_ops,
    )
    print("extended scenarios: repeated SQL writes + validation", flush=True)
    write_invalidation = write_invalidation_runs(config, target)

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workload": {
            "duration_seconds": config.duration,
            "repetitions": config.repetitions,
            "concurrency": config.concurrency,
            "pipeline": config.pipeline,
            "keys": config.keys,
            "value_size": config.value_size,
            "pg_local_cache_capacity": capacity,
            "stampede_rounds": scenario.stampede_rounds,
            "require_single_flight": scenario.require_single_flight,
            "sql_min_ops": scenario.sql_min_ops,
            "sql_extended_min_ops": scenario.sql_extended_min_ops,
        },
        "scenarios": {
            "resp_cold_get": cold,
            "resp_warm_get": warm,
            "same_key_stampede": stampede,
            "resp_mutations": mutations,
            "sql_direct_read": direct_read,
            "sql_cached_fast_path": fast_path,
            "sql_cached_extended_protocol": extended_fast_path,
            "sql_write_invalidation": write_invalidation,
        },
        "methodology": {
            "transport": "Docker bridge TCP",
            "fixed_run_timer": "after AUTH and all-client barrier",
            "latency": "pipeline send to individual response completion",
            "reply_validation": "every RESP reply and SQL error",
            "ordinary_sql_protocols": {
                "prepared": (
                    "pgbench -M prepared; parse analysis reused after the "
                    "first execution"
                ),
                "extended": (
                    "pgbench -M extended; unnamed Parse/Bind/Execute for "
                    "each execution"
                ),
            },
            "transactional_validation": (
                "authoritative SQL table compared with every cache key after commit"
            ),
            "source_revision": os.environ.get(
                "PGLC_BENCH_SOURCE_REVISION", "unknown"
            ),
            "harness_sha256": os.environ.get(
                "PGLC_BENCH_SCENARIO_HARNESS_SHA256", "unknown"
            ),
        },
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "postgres_server_version": compare.psql(
                config, "SHOW server_version"
            ),
            "plain_postgres_server_version": compare.psql(
                config, "SHOW server_version", host=compare.PLAIN_PG_HOST
            ),
        },
        "images": compare.image_metadata(),
    }
    failures = validate_report(report)
    gate_message = "all replies and post-commit values validated"
    if fast_path["status"] == "MEASURED":
        gate_message += (
            "; prepared ordinary SQL cached median >= "
            f"{scenario.sql_min_ops:.0f} ops/s"
        )
    if extended_fast_path["status"] == "MEASURED":
        gate_message += (
            "; unnamed extended ordinary SQL cached median >= "
            f"{scenario.sql_extended_min_ops:.0f} ops/s"
        )
    report["gate"] = {
        "status": "PASS" if not failures else "FAIL",
        "message": gate_message if not failures else "; ".join(failures),
    }
    write_report(report, config.output_directory)
    print(render_markdown(report), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    output = Path(os.environ.get("PGLC_BENCH_OUTPUT_DIR", "/results"))
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("extended benchmark interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"extended benchmark failed: {error}", file=sys.stderr)
        write_failure_report(error, output)
        raise
