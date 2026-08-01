#!/usr/bin/env python3
"""Unit contracts for the standalone SQL-only benchmark harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_sql_only_benchmark",
    ROOT / "benchmarks" / "sql_only.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load benchmarks/sql_only.py")
sql_only = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sql_only
SPEC.loader.exec_module(sql_only)


def config(**overrides: object) -> object:
    values: dict[str, object] = {
        "host": "postgres.example",
        "port": 5432,
        "database": "app",
        "admin_user": "postgres",
        "admin_password": "admin-password",
        "duration": 1.0,
        "warmup_seconds": 0.0,
        "repetitions": 1,
        "concurrency": 2,
        "jobs": 2,
        "pipeline": 2,
        "keys": 8,
        "payload_bytes": 64,
        "prepared_min_ops": 10_000.0,
        "extended_min_ops": 10_000.0,
        "output_directory": Path("/tmp/sql-only-results"),
        "run_id": "abc12345",
        "app_password": "ordinary-role-password",
        "keep_objects": False,
    }
    values.update(overrides)
    return sql_only.Config(**values)


PGBENCH_OUTPUT = """\
transaction type: sql-only.sql
number of transactions actually processed: 125
number of failed transactions: 0 (0.000%)
latency average = 1.250 ms
tps = 80.000000 (without initial connection time)
"""


def counters(*, hits: int = 0, misses: int = 0, fills: int = 0, bypasses: int = 0) -> dict[str, int]:
    return {
        "sql_cache_hits": hits,
        "sql_cache_misses": misses,
        "sql_cache_fills": fills,
        "sql_cache_bypasses": bypasses,
    }


def timed_run(
    protocol: str,
    *,
    cached: bool,
    operations: int = 20_000,
    rate: float = 20_000.0,
) -> dict[str, object]:
    return {
        "successful_batches": operations // 2,
        "successful_operations": operations,
        "failed_batches": 0,
        "batch_transactions_per_second": rate / 2,
        "operations_per_second": rate,
        "batch_latency_average_ms": 1.0,
        "operations_per_batch": 2,
        "query_protocol": protocol,
        "cache_enabled": cached,
        "random_seed": 1,
        "repetition": 1,
        "sql_cache_hits_during_measurement": operations if cached else 0,
        "sql_cache_misses_during_measurement": 0,
        "sql_cache_fills_during_measurement": 0,
        "sql_cache_bypasses_during_measurement": 0,
    }


def lane(protocol: str, *, rate: float = 20_000.0) -> dict[str, object]:
    direct_run = timed_run(protocol, cached=False, rate=rate / 2)
    cached_run = timed_run(protocol, cached=True, rate=rate)
    direct = sql_only.aggregate_mode([direct_run])
    cached = sql_only.aggregate_mode([cached_run])
    return {
        "status": "MEASURED",
        "query_protocol": protocol,
        "protocol_semantics": "test",
        "direct_mode": direct,
        "cached_mode": cached,
        "cached_to_direct_throughput_ratio": 2.0,
        "throughput_gate": {
            "scope": f"{protocol} cached-mode median only",
            "minimum_cached_operations_per_second": 10_000.0,
            "measured_cached_operations_per_second": rate,
            "status": "PASS",
        },
    }


def valid_report() -> dict[str, object]:
    cfg = config()
    return {
        "schema_version": 1,
        "generated_at_utc": "2026-08-02T00:00:00+00:00",
        "server": {
            "pg_local_cache_port": 0,
            "extension_version": "1.0.0",
        },
        "ordinary_application_role": {
            "name": cfg.app_user,
            "superuser": False,
            "local_cache_schema_usage": False,
        },
        "ordinary_select_proof": {
            "query": cfg.lookup_query,
            "cached_plan": "Custom Scan (pg_local_cache_sql)\n  Cache Namespace: x",
            "direct_plan": "Index Scan using rows_pkey on rows",
            "direct_and_cached_rows_equal": True,
        },
        "cold_miss_fill_hit_proof": {
            "status": "PASS",
            "sql_cache_hits_during_measurement": 1,
            "sql_cache_misses_during_measurement": 1,
            "sql_cache_fills_during_measurement": 1,
            "sql_cache_bypasses_during_measurement": 0,
        },
        "complete_keyspace_warm": {
            "status": "PASS",
            "keys_filled": cfg.keys,
            "sql_cache_hits_during_measurement": 0,
            "sql_cache_misses_during_measurement": cfg.keys,
            "sql_cache_fills_during_measurement": cfg.keys,
            "sql_cache_bypasses_during_measurement": 0,
        },
        "full_row_integrity_proof": {
            "status": "PASS",
            "source_row_count": cfg.keys,
            "source_min_id": 1,
            "source_max_id": cfg.keys,
            "source_distinct_ids": cfg.keys,
            "sentinel_keys": [1, 4, 8],
            "sentinel_rows": 3,
            "direct_and_cached_rows_equal": True,
            "sentinel_rows_sha256": "a" * 64,
            "direct_counter_deltas": {
                "sql_cache_hits_during_measurement": 0,
                "sql_cache_misses_during_measurement": 0,
                "sql_cache_fills_during_measurement": 0,
                "sql_cache_bypasses_during_measurement": 0,
            },
            "cached_counter_deltas": {
                "sql_cache_hits_during_measurement": 3,
                "sql_cache_misses_during_measurement": 0,
                "sql_cache_fills_during_measurement": 0,
                "sql_cache_bypasses_during_measurement": 0,
            },
        },
        "workload": {
            "duration_seconds": cfg.duration,
            "warmup_seconds": cfg.warmup_seconds,
            "repetitions": cfg.repetitions,
            "concurrency": cfg.concurrency,
            "jobs": cfg.jobs,
            "pipeline": cfg.pipeline,
            "keys": cfg.keys,
            "payload_bytes": cfg.payload_bytes,
            "prepared_min_ops": cfg.prepared_min_ops,
            "extended_min_ops": cfg.extended_min_ops,
        },
        "protocols": {
            "prepared": lane("prepared"),
            "extended": lane("extended"),
        },
        "gate": {
            "status": "PASS",
            "message": "all checks passed",
            "failures": [],
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_defaults_need_no_resp_token_and_have_independent_10k_gates(self) -> None:
        environment = {
            "PGHOST": "db.internal",
            "PGPORT": "6432",
            "PGDATABASE": "app",
            "PGUSER": "owner",
            "PGPASSWORD": "secret",
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
        }
        with mock.patch.dict(sql_only.os.environ, environment, clear=True):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.host, "db.internal")
        self.assertEqual(parsed.port, 6432)
        self.assertEqual(parsed.prepared_min_ops, 10_000.0)
        self.assertEqual(parsed.extended_min_ops, 10_000.0)
        self.assertFalse(hasattr(parsed, "auth_token"))

    def test_protocol_gates_are_configured_independently(self) -> None:
        environment = {
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
            "PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS": "25000",
            "PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS": "12000",
        }
        with mock.patch.dict(sql_only.os.environ, environment, clear=True):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.prepared_min_ops, 25_000.0)
        self.assertEqual(parsed.extended_min_ops, 12_000.0)

    def test_run_id_and_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "RUN_ID"):
            config(run_id="unsafe-id").validate()
        with self.assertRaisesRegex(ValueError, "key count"):
            config(keys=1, concurrency=2).validate()
        with mock.patch.dict(
            sql_only.os.environ,
            {"PGLC_SQL_ONLY_BENCH_KEEP_OBJECTS": "sometimes"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "boolean"):
                sql_only.Config.from_environment()

    def test_generated_names_and_query_use_whole_composite_primary_key(self) -> None:
        cfg = config()
        self.assertEqual(cfg.schema, "pglc_sql_bench_abc12345")
        self.assertEqual(cfg.namespace, "sqlbench_abc12345")
        self.assertIn("SELECT *", cfg.lookup_query)
        self.assertIn("tenant_id = :tenant", cfg.lookup_query)
        self.assertIn("id = :key", cfg.lookup_query)

    def test_connection_arguments_use_hostname_env_and_real_app_role(self) -> None:
        cfg = config()
        arguments = sql_only.psql_arguments(cfg, application=True)
        self.assertEqual(arguments[arguments.index("-h") + 1], cfg.host)
        self.assertEqual(arguments[arguments.index("-p") + 1], "5432")
        self.assertEqual(arguments[arguments.index("-U") + 1], cfg.app_user)
        environment = sql_only.connection_environment(cfg, application=True)
        self.assertEqual(environment["PGPASSWORD"], cfg.app_password)


class ParserAndWorkloadTests(unittest.TestCase):
    def test_pgbench_parser_converts_batches_to_select_operations(self) -> None:
        parsed = sql_only.parse_pgbench_output(PGBENCH_OUTPUT, 4)
        self.assertEqual(parsed["successful_batches"], 125)
        self.assertEqual(parsed["successful_operations"], 500)
        self.assertEqual(parsed["operations_per_second"], 320.0)
        self.assertEqual(parsed["failed_batches"], 0)

    def test_pgbench_parser_tracks_failures_and_rejects_partial_output(self) -> None:
        output = PGBENCH_OUTPUT.replace(
            "number of transactions actually processed: 125\n"
            "number of failed transactions: 0 (0.000%)",
            "number of transactions actually processed: 123\n"
            "number of failed transactions: 2 (1.600%)",
        )
        parsed = sql_only.parse_pgbench_output(output, 2)
        self.assertEqual(parsed["successful_operations"], 246)
        self.assertEqual(parsed["failed_batches"], 2)
        with self.assertRaisesRegex(ValueError, "could not parse"):
            sql_only.parse_pgbench_output("tps = 1", 1)

    def test_lookup_script_pipelines_identical_full_pk_selects(self) -> None:
        cfg = config(pipeline=3, keys=100)
        script = sql_only.lookup_script(cfg)
        self.assertEqual(script.count("SELECT *"), 3)
        self.assertEqual(script.count("tenant_id = :tenant"), 3)
        self.assertIn("\\set tenant 7", script)
        self.assertIn("id = :key_0", script)
        self.assertIn("id = :key_2", script)
        self.assertEqual(script.count("\\startpipeline"), 1)
        self.assertEqual(script.count("\\endpipeline"), 1)

    def test_counter_delta_is_exact_and_monotonic(self) -> None:
        delta = sql_only.counter_delta(
            counters(hits=10, misses=20, fills=5, bypasses=2),
            counters(hits=14, misses=21, fills=6, bypasses=2),
        )
        self.assertEqual(
            delta,
            {
                "sql_cache_hits_during_measurement": 4,
                "sql_cache_misses_during_measurement": 1,
                "sql_cache_fills_during_measurement": 1,
                "sql_cache_bypasses_during_measurement": 0,
            },
        )
        with self.assertRaisesRegex(ValueError, "moved backwards"):
            sql_only.counter_delta(counters(hits=2), counters(hits=1))

    def test_summary_uses_median_and_keeps_variance_visible(self) -> None:
        runs = [
            {
                "operations_per_second": rate,
                "batch_latency_average_ms": latency,
            }
            for rate, latency in ((10.0, 3.0), (30.0, 1.0), (20.0, 2.0))
        ]
        summary = sql_only.summarize_runs(runs)
        self.assertEqual(summary["median_operations_per_second"], 20.0)
        self.assertEqual(summary["minimum_operations_per_second"], 10.0)
        self.assertEqual(summary["maximum_operations_per_second"], 30.0)
        self.assertGreater(summary["coefficient_of_variation_percent"], 0)

    def test_runner_selects_prepared_or_unnamed_extended_and_guc_mode(self) -> None:
        cfg = config()
        with tempfile.NamedTemporaryFile() as stream, mock.patch.object(
            sql_only, "run_checked", return_value=PGBENCH_OUTPUT
        ) as run:
            prepared = sql_only.run_pgbench_once(
                cfg,
                Path(stream.name),
                protocol="prepared",
                cache_enabled=True,
                duration=1,
                seed=42,
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(arguments[arguments.index("-M") + 1], "prepared")
        self.assertEqual(arguments[arguments.index("-h") + 1], cfg.host)
        self.assertIn("pg_local_cache.sql_cache=on", environment["PGOPTIONS"])
        self.assertEqual(prepared["query_protocol"], "prepared")

        with tempfile.NamedTemporaryFile() as stream, mock.patch.object(
            sql_only, "run_checked", return_value=PGBENCH_OUTPUT
        ) as run:
            sql_only.run_pgbench_once(
                cfg,
                Path(stream.name),
                protocol="extended",
                cache_enabled=False,
                duration=1,
                seed=43,
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(arguments[arguments.index("-M") + 1], "extended")
        self.assertIn("pg_local_cache.sql_cache=off", environment["PGOPTIONS"])


class DatabaseContractTests(unittest.TestCase):
    def test_discovery_requires_pg16_extension_and_port_zero(self) -> None:
        cfg = config()
        discovery = "160014|1.0.0|0|app|16384|postgres|f"
        with mock.patch.object(sql_only, "psql", return_value=discovery), mock.patch.object(
            sql_only, "read_stats", return_value=counters()
        ):
            result = sql_only.discover_server(cfg)
        self.assertEqual(result["pg_local_cache_port"], 0)
        self.assertEqual(result["cache_capacity"], 16_384)

        for bad, message in (
            ("160014|1.0.0|6380|app|16384|postgres|f", "port=0"),
            ("150014|1.0.0|0|app|16384|postgres|f", "PostgreSQL 16"),
            ("160014||0|app|16384|postgres|f", "CREATE EXTENSION"),
        ):
            with self.subTest(bad=bad), mock.patch.object(
                sql_only, "psql", return_value=bad
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    sql_only.discover_server(cfg)

    def test_setup_creates_whole_row_table_and_calls_attach_table(self) -> None:
        cfg = config(keys=9, payload_bytes=77)
        mapping = {
            "whole_row": True,
            "namespace": cfg.namespace,
            "primary_key_columns": ["tenant_id", "id"],
        }
        with mock.patch.object(
            sql_only, "psql", return_value=json.dumps(mapping)
        ) as psql:
            returned = sql_only.setup_objects(cfg)
        query = psql.call_args.args[1]
        self.assertIn("PRIMARY KEY (tenant_id, id)", query)
        self.assertIn("generate_series(1, 9)", query)
        self.assertIn("repeat('x', 77)", query)
        self.assertIn('GRANT CONNECT ON DATABASE "app"', query)
        self.assertIn("local_cache.attach_table", query)
        self.assertNotIn("attach_value", query)
        self.assertTrue(psql.call_args.kwargs["script"])
        self.assertEqual(returned, mapping)

    def test_setup_redacts_disposable_role_password_from_errors(self) -> None:
        cfg = config(app_password="never-print-this-secret")
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=RuntimeError(
                "CONTEXT: CREATE ROLE PASSWORD 'never-print-this-secret'"
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                sql_only.setup_objects(cfg)
        self.assertNotIn(cfg.app_password, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))

    def test_application_role_must_be_actual_isolated_login(self) -> None:
        cfg = config()
        identity = f"{cfg.app_user}|f|t|f|f|f|f|f|f"
        with mock.patch.object(sql_only, "psql", return_value=identity) as psql:
            role = sql_only.validate_application_role(cfg)
        self.assertTrue(psql.call_args.kwargs["application"])
        self.assertFalse(role["superuser"])
        self.assertFalse(role["local_cache_schema_usage"])

        with mock.patch.object(
            sql_only,
            "psql",
            return_value=f"{cfg.app_user}|t|t|f|f|f|f|f|f",
        ):
            with self.assertRaisesRegex(RuntimeError, "NOSUPERUSER"):
                sql_only.validate_application_role(cfg)

    def test_plan_proof_compares_identical_rows_and_customscan_presence(self) -> None:
        cfg = config()
        replies = [
            "Index Scan using rows_pkey on rows",
            "7|1|payload",
            "Custom Scan (pg_local_cache_sql)\n  Cache Namespace: test",
            "7|1|payload",
        ]
        with mock.patch.object(sql_only, "psql", side_effect=replies):
            proof = sql_only.explain_and_sample(cfg)
        self.assertTrue(proof["direct_and_cached_rows_equal"])
        self.assertIn("SELECT *", proof["query"])

    def test_cold_probe_requires_exact_one_miss_fill_then_hit(self) -> None:
        cfg = config()
        snapshots = [
            counters(hits=10, misses=20, fills=20),
            counters(hits=11, misses=21, fills=21),
        ]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=5
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(
            sql_only, "psql", return_value="7|1|x\n7|1|x"
        ) as psql:
            proof = sql_only.cold_miss_fill_hit_proof(cfg)
        self.assertEqual(proof["status"], "PASS")
        self.assertEqual(proof["sql_cache_hits_during_measurement"], 1)
        script = psql.call_args.args[1]
        self.assertIn("PREPARE pglc_cold", script)
        self.assertEqual(script.count("EXECUTE pglc_cold(7, 1)"), 2)

    def test_cold_probe_rejects_counter_contamination(self) -> None:
        cfg = config()
        snapshots = [counters(), counters(hits=2, misses=1, fills=1)]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=0
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(
            sql_only, "psql", return_value="row\nrow"
        ):
            with self.assertRaisesRegex(RuntimeError, "accounting mismatch"):
                sql_only.cold_miss_fill_hit_proof(cfg)

    def test_complete_warm_pass_fills_every_key_exactly_once(self) -> None:
        cfg = config(keys=3)
        snapshots = [counters(), counters(misses=3, fills=3)]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=1
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(sql_only, "psql", return_value="") as psql:
            proof = sql_only.warm_all_keys(cfg)
        script = psql.call_args.args[1]
        self.assertIn("EXECUTE pglc_warm(7, 1)", script)
        self.assertIn("EXECUTE pglc_warm(7, 2)", script)
        self.assertIn("EXECUTE pglc_warm(7, 3)", script)
        self.assertEqual(proof["keys_filled"], 3)
        self.assertTrue(psql.call_args.kwargs["discard_rows"])

    def test_full_row_integrity_checks_bounds_sentinels_and_exact_hits(self) -> None:
        cfg = config(keys=8)
        rows = "7|1|first\n7|4|middle\n7|8|last"
        replies = ["8|1|8|8|t", rows, rows]
        snapshots = [
            counters(hits=10),
            counters(hits=10),
            counters(hits=10),
            counters(hits=13),
        ]
        with mock.patch.object(
            sql_only, "psql", side_effect=replies
        ) as psql, mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ):
            proof = sql_only.full_row_integrity_proof(cfg)
        self.assertEqual(proof["source_row_count"], 8)
        self.assertEqual(proof["sentinel_keys"], [1, 4, 8])
        self.assertTrue(proof["direct_and_cached_rows_equal"])
        self.assertEqual(
            proof["cached_counter_deltas"][
                "sql_cache_hits_during_measurement"
            ],
            3,
        )
        direct_script = psql.call_args_list[1].args[1]
        cached_script = psql.call_args_list[2].args[1]
        self.assertIn("pg_local_cache.sql_cache = off", direct_script)
        self.assertIn("pg_local_cache.sql_cache = on", cached_script)
        self.assertIn("EXECUTE pglc_integrity(7, 4)", cached_script)

    def test_full_row_integrity_rejects_negative_or_wrong_sentinel_rows(self) -> None:
        cfg = config(keys=8)
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=["8|1|8|8|t", "7|1|first\n7|8|last"],
        ), mock.patch.object(
            sql_only,
            "read_stats",
            side_effect=[counters(), counters()],
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 3 rows"):
                sql_only.full_row_integrity_proof(cfg)


class ValidationAndReportTests(unittest.TestCase):
    def test_valid_report_has_independent_protocol_gates_and_exact_counters(self) -> None:
        report = valid_report()
        self.assertEqual(sql_only.validate_report(report), [])

    def test_cached_hit_accounting_must_equal_successful_selects(self) -> None:
        report = valid_report()
        cached_run = report["protocols"]["prepared"]["cached_mode"]["runs"][0]
        cached_run["sql_cache_hits_during_measurement"] -= 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("non-exact hit accounting" in item for item in failures))

    def test_direct_mode_must_not_touch_any_sql_cache_counter(self) -> None:
        report = valid_report()
        direct_run = report["protocols"]["extended"]["direct_mode"]["runs"][0]
        direct_run["sql_cache_bypasses_during_measurement"] = 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("touched sql_cache_bypasses" in item for item in failures))

    def test_each_protocol_fails_its_own_10k_gate(self) -> None:
        report = valid_report()
        report["protocols"]["extended"] = lane("extended", rate=9_999.0)
        failures = sql_only.validate_report(report)
        self.assertTrue(any("extended cached median" in item for item in failures))
        self.assertFalse(any("prepared cached median" in item for item in failures))

    def test_cold_proof_is_exact_not_at_least(self) -> None:
        report = valid_report()
        report["cold_miss_fill_hit_proof"]["sql_cache_hits_during_measurement"] = 2
        failures = sql_only.validate_report(report)
        self.assertTrue(any("exactly 1" in item for item in failures))

    def test_warm_proof_requires_exact_miss_and_fill_for_every_key(self) -> None:
        report = valid_report()
        report["complete_keyspace_warm"][
            "sql_cache_fills_during_measurement"
        ] -= 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("warm proof" in item for item in failures))

    def test_full_row_integrity_proof_is_required_and_exact(self) -> None:
        report = valid_report()
        report["full_row_integrity_proof"]["cached_counter_deltas"][
            "sql_cache_hits_during_measurement"
        ] = 2
        failures = sql_only.validate_report(report)
        self.assertTrue(any("cached sentinel counters" in item for item in failures))

        report = valid_report()
        del report["full_row_integrity_proof"]
        failures = sql_only.validate_report(report)
        self.assertTrue(any("integrity proof is missing" in item for item in failures))

    def test_markdown_leads_with_high_signal_sql_throughput(self) -> None:
        markdown = sql_only.render_markdown(valid_report())
        self.assertIn("# pg_local_cache SQL-only benchmark", markdown)
        self.assertIn("## Headline throughput", markdown)
        self.assertIn("| prepared | 10 000 ops/s | 20 000 ops/s | 2.00x", markdown)
        self.assertIn("| extended |", markdown)
        self.assertIn("cold read -> fill -> warm read", markdown)
        self.assertIn("direct/cached integrity sample", markdown)
        self.assertIn("pg_local_cache.port=0", markdown)
        self.assertIn("independent >=10k", markdown)

    def test_report_writer_publishes_json_and_markdown_atomically(self) -> None:
        report = valid_report()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            sql_only.write_report(report, output)
            parsed = json.loads((output / "sql-only.json").read_text())
            markdown = (output / "sql-only.md").read_text()
            self.assertEqual(parsed["gate"]["status"], "PASS")
            self.assertIn("Headline throughput", markdown)
            self.assertFalse((output / ".sql-only.json.tmp").exists())
            self.assertFalse((output / ".sql-only.md.tmp").exists())

    def test_failure_writer_never_leaves_stale_success_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "sql-only.json").write_text("stale")
            (output / "sql-only.md").write_text("stale")
            try:
                raise RuntimeError("benchmark exploded")
            except RuntimeError as error:
                sql_only.write_failure_report(error, output)
            self.assertFalse((output / "sql-only.json").exists())
            self.assertFalse((output / "sql-only.md").exists())
            failure = json.loads((output / "sql-only-failure.json").read_text())
            self.assertEqual(failure["status"], "FAIL")
            self.assertEqual(failure["error_type"], "RuntimeError")

    def test_harness_has_no_resp_client_or_token_dependency(self) -> None:
        source = (ROOT / "benchmarks" / "sql_only.py").read_text()
        self.assertNotIn("RespConnection", source)
        self.assertNotIn("AUTH_TOKEN", source)
        self.assertNotIn("PGLC_BENCH_AUTH_TOKEN", source)


if __name__ == "__main__":
    unittest.main()
