#!/usr/bin/env python3
"""pgbench helpers used by the whole-row benchmark."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

import compare


SQL_QUERY_PROTOCOLS = frozenset(("prepared", "extended"))


def normalized_setup_sql(raw: str) -> str | None:
    statement = raw.strip()
    if not statement:
        return None
    if any(character in statement for character in ("\x00", "\\", "\n", "\r")):
        raise ValueError(
            "SQL mode setup must be one statement without psql meta commands"
        )
    if statement.count(";") > 1 or (
        ";" in statement and not statement.endswith(";")
    ):
        raise ValueError("SQL mode setup must contain one SQL statement")
    return statement if statement.endswith(";") else statement + ";"


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


def lookup_script(query: str, keys: int, pipeline: int) -> str:
    lines = ["\\startpipeline"]
    for index in range(pipeline):
        variable = f"key_{index}"
        lines.append(f"\\set {variable} random(1, {keys})")
        lines.append(substitute_key_variable(query, variable))
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
    operations_per_statement: int = 1,
) -> dict[str, Any]:
    if query_protocol not in SQL_QUERY_PROTOCOLS:
        raise ValueError("SQL query protocol must be 'prepared' or 'extended'")
    if operations_per_statement < 1:
        raise ValueError("operations_per_statement must be positive")
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
    parsed = parse_pgbench_output(
        result.stdout, config.pipeline * operations_per_statement
    )
    parsed["query_protocol"] = query_protocol
    parsed["statements_per_batch"] = config.pipeline
    parsed["operations_per_statement"] = operations_per_statement
    return parsed


def run_pgbench_repetitions(
    config: compare.Config,
    host: str,
    script: str,
    seed_base: int,
    setup_sql: str | None = None,
    operations_per_statement: int = 1,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="pglc_whole_row_", suffix=".sql", delete=False
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
                operations_per_statement=operations_per_statement,
            )
        runs = [
            run_pgbench_once(
                config,
                host,
                path,
                config.duration,
                seed_base + index,
                setup_sql,
                operations_per_statement=operations_per_statement,
            )
            for index in range(config.repetitions)
        ]
    finally:
        path.unlink(missing_ok=True)
    return {
        "runs": runs,
        "summary": compare.summarize_throughput_runs(runs),
        "operations_per_batch": config.pipeline * operations_per_statement,
        "statements_per_batch": config.pipeline,
        "operations_per_statement": operations_per_statement,
    }


def counter_delta(
    before: dict[str, int], after: dict[str, int], *names: str
) -> dict[str, int]:
    return {
        f"{name}_during_measurement": after.get(name, 0) - before.get(name, 0)
        for name in names
    }
