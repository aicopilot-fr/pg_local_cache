#!/usr/bin/env python3
"""Source-level tests for the extended benchmark scenario harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_scenarios", ROOT / "benchmarks" / "scenarios.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load benchmarks/scenarios.py")
scenarios = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scenarios
SPEC.loader.exec_module(scenarios)
compare = scenarios.compare


def config(**overrides: object) -> object:
    values: dict[str, object] = {
        "duration": 1.0,
        "warmup_seconds": 0.0,
        "repetitions": 1,
        "concurrency": 2,
        "pipeline": 2,
        "keys": 8,
        "value_size": 8,
        "max_latency_samples": 1000,
        "min_ops": 0.0,
        "client_cpus": 2.0,
        "server_cpus": 2.0,
        "client_memory": "1g",
        "server_memory": "1g",
        "pg_local_cache_workers": 1,
        "pg_jobs": 2,
        "output_directory": Path("/tmp/results"),
        "auth_token": "test-token",
        "pg_password": "test-password",
    }
    values.update(overrides)
    return compare.Config(**values)


class ScenarioDefinitionTests(unittest.TestCase):
    def test_runner_collects_scenarios_before_propagating_main_failure(
        self,
    ) -> None:
        runner = (ROOT / "benchmarks" / "run.sh").read_text()
        scenario_invocation = runner.index(
            "/usr/local/lib/pg_local_cache/scenarios.py"
        )
        main_failure_exit = runner.index(
            'if ((benchmark_status != 0)); then\n    exit "$benchmark_status"'
        )
        self.assertLess(scenario_invocation, main_failure_exit)

    def test_sql_throughput_gate_defaults_to_ten_thousand(self) -> None:
        with mock.patch.dict(
            scenarios.os.environ,
            {
                "PGLC_BENCH_AUTH_TOKEN": "test-token",
                "PGLC_BENCH_PG_PASSWORD": "test-password",
            },
            clear=True,
        ):
            scenario = scenarios.ScenarioConfig.from_environment()
        self.assertEqual(scenario.sql_min_ops, 10_000.0)
        self.assertEqual(scenario.sql_extended_min_ops, 10_000.0)

    def test_extended_sql_gate_is_parsed_independently(self) -> None:
        with mock.patch.dict(
            scenarios.os.environ,
            {
                "PGLC_BENCH_AUTH_TOKEN": "test-token",
                "PGLC_BENCH_PG_PASSWORD": "test-password",
                "PGLC_BENCH_SQL_MIN_OPS": "12000",
                "PGLC_BENCH_SQL_EXTENDED_MIN_OPS": "9000",
            },
            clear=True,
        ):
            scenario = scenarios.ScenarioConfig.from_environment()
        self.assertEqual(scenario.sql_min_ops, 12_000.0)
        self.assertEqual(scenario.sql_extended_min_ops, 9_000.0)

    def test_extended_sql_gate_is_forwarded_to_benchmark_container(
        self,
    ) -> None:
        compose = (ROOT / "benchmarks" / "compose.yaml").read_text()
        self.assertIn(
            'PGLC_BENCH_SQL_EXTENDED_MIN_OPS: '
            '"${PGLC_BENCH_SQL_EXTENDED_MIN_OPS:-}"',
            compose,
        )

    def test_boolean_gate_parser_is_explicit(self) -> None:
        with mock.patch.dict(
            scenarios.os.environ,
            {"PGLC_TEST_BOOL": "yes"},
            clear=False,
        ):
            self.assertTrue(scenarios.env_bool("PGLC_TEST_BOOL", False))
        with mock.patch.dict(
            scenarios.os.environ,
            {"PGLC_TEST_BOOL": "sometimes"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "boolean"):
                scenarios.env_bool("PGLC_TEST_BOOL", False)

    def test_setup_sql_is_single_statement(self) -> None:
        self.assertEqual(
            scenarios.normalized_setup_sql(
                "SET pg_local_cache.sql_cache = on"
            ),
            "SET pg_local_cache.sql_cache = on;",
        )
        self.assertIsNone(scenarios.normalized_setup_sql("  "))
        for invalid in (
            "SET a=1; SET b=2;",
            "SET a=1; SELECT 1",
            "\\set mode on",
            "SET a=1\nSELECT 1",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    scenarios.normalized_setup_sql(invalid)

    def test_lookup_lanes_use_same_ordinary_parameterized_query(self) -> None:
        direct = scenarios.lookup_script(
            scenarios.ORDINARY_LOOKUP,
            keys=100,
            pipeline=3,
        )
        cached = scenarios.lookup_script(
            scenarios.ORDINARY_LOOKUP,
            keys=100,
            pipeline=3,
        )
        self.assertEqual(direct, cached)
        self.assertEqual(direct.count("SELECT value"), 3)
        self.assertIn("WHERE id = :key_0;", direct)
        self.assertIn("WHERE id = :key_2;", direct)
        self.assertEqual(direct.count("\\startpipeline"), 1)
        self.assertEqual(direct.count("\\endpipeline"), 1)

    def test_setup_sql_becomes_a_libpq_session_option(self) -> None:
        self.assertEqual(
            scenarios.setup_sql_to_pgoptions(
                "SET pg_local_cache.sql_cache = on;"
            ),
            "-c pg_local_cache.sql_cache=on",
        )
        self.assertEqual(
            scenarios.setup_sql_to_pgoptions(
                "SET SESSION pg_local_cache.sql_cache=off;"
            ),
            "-c pg_local_cache.sql_cache=off",
        )
        with self.assertRaisesRegex(ValueError, "simple SET"):
            scenarios.setup_sql_to_pgoptions("SELECT local_cache.enable();")

    def test_write_script_counts_sql_statements_not_batches(self) -> None:
        script = scenarios.write_script(
            keys=50, value_size=16, pipeline=4, concurrency=2
        )
        self.assertEqual(script.count("UPDATE public."), 4)
        self.assertIn("(:client_id * 25) + 1", script)
        self.assertIn("(:client_id * 25) + 4", script)
        self.assertNotIn("random(", script)
        self.assertIn("repeat('y', 16)", script)

    def test_write_script_rejects_overlapping_client_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "disjoint key range"):
            scenarios.write_script(
                keys=8, value_size=16, pipeline=5, concurrency=2
            )

    def test_fixed_partition_is_complete_disjoint_and_balanced(self) -> None:
        frames = [index.to_bytes(4, "big") for index in range(16)]
        partitions = scenarios.partition_frames(frames, 4, 2)
        self.assertEqual([len(batches) for batches in partitions], [2] * 4)
        observed = b"".join(
            batch for batches in partitions for batch in batches
        )
        for frame in frames:
            self.assertEqual(observed.count(frame), 1)
        with self.assertRaisesRegex(ValueError, "divisible"):
            scenarios.partition_frames(frames[:15], 4, 2)

    def test_nearest_rank_percentiles_include_short_stampede_waves(self) -> None:
        values = [1.0, 2.0, 3.0, 100.0]
        self.assertEqual(scenarios.percentile(values, 50), 2.0)
        self.assertEqual(scenarios.percentile(values, 95), 100.0)
        self.assertEqual(scenarios.percentile([], 99), 0.0)


class PgbenchTests(unittest.TestCase):
    OUTPUT = """\
