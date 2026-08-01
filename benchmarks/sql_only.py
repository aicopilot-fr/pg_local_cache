#!/usr/bin/env python3
"""Standalone benchmark for pg_local_cache's transparent SQL-only API.

The harness talks only to PostgreSQL.  It deliberately requires
``pg_local_cache.port = 0`` and never opens a RESP connection.  A disposable
whole-row table is attached with ``local_cache.attach_table`` and queried by
a real LOGIN NOSUPERUSER role through ordinary ``SELECT *`` statements.

Two protocol lanes are reported independently:

* pgbench ``prepared`` (server-side prepared statement reuse), and
* pgbench ``extended`` (unnamed Parse/Bind/Execute for every statement).

Each lane compares the exact same query with the SQL cache disabled and
enabled.  Cache counters are sampled around every timed run, so unrelated
pg_local_cache SQL traffic makes the benchmark fail closed instead of
silently inflating its result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Mapping, Sequence


SQL_COUNTERS = (
    "sql_cache_hits",
    "sql_cache_misses",
    "sql_cache_fills",
    "sql_cache_bypasses",
)
PROTOCOLS = ("prepared", "extended")
CUSTOM_SCAN_NAME = "Custom Scan (pg_local_cache_sql)"
DEFAULT_MINIMUM_OPS = 10_000.0
TENANT_ID = 7
MAX_PAYLOAD_BYTES = 3_000


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip() or str(default)
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
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


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


def safe_connection_value(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a non-empty single-line value")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    database: str
    admin_user: str
    admin_password: str
    duration: float
    warmup_seconds: float
    repetitions: int
    concurrency: int
    jobs: int
    pipeline: int
    keys: int
    payload_bytes: int
    prepared_min_ops: float
    extended_min_ops: float
    output_directory: Path
    run_id: str
    app_password: str
    keep_objects: bool

    @classmethod
    def from_environment(cls) -> "Config":
        concurrency = env_int("PGLC_SQL_ONLY_BENCH_CONCURRENCY", 16, 1, 256)
        generated_run_id = secrets.token_hex(5)
        config = cls(
            host=safe_connection_value("PGHOST", "127.0.0.1"),
            port=env_int(
                "PGPORT",
                5432,
                1,
                65535,
            ),
            database=safe_connection_value("PGDATABASE", "postgres"),
            admin_user=safe_connection_value("PGUSER", "postgres"),
            admin_password=os.environ.get("PGPASSWORD", ""),
            duration=env_float(
                "PGLC_SQL_ONLY_BENCH_DURATION", 30.0, 1.0, 3600.0
            ),
            warmup_seconds=env_float(
                "PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS", 5.0, 0.0, 600.0
            ),
            repetitions=env_int(
                "PGLC_SQL_ONLY_BENCH_REPETITIONS", 3, 1, 20
            ),
            concurrency=concurrency,
            jobs=env_int(
                "PGLC_SQL_ONLY_BENCH_JOBS",
                min(concurrency, max(1, os.cpu_count() or 1)),
                1,
                concurrency,
            ),
            pipeline=env_int("PGLC_SQL_ONLY_BENCH_PIPELINE", 32, 1, 256),
            keys=env_int("PGLC_SQL_ONLY_BENCH_KEYS", 16_384, 1, 65_536),
            payload_bytes=env_int(
                "PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES",
                128,
                1,
                MAX_PAYLOAD_BYTES,
            ),
            prepared_min_ops=env_float(
                "PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS",
                DEFAULT_MINIMUM_OPS,
                0.0,
                1e12,
            ),
            extended_min_ops=env_float(
                "PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS",
                DEFAULT_MINIMUM_OPS,
                0.0,
                1e12,
            ),
            output_directory=Path(
                os.environ.get(
                    "PGLC_SQL_ONLY_BENCH_OUTPUT_DIR", "benchmark-results"
                )
            ),
            run_id=os.environ.get(
                "PGLC_SQL_ONLY_BENCH_RUN_ID", generated_run_id
            ).strip(),
            app_password=(
                os.environ.get("PGLC_SQL_ONLY_BENCH_APP_PASSWORD")
                or secrets.token_urlsafe(32)
            ),
            keep_objects=env_bool("PGLC_SQL_ONLY_BENCH_KEEP_OBJECTS", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9]{8,16}", self.run_id):
            raise ValueError(
                "PGLC_SQL_ONLY_BENCH_RUN_ID must contain 8-16 lowercase ASCII "
                "letters or digits"
            )
        if self.jobs > self.concurrency:
            raise ValueError("pgbench jobs must not exceed concurrency")
        if self.keys < self.concurrency:
            raise ValueError("key count must be at least concurrency")
        if self.pipeline > 256:
            raise ValueError("pipeline exceeds the supported benchmark limit")
        if "\x00" in self.admin_password:
            raise ValueError("PGPASSWORD contains a NUL byte")
        if not self.app_password or "\x00" in self.app_password:
            raise ValueError(
                "PGLC_SQL_ONLY_BENCH_APP_PASSWORD must not be empty or contain NUL"
            )

    @property
    def schema(self) -> str:
        return f"pglc_sql_bench_{self.run_id}"

    @property
    def table(self) -> str:
        return "rows"

    @property
    def namespace(self) -> str:
        return f"sqlbench_{self.run_id}"

    @property
    def app_user(self) -> str:
        return f"pglc_sql_app_{self.run_id}"

    @property
    def qualified_table(self) -> str:
        return f"{sql_identifier(self.schema)}.{sql_identifier(self.table)}"

    @property
    def lookup_query(self) -> str:
        return (
            f"SELECT * FROM {self.qualified_table} "
            "WHERE tenant_id = :tenant AND id = :key;"
        )


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL literal contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def connection_environment(config: Config, *, application: bool) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    password = config.app_password if application else config.admin_password
    if password:
        environment["PGPASSWORD"] = password
    else:
        environment.pop("PGPASSWORD", None)
    return environment


def psql_arguments(config: Config, *, application: bool) -> list[str]:
    return [
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        config.host,
        "-p",
        str(config.port),
        "-U",
        config.app_user if application else config.admin_user,
        "-d",
        config.database,
        "-Atq",
    ]


def run_checked(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 300.0,
) -> str:
    result = subprocess.run(
        list(arguments),
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(environment) if environment is not None else None,
        timeout=timeout,
    )
    if result.returncode != 0:
        output = result.stdout.strip()
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            f"{arguments[0]}\n{output}"
        )
    return result.stdout.strip()


def psql(
    config: Config,
    query: str,
    *,
    application: bool = False,
    script: bool = False,
    discard_rows: bool = False,
) -> str:
    arguments = psql_arguments(config, application=application)
    input_text: str | None = None
    if discard_rows:
        arguments.extend(("-o", os.devnull))
    if script:
        arguments.extend(("-f", "-"))
        input_text = query
    else:
        arguments.extend(("-c", query))
    return run_checked(
        arguments,
        environment=connection_environment(config, application=application),
        input_text=input_text,
        timeout=300,
    )


def parse_pgbench_output(
    output: str, operations_per_batch: int
) -> dict[str, Any]:
    if operations_per_batch <= 0:
        raise ValueError("operations_per_batch must be positive")
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


def lookup_script(config: Config) -> str:
    lines = [f"\\set tenant {TENANT_ID}", "\\startpipeline"]
    for index in range(config.pipeline):
        variable = f"key_{index}"
        lines.append(f"\\set {variable} random(1, {config.keys})")
        lines.append(config.lookup_query.replace(":key", f":{variable}"))
    lines.append("\\endpipeline")
    return "\n".join(lines) + "\n"


def read_stats(config: Config) -> dict[str, int]:
    raw = psql(config, "SELECT local_cache.stats()::text")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"local_cache.stats() returned invalid JSON: {raw!r}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("local_cache.stats() did not return a JSON object")
    result: dict[str, int] = {}
    for counter in SQL_COUNTERS:
        value = parsed.get(counter)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError(
                f"local_cache.stats() has invalid {counter}: {value!r}"
            )
        result[counter] = value
    return result


def counter_delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    result: dict[str, int] = {}
    for counter in SQL_COUNTERS:
        if counter not in before or counter not in after:
            raise ValueError(f"counter snapshots lack {counter}")
        delta = after[counter] - before[counter]
        if delta < 0:
            raise ValueError(f"counter {counter} moved backwards")
        result[f"{counter}_during_measurement"] = delta
    return result


def summarize_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    if not runs:
        raise ValueError("at least one run is required")
    rates = [float(run["operations_per_second"]) for run in runs]
    if any(not math.isfinite(rate) or rate < 0 for rate in rates):
        raise ValueError("run throughput must be finite and non-negative")
    median = statistics.median(rates)
    mean = statistics.fmean(rates)
    deviation = statistics.pstdev(rates) if len(rates) > 1 else 0.0
    return {
        "median_operations_per_second": median,
        "mean_operations_per_second": mean,
        "minimum_operations_per_second": min(rates),
        "maximum_operations_per_second": max(rates),
        "coefficient_of_variation_percent": (
            deviation / mean * 100.0 if mean else 0.0
        ),
        "median_batch_latency_average_ms": statistics.median(
            float(run["batch_latency_average_ms"]) for run in runs
        ),
    }


def aggregate_mode(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "runs": [dict(run) for run in runs],
        "summary": summarize_runs(runs),
    }
    for counter in SQL_COUNTERS:
        field = f"{counter}_during_measurement"
        result[field] = sum(int(run[field]) for run in runs)
    return result


def discover_server(config: Config) -> dict[str, Any]:
    output = psql(
        config,
        "SELECT current_setting('server_version_num'), "
        "COALESCE((SELECT extversion FROM pg_catalog.pg_extension "
        "WHERE extname = 'pg_local_cache'), ''), "
        "COALESCE(current_setting('pg_local_cache.port', true), ''), "
        "COALESCE(current_setting('pg_local_cache.database', true), ''), "
        "COALESCE(current_setting('pg_local_cache.cache_entries', true), ''), "
        "current_user, pg_catalog.pg_is_in_recovery()",
    )
    fields = output.split("|")
    if len(fields) != 7:
        raise RuntimeError(f"unexpected PostgreSQL discovery output: {output!r}")
    version_num, extension_version, cache_port, cache_database, capacity = fields[:5]
    if int(version_num) // 10_000 != 16:
        raise RuntimeError(
            f"this pg_local_cache build requires PostgreSQL 16, got {version_num}"
        )
    if not extension_version:
        raise RuntimeError("CREATE EXTENSION pg_local_cache must be run first")
    if cache_port != "0":
        raise RuntimeError(
            "SQL-only benchmark requires pg_local_cache.port=0 "
            f"(actual {cache_port or 'unset'})"
        )
    if cache_database != config.database:
        raise RuntimeError(
            "PGDATABASE must match pg_local_cache.database "
            f"({config.database!r} != {cache_database!r})"
        )
    try:
        cache_capacity = int(capacity)
    except ValueError as error:
        raise RuntimeError(
            f"invalid pg_local_cache.cache_entries value {capacity!r}"
        ) from error
    if config.keys > cache_capacity:
        raise RuntimeError(
            f"benchmark keyspace {config.keys} exceeds cache capacity "
            f"{cache_capacity}"
        )
    if fields[6] == "t":
        raise RuntimeError("SQL-only benchmark requires a writable primary")
    # Calling stats proves that shared_preload_libraries initialized the
    # shared state, rather than merely accepting placeholder GUCs.
    read_stats(config)
    return {
        "server_version_num": int(version_num),
        "extension_version": extension_version,
        "pg_local_cache_port": int(cache_port),
        "pg_local_cache_database": cache_database,
        "cache_capacity": cache_capacity,
        "admin_user": fields[5],
        "in_recovery": fields[6] == "t",
    }


def preflight_names(config: Config) -> None:
    output = psql(
        config,
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = "
        f"{sql_literal(config.app_user)}), "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = "
        f"{sql_literal(config.schema)}), "
        "EXISTS (SELECT 1 FROM local_cache.mapping WHERE namespace = "
        f"{sql_literal(config.namespace)})",
    )
    if output != "f|f|f":
        raise RuntimeError(
            "benchmark run ID collides with existing database objects; "
            "choose another PGLC_SQL_ONLY_BENCH_RUN_ID"
        )


def setup_objects(config: Config) -> dict[str, Any]:
    role = sql_identifier(config.app_user)
    schema = sql_identifier(config.schema)
    table = config.qualified_table
    setup_sql = (
        "BEGIN;"
        "DO $pglc$ BEGIN EXECUTE pg_catalog.format("
        "'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', "
        f"{sql_literal(config.app_user)}, {sql_literal(config.app_password)}"
        "); END $pglc$;"
        f"GRANT CONNECT ON DATABASE {sql_identifier(config.database)} TO {role};"
        f"CREATE SCHEMA {schema};"
        f"CREATE TABLE {table} ("
        "tenant_id bigint NOT NULL, id bigint NOT NULL, payload text NOT NULL, "
        "amount numeric(18,2) NOT NULL, enabled boolean NOT NULL, "
        "metadata jsonb NOT NULL, note text, "
        "PRIMARY KEY (tenant_id, id));"
        f"INSERT INTO {table} "
        f"SELECT {TENANT_ID}, g, repeat('x', {config.payload_bytes}), "
        "(g % 10000)::numeric / 100, (g % 2 = 0), "
        "pg_catalog.jsonb_build_object('bucket', g % 16, "
        "'active', g % 2 = 0), "
        "CASE WHEN g % 3 = 0 THEN NULL ELSE 'note-' || g::text END "
        f"FROM pg_catalog.generate_series(1, {config.keys}) AS g;"
        f"GRANT USAGE ON SCHEMA {schema} TO {role};"
        f"GRANT SELECT ON TABLE {table} TO {role};"
        f"ANALYZE {table};"
        "SELECT local_cache.attach_table("
        f"{sql_literal(config.schema + '.' + config.table)}::regclass, false, "
        f"{sql_literal(config.namespace)})::text;"
        "COMMIT;"
    )
    # Feed the role password over stdin; never expose it in the psql process
    # command line where another local user could read it through /proc.
    try:
        output = psql(config, setup_sql, script=True)
    except RuntimeError as error:
        # PostgreSQL may include dynamic SQL in PL/pgSQL CONTEXT.  Ensure an
        # installation error cannot copy the disposable login secret into a
        # console log or failure artifact.
        raise RuntimeError(
            str(error).replace(config.app_password, "<redacted>")
        ) from None
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("attach_table did not return mapping metadata")
    try:
        mapping = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"attach_table returned invalid JSON: {lines[-1]!r}"
        ) from error
    if not isinstance(mapping, dict) or mapping.get("whole_row") is not True:
        raise RuntimeError(f"attach_table did not create a whole-row mapping: {mapping!r}")
    return mapping


def cleanup_objects(config: Config) -> None:
    cleanup_sql = (
        "DO $pglc$ BEGIN "
        f"IF pg_catalog.to_regclass({sql_literal(config.schema + '.' + config.table)}) "
        "IS NOT NULL AND pg_catalog.to_regprocedure("
        "'local_cache.detach_table(regclass)') IS NOT NULL THEN "
        "EXECUTE pg_catalog.format('SELECT local_cache.detach_table(%L::regclass)', "
        f"{sql_literal(config.schema + '.' + config.table)}); "
        "END IF; END $pglc$;"
        f"DROP SCHEMA IF EXISTS {sql_identifier(config.schema)} CASCADE;"
        "DO $pglc$ BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles "
        f"WHERE rolname = {sql_literal(config.app_user)}) THEN "
        "EXECUTE pg_catalog.format('DROP OWNED BY %I', "
        f"{sql_literal(config.app_user)});"
        "EXECUTE pg_catalog.format('DROP ROLE %I', "
        f"{sql_literal(config.app_user)});"
        "END IF; END $pglc$;"
    )
    psql(config, cleanup_sql)


def validate_application_role(config: Config) -> dict[str, Any]:
    output = psql(
        config,
        "SELECT current_user, rolsuper, rolcanlogin, rolinherit, "
        "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
        "pg_catalog.has_schema_privilege(current_user, 'local_cache', 'USAGE') "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user",
        application=True,
    )
    fields = output.split("|")
    expected = [config.app_user, "f", "t", "f", "f", "f", "f", "f", "f"]
    if fields != expected:
        raise RuntimeError(
            f"benchmark application role is not isolated NOSUPERUSER: {fields!r}"
        )
    return {
        "name": config.app_user,
        "login": True,
        "superuser": False,
        "inherit": False,
        "createdb": False,
        "createrole": False,
        "replication": False,
        "bypass_rls": False,
        "local_cache_schema_usage": False,
    }


def explain_and_sample(config: Config) -> dict[str, Any]:
    literal_query = config.lookup_query.replace(
        ":tenant", str(TENANT_ID)
    ).replace(":key", "1")
    direct_plan = psql(
        config,
        "SET pg_local_cache.sql_cache = off;"
        f"EXPLAIN (COSTS OFF) {literal_query}",
        application=True,
    )
    direct_value = psql(
        config,
        "SET pg_local_cache.sql_cache = off;" + literal_query,
        application=True,
    )
    cached_plan = psql(
        config,
        "SET pg_local_cache.sql_cache = on;"
        f"EXPLAIN (COSTS OFF) {literal_query}",
        application=True,
    )
    cached_value = psql(
        config,
        "SET pg_local_cache.sql_cache = on;" + literal_query,
        application=True,
    )
    if CUSTOM_SCAN_NAME in direct_plan:
        raise RuntimeError("cache-off plan unexpectedly contains pg_local_cache CustomScan")
    if CUSTOM_SCAN_NAME not in cached_plan:
        raise RuntimeError("cache-on whole-row lookup lacks pg_local_cache CustomScan")
    if not direct_value or cached_value != direct_value:
        raise RuntimeError("cached and direct ordinary SELECT returned different rows")
    return {
        "query": config.lookup_query,
        "validated_key": {"tenant_id": TENANT_ID, "id": 1},
        "direct_plan": direct_plan,
        "cached_plan": cached_plan,
        "direct_and_cached_rows_equal": True,
        "sample_output_bytes": len(cached_value.encode("utf-8")),
    }


def invalidate_namespace(config: Config) -> int:
    raw = psql(
        config,
        "SELECT local_cache.invalidate("
        f"{sql_literal(config.namespace)})",
    )
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"invalidate returned a non-integer: {raw!r}") from error
    if value < 0:
        raise RuntimeError(f"invalidate returned a negative count: {value}")
    return value


def validate_expected_deltas(
    actual: Mapping[str, int], expected: Mapping[str, int], context: str
) -> None:
    normalized_expected = {
        f"{counter}_during_measurement": int(expected.get(counter, 0))
        for counter in SQL_COUNTERS
    }
    if dict(actual) != normalized_expected:
        raise RuntimeError(
            f"{context} SQL-cache accounting mismatch: expected "
            f"{normalized_expected!r}, got {dict(actual)!r}"
        )


def cold_miss_fill_hit_proof(config: Config) -> dict[str, Any]:
    invalidated = invalidate_namespace(config)
    before = read_stats(config)
    statement = config.lookup_query.replace(":tenant", "$1").replace(
        ":key", "$2"
    )
    output = psql(
        config,
        "SET pg_local_cache.sql_cache = on;\n"
        "SET plan_cache_mode = force_generic_plan;\n"
        f"PREPARE pglc_cold(bigint, bigint) AS {statement}\n"
        f"EXECUTE pglc_cold({TENANT_ID}, 1);\n"
        f"EXECUTE pglc_cold({TENANT_ID}, 1);\n"
        "DEALLOCATE pglc_cold;\n",
        application=True,
        script=True,
    )
    rows = output.splitlines()
    if len(rows) != 2 or not rows[0] or rows[0] != rows[1]:
        raise RuntimeError(f"cold-fill probe returned unexpected rows: {rows!r}")
    deltas = counter_delta(before, read_stats(config))
    validate_expected_deltas(
        deltas,
        {"sql_cache_hits": 1, "sql_cache_misses": 1, "sql_cache_fills": 1},
        "cold miss/fill/hit probe",
    )
    return {
        "status": "PASS",
        "invalidated_entries": invalidated,
        "query": statement,
        "executions": 2,
        "validated_key": {"tenant_id": TENANT_ID, "id": 1},
        **deltas,
    }


def warm_all_keys(config: Config) -> dict[str, Any]:
    invalidated = invalidate_namespace(config)
    statement = config.lookup_query.replace(":tenant", "$1").replace(
        ":key", "$2"
    )
    lines = [
        "SET pg_local_cache.sql_cache = on;",
        "SET plan_cache_mode = force_generic_plan;",
        f"PREPARE pglc_warm(bigint, bigint) AS {statement}",
    ]
    lines.extend(
        f"EXECUTE pglc_warm({TENANT_ID}, {key});"
        for key in range(1, config.keys + 1)
    )
    lines.append("DEALLOCATE pglc_warm;")
    before = read_stats(config)
    psql(
        config,
        "\n".join(lines) + "\n",
        application=True,
        script=True,
        discard_rows=True,
    )
    deltas = counter_delta(before, read_stats(config))
    validate_expected_deltas(
        deltas,
        {
            "sql_cache_misses": config.keys,
            "sql_cache_fills": config.keys,
        },
        "complete keyspace warm pass",
    )
    return {
        "status": "PASS",
        "invalidated_entries": invalidated,
        "keys_filled": config.keys,
        **deltas,
    }


def full_row_integrity_proof(config: Config) -> dict[str, Any]:
    aggregate = psql(
        config,
        "SET pg_local_cache.sql_cache = off;"
        "SELECT count(*), min(id), max(id), count(DISTINCT id), "
        f"bool_and(tenant_id = {TENANT_ID}) FROM {config.qualified_table}",
        application=True,
    )
    expected_aggregate = f"{config.keys}|1|{config.keys}|{config.keys}|t"
    if aggregate != expected_aggregate:
        raise RuntimeError(
            "source table row-count/key-range proof failed: "
            f"expected {expected_aggregate!r}, got {aggregate!r}"
        )

    sentinel_keys = sorted({1, (config.keys + 1) // 2, config.keys})
    statement = config.lookup_query.replace(":tenant", "$1").replace(
        ":key", "$2"
    )

    def execute(cache_enabled: bool) -> tuple[list[str], dict[str, int]]:
        mode = "on" if cache_enabled else "off"
        lines = [
            f"SET pg_local_cache.sql_cache = {mode};",
            "SET plan_cache_mode = force_generic_plan;",
            f"PREPARE pglc_integrity(bigint, bigint) AS {statement}",
        ]
        lines.extend(
            f"EXECUTE pglc_integrity({TENANT_ID}, {key});"
            for key in sentinel_keys
        )
        lines.append("DEALLOCATE pglc_integrity;")
        before = read_stats(config)
        output = psql(
            config,
            "\n".join(lines) + "\n",
            application=True,
            script=True,
        )
        deltas = counter_delta(before, read_stats(config))
        validate_expected_deltas(
            deltas,
            {"sql_cache_hits": len(sentinel_keys)} if cache_enabled else {},
            f"{'cached' if cache_enabled else 'direct'} sentinel proof",
        )
        rows = output.splitlines()
        if len(rows) != len(sentinel_keys):
            raise RuntimeError(
                f"sentinel proof expected {len(sentinel_keys)} rows, got {len(rows)}"
            )
        returned_keys: list[int] = []
        for row in rows:
            fields = row.split("|", 2)
            if len(fields) < 2 or fields[0] != str(TENANT_ID):
                raise RuntimeError(f"sentinel proof returned malformed row: {row!r}")
            try:
                returned_keys.append(int(fields[1]))
            except ValueError as error:
                raise RuntimeError(
                    f"sentinel proof returned a non-integer key: {row!r}"
                ) from error
        if returned_keys != sentinel_keys:
            raise RuntimeError(
                f"sentinel proof returned keys {returned_keys!r}, "
                f"expected {sentinel_keys!r}"
            )
        return rows, deltas

    direct_rows, direct_deltas = execute(False)
    cached_rows, cached_deltas = execute(True)
    if cached_rows != direct_rows:
        raise RuntimeError("cached sentinel rows differ from direct PostgreSQL rows")
    digest = hashlib.sha256(
        ("\n".join(direct_rows) + "\n").encode("utf-8")
    ).hexdigest()
    return {
        "status": "PASS",
        "source_row_count": config.keys,
        "source_min_id": 1,
        "source_max_id": config.keys,
        "source_distinct_ids": config.keys,
        "sentinel_keys": sentinel_keys,
        "sentinel_rows": len(direct_rows),
        "direct_and_cached_rows_equal": True,
        "sentinel_rows_sha256": digest,
        "direct_counter_deltas": direct_deltas,
        "cached_counter_deltas": cached_deltas,
    }


def run_pgbench_once(
    config: Config,
    script_path: Path,
    *,
    protocol: str,
    cache_enabled: bool,
    duration: float,
    seed: int,
) -> dict[str, Any]:
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported pgbench protocol {protocol!r}")
    environment = connection_environment(config, application=True)
    mode = "on" if cache_enabled else "off"
    environment["PGOPTIONS"] = (
        f"-c pg_local_cache.sql_cache={mode} "
        "-c plan_cache_mode=force_generic_plan"
    )
    output = run_checked(
        [
            "pgbench",
            "-h",
            config.host,
            "-p",
            str(config.port),
            "-U",
            config.app_user,
            "-d",
            config.database,
            "-n",
            "-M",
            protocol,
            "-c",
            str(config.concurrency),
            "-j",
            str(config.jobs),
            "-T",
            str(max(1, math.ceil(duration))),
            "--random-seed",
            str(seed),
            "-f",
            str(script_path),
        ],
        environment=environment,
        timeout=max(90.0, math.ceil(duration) + 60.0),
    )
    result = parse_pgbench_output(output, config.pipeline)
    result.update(
        {
            "query_protocol": protocol,
            "cache_enabled": cache_enabled,
            "random_seed": seed,
        }
    )
    return result


def measure_protocol(
    config: Config, script_path: Path, protocol: str, minimum_ops: float
) -> dict[str, Any]:
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported protocol {protocol!r}")
    if config.warmup_seconds > 0:
        for cache_enabled in (False, True):
            warmup = run_pgbench_once(
                config,
                script_path,
                protocol=protocol,
                cache_enabled=cache_enabled,
                duration=config.warmup_seconds,
                seed=70_000,
            )
            if warmup["failed_batches"]:
                raise RuntimeError(f"{protocol} warmup had failed batches")

    runs: dict[str, list[dict[str, Any]]] = {"direct": [], "cached": []}
    for repetition in range(config.repetitions):
        order = (
            (("direct", False), ("cached", True))
            if repetition % 2 == 0
            else (("cached", True), ("direct", False))
        )
        for mode, cache_enabled in order:
            before = read_stats(config)
            run = run_pgbench_once(
                config,
                script_path,
                protocol=protocol,
                cache_enabled=cache_enabled,
                duration=config.duration,
                seed=71_000 + repetition,
            )
            run.update(counter_delta(before, read_stats(config)))
            run["repetition"] = repetition + 1
            runs[mode].append(run)

    direct = aggregate_mode(runs["direct"])
    cached = aggregate_mode(runs["cached"])
    direct_median = direct["summary"]["median_operations_per_second"]
    cached_median = cached["summary"]["median_operations_per_second"]
    return {
        "status": "MEASURED",
        "query_protocol": protocol,
        "protocol_semantics": (
            "extended protocol with server-side prepared statement reuse"
            if protocol == "prepared"
            else "unnamed extended protocol; Parse/Bind/Execute per statement"
        ),
        "direct_mode": direct,
        "cached_mode": cached,
        "cached_to_direct_throughput_ratio": (
            cached_median / direct_median if direct_median else 0.0
        ),
        "throughput_gate": {
            "scope": f"{protocol} cached-mode median only",
            "minimum_cached_operations_per_second": minimum_ops,
            "measured_cached_operations_per_second": cached_median,
            "status": (
                "PASS"
                if math.isfinite(cached_median) and cached_median >= minimum_ops
                else "FAIL"
            ),
        },
    }


def validate_cold_proof(proof: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    expected = {
        "sql_cache_hits_during_measurement": 1,
        "sql_cache_misses_during_measurement": 1,
        "sql_cache_fills_during_measurement": 1,
        "sql_cache_bypasses_during_measurement": 0,
    }
    if proof.get("status") != "PASS":
        failures.append("cold SQL miss/fill/hit proof did not pass")
    for field, wanted in expected.items():
        if proof.get(field) != wanted:
            failures.append(f"cold proof {field} is not exactly {wanted}")
    return failures


def validate_warm_proof(
    proof: Mapping[str, Any], expected_keys: object
) -> list[str]:
    failures: list[str] = []
    if not isinstance(expected_keys, int) or isinstance(expected_keys, bool):
        return ["warm proof expected key count is invalid"]
    expected = {
        "sql_cache_hits_during_measurement": 0,
        "sql_cache_misses_during_measurement": expected_keys,
        "sql_cache_fills_during_measurement": expected_keys,
        "sql_cache_bypasses_during_measurement": 0,
    }
    if proof.get("status") != "PASS":
        failures.append("complete keyspace warm proof did not pass")
    if proof.get("keys_filled") != expected_keys:
        failures.append("complete keyspace warm proof filled the wrong key count")
    for field, wanted in expected.items():
        if proof.get(field) != wanted:
            failures.append(f"warm proof {field} is not exactly {wanted}")
    return failures


def validate_protocol_lane(
    lane: Mapping[str, Any], protocol: str, minimum_ops: float
) -> list[str]:
    failures: list[str] = []
    if lane.get("status") != "MEASURED":
        return [f"{protocol} lane was not measured"]
    if lane.get("query_protocol") != protocol:
        failures.append(f"{protocol} lane has the wrong protocol label")
    for mode_name, cache_enabled in (
        ("direct_mode", False),
        ("cached_mode", True),
    ):
        mode = lane.get(mode_name)
        if not isinstance(mode, Mapping):
            failures.append(f"{protocol} {mode_name} is missing")
            continue
        runs = mode.get("runs")
        if not isinstance(runs, list) or not runs:
            failures.append(f"{protocol} {mode_name} has no runs")
            continue
        for index, run in enumerate(runs, start=1):
            if run.get("query_protocol") != protocol:
                failures.append(f"{protocol} {mode_name} run {index} mislabeled")
            if run.get("cache_enabled") is not cache_enabled:
                failures.append(f"{protocol} {mode_name} run {index} used wrong mode")
            if int(run.get("failed_batches", -1)) != 0:
                failures.append(f"{protocol} {mode_name} run {index} failed")
            successful = int(run.get("successful_operations", -1))
            wanted = successful if cache_enabled else 0
            if run.get("sql_cache_hits_during_measurement") != wanted:
                failures.append(
                    f"{protocol} {mode_name} run {index} has non-exact hit accounting"
                )
            for counter in (
                "sql_cache_misses",
                "sql_cache_fills",
                "sql_cache_bypasses",
            ):
                if run.get(f"{counter}_during_measurement") != 0:
                    failures.append(
                        f"{protocol} {mode_name} run {index} touched {counter}"
                    )
    try:
        cached_median = float(
            lane["cached_mode"]["summary"]["median_operations_per_second"]
        )
    except (KeyError, TypeError, ValueError):
        failures.append(f"{protocol} cached median is missing")
    else:
        if not math.isfinite(cached_median) or cached_median < minimum_ops:
            failures.append(
                f"{protocol} cached median {cached_median:.0f} ops/s is below "
                f"the independent {minimum_ops:.0f} ops/s gate"
            )
    return failures


def validate_report(report: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    server = report.get("server", {})
    if not isinstance(server, Mapping) or server.get("pg_local_cache_port") != 0:
        failures.append("benchmark server is not in SQL-only port=0 mode")
    role = report.get("ordinary_application_role", {})
    if not isinstance(role, Mapping) or role.get("superuser") is not False:
        failures.append("ordinary benchmark role is not a proven NOSUPERUSER")
    if isinstance(role, Mapping) and role.get("local_cache_schema_usage") is not False:
        failures.append("ordinary benchmark role can access local_cache schema")
    proof = report.get("cold_miss_fill_hit_proof", {})
    if not isinstance(proof, Mapping):
        failures.append("cold SQL proof is missing")
    else:
        failures.extend(validate_cold_proof(proof))
    warm = report.get("complete_keyspace_warm", {})
    workload = report.get("workload", {})
    expected_keys = workload.get("keys") if isinstance(workload, Mapping) else None
    if not isinstance(warm, Mapping):
        failures.append("complete keyspace warm proof is missing")
    else:
        failures.extend(validate_warm_proof(warm, expected_keys))
    integrity = report.get("full_row_integrity_proof")
    if not isinstance(integrity, Mapping):
        failures.append("full-row integrity proof is missing")
    elif not isinstance(expected_keys, int) or isinstance(expected_keys, bool):
        failures.append("full-row integrity expected key count is invalid")
    else:
        expected_sentinels = sorted({1, (expected_keys + 1) // 2, expected_keys})
        if integrity.get("status") != "PASS":
            failures.append("full-row integrity proof did not pass")
        for field, wanted in (
            ("source_row_count", expected_keys),
            ("source_min_id", 1),
            ("source_max_id", expected_keys),
            ("source_distinct_ids", expected_keys),
            ("sentinel_keys", expected_sentinels),
            ("sentinel_rows", len(expected_sentinels)),
            ("direct_and_cached_rows_equal", True),
        ):
            if integrity.get(field) != wanted:
                failures.append(f"full-row integrity {field} is not {wanted!r}")
        direct_deltas = integrity.get("direct_counter_deltas")
        cached_deltas = integrity.get("cached_counter_deltas")
        zero_deltas = {
            f"{counter}_during_measurement": 0 for counter in SQL_COUNTERS
        }
        expected_cached_deltas = dict(zero_deltas)
        expected_cached_deltas["sql_cache_hits_during_measurement"] = len(
            expected_sentinels
        )
        if direct_deltas != zero_deltas:
            failures.append("full-row direct sentinel counters are not all zero")
        if cached_deltas != expected_cached_deltas:
            failures.append("full-row cached sentinel counters are not exact hits")
        digest = integrity.get("sentinel_rows_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append("full-row sentinel digest is invalid")
    plan = report.get("ordinary_select_proof", {})
    if not isinstance(plan, Mapping):
        failures.append("ordinary SELECT plan proof is missing")
    else:
        if CUSTOM_SCAN_NAME not in str(plan.get("cached_plan", "")):
            failures.append("cached ordinary SELECT did not use CustomScan")
        if CUSTOM_SCAN_NAME in str(plan.get("direct_plan", "")):
            failures.append("direct ordinary SELECT unexpectedly used CustomScan")
        if plan.get("direct_and_cached_rows_equal") is not True:
            failures.append("cached and direct ordinary SELECT rows differ")
    protocols = report.get("protocols", {})
    if not isinstance(protocols, Mapping):
        return failures + ["protocol results are missing"]
    minimums = {
        "prepared": report.get("workload", {}).get("prepared_min_ops", DEFAULT_MINIMUM_OPS),
        "extended": report.get("workload", {}).get("extended_min_ops", DEFAULT_MINIMUM_OPS),
    }
    for protocol in PROTOCOLS:
        lane = protocols.get(protocol)
        if not isinstance(lane, Mapping):
            failures.append(f"{protocol} result is missing")
            continue
        failures.extend(
            validate_protocol_lane(lane, protocol, float(minimums[protocol]))
        )
    return failures


def format_number(value: object, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_markdown(report: Mapping[str, Any]) -> str:
    workload = report["workload"]
    lines = [
        "# pg_local_cache SQL-only benchmark",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "Ordinary `SELECT *` by the complete composite primary key, executed "
        "by a real LOGIN NOSUPERUSER. The PostgreSQL instance has "
        "`pg_local_cache.port=0`; no RESP listener, client, or token is used.",
        "",
        "## Headline throughput",
        "",
        "| Protocol | Direct PostgreSQL | Cached SQL | Cached/direct | Gate |",
        "|---|---:|---:|---:|---:|",
    ]
    for protocol in PROTOCOLS:
        lane = report["protocols"][protocol]
        direct = lane["direct_mode"]["summary"]["median_operations_per_second"]
        cached = lane["cached_mode"]["summary"]["median_operations_per_second"]
        lines.append(
            f"| {protocol} | {format_number(direct)} ops/s | "
            f"{format_number(cached)} ops/s | "
            f"{format_number(lane['cached_to_direct_throughput_ratio'], 2)}x | "
            f"**{lane['throughput_gate']['status']}** "
            f"(>= {format_number(lane['throughput_gate']['minimum_cached_operations_per_second'])}) |"
        )
    cold = report["cold_miss_fill_hit_proof"]
    warm = report["complete_keyspace_warm"]
    integrity = report["full_row_integrity_proof"]
    lines.extend(
        (
            "",
            "## Correctness evidence",
            "",
            "| Proof | Hits | Misses | Fills | Bypasses |",
            "|---|---:|---:|---:|---:|",
            f"| cold read -> fill -> warm read | "
            f"{cold['sql_cache_hits_during_measurement']} | "
            f"{cold['sql_cache_misses_during_measurement']} | "
            f"{cold['sql_cache_fills_during_measurement']} | "
            f"{cold['sql_cache_bypasses_during_measurement']} |",
            f"| complete {warm['keys_filled']}-key warm pass | "
            f"{warm['sql_cache_hits_during_measurement']} | "
            f"{warm['sql_cache_misses_during_measurement']} | "
            f"{warm['sql_cache_fills_during_measurement']} | "
            f"{warm['sql_cache_bypasses_during_measurement']} |",
            f"| {integrity['sentinel_rows']}-row direct/cached integrity sample | "
            f"{integrity['cached_counter_deltas']['sql_cache_hits_during_measurement']} | "
            "0 | 0 | 0 |",
            "",
            "Every cached timed run requires `hits == successful SELECTs` and "
            "zero misses, fills, or bypasses. Every direct run requires all "
            "four SQL-cache counter deltas to be zero.",
            "",
            "## Workload",
            "",
            "| Parameter | Value |",
            "|---|---:|",
            f"| Duration per measured run | {workload['duration_seconds']} s |",
            f"| Untimed warmup per mode | {workload['warmup_seconds']} s |",
            f"| Repetitions per mode/protocol | {workload['repetitions']} |",
            f"| Concurrent connections | {workload['concurrency']} |",
            f"| pgbench jobs | {workload['jobs']} |",
            f"| SELECTs per pipeline batch | {workload['pipeline']} |",
            f"| Distinct whole rows | {workload['keys']} |",
            f"| Text payload bytes | {workload['payload_bytes']} |",
            "",
            f"Overall gate: **{report['gate']['status']}** — "
            f"{report['gate']['message']}",
            "",
            "Prepared and unnamed-extended results have independent >=10k "
            "gates and are never pooled. Alternating direct/cached order "
            "reduces run-order bias; both modes use identical SQL, key stream, "
            "connections, jobs, duration, and pipeline depth.",
            "",
        )
    )
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "sql-only-failure.json").unlink(missing_ok=True)
    (output_directory / "sql-only-failure.md").unlink(missing_ok=True)
    json_tmp = output_directory / ".sql-only.json.tmp"
    markdown_tmp = output_directory / ".sql-only.md.tmp"
    json_tmp.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_tmp.write_text(render_markdown(report), encoding="utf-8")
    os.replace(json_tmp, output_directory / "sql-only.json")
    os.replace(markdown_tmp, output_directory / "sql-only.md")


def write_failure_report(error: BaseException, output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "sql-only.json").unlink(missing_ok=True)
    (output_directory / "sql-only.md").unlink(missing_ok=True)
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
    }
    json_tmp = output_directory / ".sql-only-failure.json.tmp"
    json_tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(json_tmp, output_directory / "sql-only-failure.json")
    markdown_tmp = output_directory / ".sql-only-failure.md.tmp"
    markdown_tmp.write_text(
        "# SQL-only benchmark failed\n\n"
        f"- Error: `{type(error).__name__}: {error}`\n",
        encoding="utf-8",
    )
    os.replace(markdown_tmp, output_directory / "sql-only-failure.md")


def tool_version(command: str) -> str:
    try:
        return run_checked([command, "--version"], timeout=30)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return f"unavailable: {error}"


def build_report(config: Config) -> dict[str, Any]:
    server = discover_server(config)
    preflight_names(config)
    cleanup_authorized = True
    primary_error: BaseException | None = None
    try:
        mapping = setup_objects(config)
        role = validate_application_role(config)
        select_proof = explain_and_sample(config)
        cold_proof = cold_miss_fill_hit_proof(config)
        warm_proof = warm_all_keys(config)
        integrity_proof = full_row_integrity_proof(config)
        script = lookup_script(config)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="pglc_sql_only_",
            suffix=".sql",
            delete=False,
        ) as stream:
            stream.write(script)
            script_path = Path(stream.name)
        try:
            protocols = {
                "prepared": measure_protocol(
                    config,
                    script_path,
                    "prepared",
                    config.prepared_min_ops,
                ),
                "extended": measure_protocol(
                    config,
                    script_path,
                    "extended",
                    config.extended_min_ops,
                ),
            }
        finally:
            script_path.unlink(missing_ok=True)
        report: dict[str, Any] = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "server": server,
            "connection": {
                "host": config.host,
                "port": config.port,
                "database": config.database,
            },
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "psql": tool_version("psql"),
                "pgbench": tool_version("pgbench"),
                "source_revision": os.environ.get(
                    "PGLC_BENCH_SOURCE_REVISION", "unknown"
                ),
                "harness_sha256": os.environ.get(
                    "PGLC_BENCH_SQL_ONLY_HARNESS_SHA256", "unknown"
                ),
            },
            "workload": {
                "duration_seconds": config.duration,
                "warmup_seconds": config.warmup_seconds,
                "repetitions": config.repetitions,
                "concurrency": config.concurrency,
                "jobs": config.jobs,
                "pipeline": config.pipeline,
                "keys": config.keys,
                "payload_bytes": config.payload_bytes,
                "prepared_min_ops": config.prepared_min_ops,
                "extended_min_ops": config.extended_min_ops,
                "query": config.lookup_query,
            },
            "methodology": {
                "transport": "PostgreSQL wire protocol only; RESP disabled",
                "table_shape": "whole row with composite (tenant_id, id) primary key",
                "application_access": "actual LOGIN NOSUPERUSER, SELECT only",
                "direct_mode": "SET pg_local_cache.sql_cache=off",
                "cached_mode": "SET pg_local_cache.sql_cache=on",
                "prepared_protocol": "pgbench -M prepared",
                "unnamed_extended_protocol": "pgbench -M extended",
                "counter_isolation": (
                    "global SQL counters must exactly equal harness operations; "
                    "concurrent cache traffic fails the run"
                ),
                "row_integrity": (
                    "full source count/key range plus direct-vs-cached whole-row "
                    "sentinels at first, middle and last key"
                ),
            },
            "mapping": mapping,
            "ordinary_application_role": role,
            "ordinary_select_proof": select_proof,
            "cold_miss_fill_hit_proof": cold_proof,
            "complete_keyspace_warm": warm_proof,
            "full_row_integrity_proof": integrity_proof,
            "protocols": protocols,
        }
        failures = validate_report(report)
        report["gate"] = {
            "status": "PASS" if not failures else "FAIL",
            "message": (
                "both SQL-only protocol gates and all exact accounting checks passed"
                if not failures
                else "; ".join(failures)
            ),
            "failures": failures,
        }
        return report
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if cleanup_authorized and not config.keep_objects:
            try:
                cleanup_objects(config)
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                print(
                    f"warning: benchmark cleanup also failed: {cleanup_error}",
                    file=sys.stderr,
                    flush=True,
                )


def main() -> int:
    config = Config.from_environment()
    report = build_report(config)
    write_report(report, config.output_directory)
    print(render_markdown(report), flush=True)
    return 0 if report["gate"]["status"] == "PASS" else 1


if __name__ == "__main__":
    output = Path(
        os.environ.get("PGLC_SQL_ONLY_BENCH_OUTPUT_DIR", "benchmark-results")
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"SQL-only benchmark failed: {error}", file=sys.stderr, flush=True)
        try:
            write_failure_report(error, output)
        except Exception as report_error:
            print(
                f"could not write SQL-only failure artifact: {report_error}",
                file=sys.stderr,
                flush=True,
            )
        raise SystemExit(1)