transaction type: scenario.sql
number of transactions actually processed: 100
number of failed transactions: 0 (0.000%)
latency average = 1.250 ms
tps = 80.000000 (without initial connection time)
"""

    def test_parser_converts_pipeline_batches_to_operations(self) -> None:
        result = scenarios.parse_pgbench_output(self.OUTPUT, 4)
        self.assertEqual(result["successful_batches"], 100)
        self.assertEqual(result["successful_operations"], 400)
        self.assertEqual(result["operations_per_second"], 320.0)
        self.assertEqual(result["operations_per_batch"], 4)

    def test_parser_keeps_failed_batches_out_of_successful_operations(
        self,
    ) -> None:
        output = self.OUTPUT.replace(
            "number of transactions actually processed: 100\n"
            "number of failed transactions: 0 (0.000%)",
            "number of transactions actually processed: 98\n"
            "number of failed transactions: 2 (2.000%)",
        )
        result = scenarios.parse_pgbench_output(output, 4)
        self.assertEqual(result["successful_batches"], 98)
        self.assertEqual(result["successful_operations"], 392)
        self.assertEqual(result["failed_batches"], 2)

    def test_runner_requires_prepared_mode_and_selected_host(self) -> None:
        completed = subprocess.CompletedProcess([], 0, self.OUTPUT, "")
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ) as run:
            result = scenarios.run_pgbench_once(
                config(pipeline=4),
                "same-postgres",
                Path("/tmp/scenario.sql"),
                1,
                42,
            )
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-M") + 1], "prepared")
        self.assertEqual(arguments[arguments.index("-h") + 1], "same-postgres")
        self.assertNotIn("-d", arguments)
        self.assertEqual(arguments[-1], compare.PG_DATABASE)
        self.assertEqual(result["operations_per_second"], 320.0)
        self.assertEqual(result["query_protocol"], "prepared")

    def test_runner_selects_unnamed_extended_protocol(self) -> None:
        completed = subprocess.CompletedProcess([], 0, self.OUTPUT, "")
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ) as run:
            result = scenarios.run_pgbench_once(
                config(pipeline=4),
                "same-postgres",
                Path("/tmp/scenario.sql"),
                1,
                42,
                query_protocol="extended",
            )
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-M") + 1], "extended")
        self.assertEqual(result["query_protocol"], "extended")

    def test_runner_rejects_unknown_protocol_before_spawn(self) -> None:
        with mock.patch.object(scenarios.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "query protocol"):
                scenarios.run_pgbench_once(
                    config(),
                    "same-postgres",
                    Path("/tmp/scenario.sql"),
                    1,
                    42,
                    query_protocol="simple",
                )
        run.assert_not_called()

    def test_runner_applies_mode_before_connecting(self) -> None:
        completed = subprocess.CompletedProcess([], 0, self.OUTPUT, "")
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ) as run:
            scenarios.run_pgbench_once(
                config(),
                "same-postgres",
                Path("/tmp/scenario.sql"),
                1,
                42,
                "SET pg_local_cache.sql_cache = on;",
            )
        self.assertEqual(
            run.call_args.kwargs["env"]["PGOPTIONS"],
            "-c pg_local_cache.sql_cache=on",
        )

    def test_unparseable_output_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not parse"):
            scenarios.parse_pgbench_output("tps = unavailable", 1)

    def test_prepared_probe_uses_unprivileged_app_role_and_checks_values(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            f"{compare.PG_APP_USER}|f|f\nxxxxxxxx\nxxxxxxxx\n",
            "",
        )
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ) as run:
            result = scenarios.prepared_lookup_probe(
                config(keys=2, value_size=8),
                "same-postgres",
                "SET pg_local_cache.sql_cache = on;",
            )
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments[arguments.index("-U") + 1], compare.PG_APP_USER
        )
        self.assertIn("PREPARE pglc_lookup_probe(bigint)", arguments[-1])
        self.assertIn("WHERE id = $1", arguments[-1])
        self.assertEqual(
            run.call_args.kwargs["env"]["PGOPTIONS"],
            "-c pg_local_cache.sql_cache=on",
        )
        self.assertEqual(result["status"], "PASS")

    def test_prepared_probe_fails_on_wrong_custom_scan_value(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, f"{compare.PG_APP_USER}|f|f\nwrong\n", ""
        )
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong values"):
                scenarios.prepared_lookup_probe(
                    config(keys=1, value_size=8),
                    "same-postgres",
                    "SET pg_local_cache.sql_cache = on;",
                )

    def test_extended_probe_uses_bind_and_checks_values(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            f"{compare.PG_APP_USER}|f|f\nxxxxxxxx\nxxxxxxxx\n",
            "",
        )
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ) as run:
            result = scenarios.extended_lookup_probe(
                config(keys=2, value_size=8),
                "same-postgres",
                "SET pg_local_cache.sql_cache = on;",
            )
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["-f", "-"])
        self.assertNotIn("-c", arguments)
        script = run.call_args.kwargs["input"]
        self.assertEqual(script.count("WHERE id = $1"), 2)
        self.assertIn("\\bind 1\n\\g", script)
        self.assertIn("\\bind 2\n\\g", script)
        self.assertEqual(result["query_protocol"], "extended")
        self.assertEqual(result["validated_keys"], [1, 2])

    def test_extended_probe_fails_on_wrong_custom_scan_value(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, f"{compare.PG_APP_USER}|f|f\nwrong\n", ""
        )
        with mock.patch.object(
            scenarios.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong values"):
                scenarios.extended_lookup_probe(
                    config(keys=1, value_size=8),
                    "same-postgres",
                    "SET pg_local_cache.sql_cache = on;",
                )

    def test_cold_sql_probe_requires_miss_fill_then_hit(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "xxxxxxxx\nxxxxxxxx\n", "")
        controller = mock.Mock()
        controller.command.return_value = 8
        stats = [
            {
                "sql_cache_hits": 10,
                "sql_cache_misses": 20,
                "sql_cache_fills": 5,
                "sql_cache_bypasses": 3,
            },
            {
                "sql_cache_hits": 11,
                "sql_cache_misses": 21,
                "sql_cache_fills": 6,
                "sql_cache_bypasses": 3,
            },
        ]
        with (
            mock.patch.object(
                scenarios, "target_controller", return_value=controller
            ),
            mock.patch.object(
                scenarios.compare, "read_pglc_stats", side_effect=stats
            ),
            mock.patch.object(
                scenarios.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = scenarios.sql_cold_fill_probe(
                config(keys=8, value_size=8),
                mock.Mock(),
                "SET pg_local_cache.sql_cache = on;",
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sql_cache_hits_during_measurement"], 1)
        self.assertEqual(result["sql_cache_misses_during_measurement"], 1)
        self.assertEqual(result["sql_cache_fills_during_measurement"], 1)
        self.assertEqual(controller.command.call_args.args[0], "INVALIDATE")
        self.assertEqual(
            run.call_args.kwargs["env"]["PGOPTIONS"],
            "-c pg_local_cache.sql_cache=on",
        )
        self.assertIn("PREPARE pglc_cold_fill_probe", run.call_args.args[0][-1])

    def test_cold_sql_probe_fails_closed_on_counter_mismatch(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "xxxxxxxx\nxxxxxxxx\n", "")
        controller = mock.Mock()
        controller.command.return_value = 8
        unchanged = {
            "sql_cache_hits": 0,
            "sql_cache_misses": 0,
            "sql_cache_fills": 0,
            "sql_cache_bypasses": 0,
        }
        with (
            mock.patch.object(
                scenarios, "target_controller", return_value=controller
            ),
            mock.patch.object(
                scenarios.compare,
                "read_pglc_stats",
                side_effect=[unchanged, unchanged],
            ),
            mock.patch.object(
                scenarios.subprocess, "run", return_value=completed
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "miss/fill/hit"):
                scenarios.sql_cold_fill_probe(
                    config(keys=8, value_size=8),
                    mock.Mock(),
                    "SET pg_local_cache.sql_cache = on;",
                )

    def test_cold_sql_probe_can_use_unnamed_extended_protocol(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, "xxxxxxxx\nxxxxxxxx\n", ""
        )
        controller = mock.Mock()
        controller.command.return_value = 8
        before = {
            "sql_cache_hits": 10,
            "sql_cache_misses": 20,
            "sql_cache_fills": 5,
            "sql_cache_bypasses": 3,
        }
        after = {
            "sql_cache_hits": 11,
            "sql_cache_misses": 21,
            "sql_cache_fills": 6,
            "sql_cache_bypasses": 3,
        }
        with (
            mock.patch.object(
                scenarios, "target_controller", return_value=controller
            ),
            mock.patch.object(
                scenarios.compare,
                "read_pglc_stats",
                side_effect=[before, after],
            ),
            mock.patch.object(
                scenarios.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = scenarios.sql_cold_fill_probe(
                config(keys=8, value_size=8),
                mock.Mock(),
                "SET pg_local_cache.sql_cache = on;",
                "extended",
            )
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["-f", "-"])
        self.assertNotIn("-c", arguments)
        self.assertEqual(
            run.call_args.kwargs["input"].count("\\bind 1\n\\g"), 2
        )
        self.assertEqual(result["query_protocol"], "extended")


class ReportingTests(unittest.TestCase):
    @staticmethod
    def validation_report(
        cached_median: float,
        extended_cached_median: float = 10_000.0,
    ) -> dict[str, object]:
        counters = {
            "sql_cache_hits_during_measurement": 0,
            "sql_cache_misses_during_measurement": 0,
            "sql_cache_fills_during_measurement": 0,
            "sql_cache_bypasses_during_measurement": 0,
        }
        return {
            "workload": {
                "require_single_flight": False,
                "sql_min_ops": 10_000.0,
                "sql_extended_min_ops": 10_000.0,
            },
            "scenarios": {
                "resp_cold_get": {
                    "expected_database_reads_per_run": 0,
                    "runs": [],
                },
                "resp_warm_get": {"runs": []},
                "same_key_stampede": {
                    "rounds": [],
                    "concurrent_requests_per_round": 0,
                },
                "resp_mutations": {},
                "sql_write_invalidation": {
                    "mapped_postgres": {"runs": []},
                    "stock_postgres": {"runs": []},
                },
                "sql_direct_read": {"runs": []},
                "sql_cached_fast_path": {
                    "status": "MEASURED",
                    "query_protocol": "prepared",
                    "direct_mode": {
                        "runs": [],
                        "summary": {
                            "median_operations_per_second": 1.0
                        },
                        **counters,
                    },
                    "cached_mode": {
                        "runs": [],
                        "summary": {
                            "median_operations_per_second": cached_median
                        },
                        **counters,
                    },
                },
                "sql_cached_extended_protocol": {
                    "status": "MEASURED",
                    "query_protocol": "extended",
                    "direct_mode": {
                        "runs": [],
                        "summary": {
                            "median_operations_per_second": 1.0
                        },
                        **counters,
                    },
                    "cached_mode": {
                        "runs": [],
                        "summary": {
                            "median_operations_per_second": (
                                extended_cached_median
                            )
                        },
                        **counters,
                    },
                },
            },
        }

    def test_sql_cached_median_has_an_independent_gate(self) -> None:
        failures = scenarios.validate_report(self.validation_report(9_999.0))
        self.assertEqual(len(failures), 1)
        self.assertIn("below the 10000 ops/s gate", failures[0])
        self.assertEqual(
            scenarios.validate_report(self.validation_report(10_000.0)), []
        )

    def test_extended_cached_median_has_a_separate_gate(self) -> None:
        report = self.validation_report(10_000.0, 9_999.0)
        failures = scenarios.validate_report(report)
        self.assertEqual(len(failures), 1)
        self.assertIn("extended-protocol cached median", failures[0])
        self.assertIn("below the 10000 ops/s gate", failures[0])

    def test_extended_counters_are_validated_independently(self) -> None:
        report = self.validation_report(10_000.0, 10_000.0)
        extended = report["scenarios"]["sql_cached_extended_protocol"]
        extended["cached_mode"]["sql_cache_bypasses_during_measurement"] = 1
        failures = scenarios.validate_report(report)
        self.assertEqual(len(failures), 1)
        self.assertIn("extended-protocol cached mode", failures[0])

    def test_counter_delta_names_the_measurement_window(self) -> None:
        self.assertEqual(
            scenarios.counter_delta(
                {"cache_hits": 10, "database_reads": 2},
                {"cache_hits": 15, "database_reads": 3},
                "cache_hits",
                "database_reads",
            ),
            {
                "cache_hits_during_measurement": 5,
                "database_reads_during_measurement": 1,
            },
        )

    def test_scenario_report_is_written_atomically(self) -> None:
        summary = {
            "median_operations_per_second": 100.0,
            "minimum_operations_per_second": 100.0,
            "maximum_operations_per_second": 100.0,
            "coefficient_of_variation_percent": 0.0,
            "median_p50_ms": 1.0,
            "median_p95_ms": 2.0,
            "median_p99_ms": 3.0,
        }
        write_summary = {
            key: summary[key]
            for key in (
                "median_operations_per_second",
                "minimum_operations_per_second",
                "maximum_operations_per_second",
                "coefficient_of_variation_percent",
            )
        }
        report = {
            "generated_at_utc": "2026-08-01T00:00:00+00:00",
            "scenarios": {
                "resp_warm_get": {"summary": summary},
                "resp_cold_get": {"summary": summary},
                "same_key_stampede": {
                    "concurrent_requests_per_round": 2,
                    "rounds": [{}, {}],
                    "database_reads_per_round": 1.0,
                    "database_reads_per_request": 0.5,
                },
                "resp_mutations": {
                    target: {
                        "set_summary": summary,
                        "del_summary": summary,
                    }
                    for target in ("pg_local_cache", "valkey", "redis")
                },
                "sql_direct_read": {"summary": write_summary},
                "sql_cached_fast_path": {
                    "status": "SKIPPED",
                    "reason": "feature disabled",
                },
                "sql_cached_extended_protocol": {
                    "status": "MEASURED",
                    "query_protocol": "extended",
                    "protocol_semantics": (
                        "unnamed extended protocol; Parse/Bind/Execute "
                        "per query"
                    ),
                    "query": scenarios.ORDINARY_LOOKUP,
                    "direct_setup": "SET pg_local_cache.sql_cache = off;",
                    "cached_setup": "SET pg_local_cache.sql_cache = on;",
                    "direct_mode": {"summary": write_summary},
                    "cached_mode": {
                        "summary": write_summary,
                        "sql_cache_hits_during_measurement": 100,
                        "sql_cache_bypasses_during_measurement": 0,
                    },
                    "cached_to_direct_throughput_ratio": 1.0,
                    "throughput_gate": {
                        "status": "PASS",
                        "minimum_cached_operations_per_second": 10_000,
                    },
                },
                "sql_write_invalidation": {
                    "mapped_postgres": {"summary": write_summary},
                    "stock_postgres": {"summary": write_summary},
                    "mapped_to_stock_throughput_ratio": 1.0,
                },
            },
            "gate": {"status": "PASS", "message": "test"},
        }
        with tempfile.TemporaryDirectory() as directory:
            scenarios.write_report(report, Path(directory))
            markdown = (Path(directory) / "scenarios.md").read_text()
            self.assertIn("Same-key cold stampede", markdown)
            self.assertIn("SQL cached fast-path: **SKIPPED**", markdown)
            self.assertIn(
                "SQL unnamed extended-protocol ordinary-query cache pair: "
                "**MEASURED**",
                markdown,
            )
            self.assertIn(
                "Protocol-specific throughput gate: **PASS** at 10 000",
                markdown,
            )
            self.assertIn("after commit", markdown)
            self.assertIn("not an active-cache invalidation", markdown)
            self.assertTrue((Path(directory) / "scenarios.json").is_file())
            self.assertFalse(
                (Path(directory) / ".scenarios.json.tmp").exists()
            )

    def test_failure_report_replaces_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "scenarios.json").write_text("stale")
            (output / "scenarios.md").write_text("stale")
            try:
                raise RuntimeError("intentional scenario failure")
            except RuntimeError as error:
                scenarios.write_failure_report(error, output)
            self.assertFalse((output / "scenarios.json").exists())
            self.assertFalse((output / "scenarios.md").exists())
            self.assertIn(
                "intentional scenario failure",
                (output / "scenarios-failure.json").read_text(),
            )
            self.assertTrue((output / "scenarios-failure.md").is_file())


if __name__ == "__main__":
    unittest.main()
