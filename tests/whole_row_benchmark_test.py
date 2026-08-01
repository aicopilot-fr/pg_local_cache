#!/usr/bin/env python3
"""Source/unit contracts for the separate whole-row benchmark harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_whole_row_benchmark",
    ROOT / "benchmarks" / "whole_row.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load benchmarks/whole_row.py")
whole_row = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = whole_row
SPEC.loader.exec_module(whole_row)
compare = whole_row.compare


def config(**overrides: object) -> object:
    values: dict[str, object] = {
        "duration": 1.0,
        "warmup_seconds": 0.0,
        "repetitions": 1,
        "concurrency": 2,
        "pipeline": 2,
        "keys": 8,
        "value_size": 64,
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


def throughput_summary(rate: float) -> dict[str, float]:
    return {
        "median_operations_per_second": rate,
        "mean_operations_per_second": rate,
        "minimum_operations_per_second": rate,
        "maximum_operations_per_second": rate,
        "coefficient_of_variation_percent": 0.0,
        "median_p50_ms": 1.0,
        "median_p95_ms": 2.0,
        "median_p99_ms": 3.0,
    }


class WholeRowDefinitionTests(unittest.TestCase):
    def test_independent_gates_default_to_ten_thousand(self) -> None:
        with mock.patch.dict(
            whole_row.os.environ,
            {
                "PGLC_BENCH_AUTH_TOKEN": "test-token",
                "PGLC_BENCH_PG_PASSWORD": "test-password",
            },
            clear=True,
        ):
            parsed = whole_row.WholeRowConfig.from_environment()
        self.assertEqual(parsed.resp_min_ops, 10_000.0)
        self.assertEqual(parsed.sql_min_ops, 10_000.0)
        self.assertEqual(parsed.width_min_ops, 0.0)
        self.assertEqual(parsed.payload_sizes, (64, 512, 2048))

    def test_gates_and_widths_are_parsed_independently(self) -> None:
        with mock.patch.dict(
            whole_row.os.environ,
            {
                "PGLC_BENCH_AUTH_TOKEN": "test-token",
                "PGLC_BENCH_PG_PASSWORD": "test-password",
                "PGLC_BENCH_ROW_RESP_MIN_OPS": "11000",
                "PGLC_BENCH_ROW_SQL_MIN_OPS": "12000",
                "PGLC_BENCH_ROW_WIDTH_MIN_OPS": "9000",
                "PGLC_BENCH_ROW_PAYLOAD_SIZES": "64, 512,64,2048",
            },
            clear=True,
        ):
            parsed = whole_row.WholeRowConfig.from_environment()
        self.assertEqual(parsed.resp_min_ops, 11_000.0)
        self.assertEqual(parsed.sql_min_ops, 12_000.0)
        self.assertEqual(parsed.width_min_ops, 9_000.0)
        self.assertEqual(parsed.payload_sizes, (64, 512, 2048))

    def test_width_parser_fails_closed_on_oversized_or_empty_items(self) -> None:
        for raw in ("64,,512", "0", "3001", "wide"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                whole_row.parse_payload_sizes(raw)

    def test_kvik_keys_reverse_json_fields_but_keep_composite_values(self) -> None:
        key = whole_row.row_key(42)
        self.assertEqual(
            key,
            b'CRUD:benchmark.public.pg_local_cache_whole_row_comparison:'
            b'{"id":42,"tenant_id":7}',
        )

    def test_variable_batches_are_complete_disjoint_and_balanced(self) -> None:
        cfg = config()
        frames = [f"frame-{index}".encode() for index in range(cfg.keys)]
        seen: list[set[int]] = []
        for worker in range(cfg.concurrency):
            batches = whole_row.build_variable_batches(frames, worker, cfg)
            indexes = {
                index for _, batch_indexes in batches for index in batch_indexes
            }
            self.assertEqual(len(indexes), cfg.keys // cfg.concurrency)
            self.assertTrue(all(len(batch) == cfg.pipeline for _, batch in batches))
            seen.append(indexes)
        self.assertFalse(seen[0].intersection(seen[1]))
        self.assertEqual(set.union(*seen), set(range(cfg.keys)))

    def test_every_variable_response_is_checked_against_its_own_row(self) -> None:
        source = (ROOT / "benchmarks" / "whole_row.py").read_text()
        worker = source[source.index("def variable_worker(") :]
        worker = worker[: worker.index("\ndef run_variable_resp_load(")]
        self.assertIn("response != expected[index]", worker)
        external = source[source.index("def populate_external_target(") :]
        external = external[: external.index("\ndef get_frames(")]
        self.assertIn("row_key(index + 1), expected[index]", external)

    def test_pg_local_cache_is_reset_before_the_full_keyspace_warm_pass(self) -> None:
        connection = mock.MagicMock()
        connection.command.return_value = 17
        with mock.patch.object(
            compare, "RespConnection", return_value=connection
        ) as constructor:
            removed = whole_row.reset_pg_local_cache(config())
        constructor.assert_called_once_with(compare.TARGETS[0], "test-token")
        connection.command.assert_called_once_with("INVALIDATE", "CRUD")
        connection.close.assert_called_once_with()
        self.assertEqual(removed, 17)

        source = (ROOT / "benchmarks" / "whole_row.py").read_text()
        comparison = source[source.index("def resp_comparison(") :]
        comparison = comparison[: comparison.index("\ndef sql_lookup_script(")]
        self.assertLess(
            comparison.index("reset_pg_local_cache(config)"),
            comparison.index("stabilize_pg_local_cache(config, frames, expected)"),
        )

    def test_pg_local_cache_warm_pass_is_counter_verified(self) -> None:
        snapshots = [
            {"cache_misses": 10, "database_reads": 10},
            {"cache_misses": 14, "database_reads": 14},
            {"cache_misses": 14, "database_reads": 14},
            {"cache_misses": 14, "database_reads": 14},
        ]
        with mock.patch.object(
            compare, "read_pglc_stats", side_effect=snapshots
        ), mock.patch.object(whole_row, "warm_variable") as warm:
            result = whole_row.stabilize_pg_local_cache(
                config(), [b"frame"], [b"value"], max_passes=2
            )
        self.assertEqual(
            result,
            {
                "passes": 2,
                "cache_misses_before_stable": 4,
                "database_reads_before_stable": 4,
            },
        )
        self.assertEqual(warm.call_count, 2)
        warm.assert_called_with(
            compare.TARGETS[0], config(), [b"frame"], [b"value"]
        )

    def test_pg_local_cache_stabilization_fails_closed(self) -> None:
        snapshots = [
            {"cache_misses": 0, "database_reads": 0},
            {"cache_misses": 1, "database_reads": 1},
            {"cache_misses": 1, "database_reads": 1},
            {"cache_misses": 2, "database_reads": 2},
        ]
        with mock.patch.object(
            compare, "read_pglc_stats", side_effect=snapshots
        ), mock.patch.object(whole_row, "warm_variable"):
            with self.assertRaisesRegex(RuntimeError, "did not reach a fully warm"):
                whole_row.stabilize_pg_local_cache(
                    config(), [b"frame"], [b"value"], max_passes=2
                )

    def test_sql_lanes_cover_star_projection_and_predicate_order(self) -> None:
        self.assertEqual(
            set(whole_row.SQL_LANES),
            {
                "select_star",
                "reordered_projection",
                "composite_predicate_reordered",
            },
        )
        self.assertIn("SELECT *", whole_row.SQL_LANES["select_star"])
        self.assertIn(
            "SELECT metadata, payload",
            whole_row.SQL_LANES["reordered_projection"],
        )
        reordered = whole_row.SQL_LANES["composite_predicate_reordered"]
        self.assertLess(reordered.index("id = :key"), reordered.index("tenant_id = 7"))
        script = whole_row.sql_lookup_script(reordered, config())
        self.assertEqual(script.count("SELECT payload"), 2)
        self.assertIn("\\startpipeline", script)
        self.assertIn("\\endpipeline", script)

    def test_whole_row_report_runs_before_prior_harness_failures_propagate(self) -> None:
        runner = (ROOT / "benchmarks" / "run.sh").read_text()
        invocation = runner.index("/usr/local/lib/pg_local_cache/whole_row.py")
        prior_exit = runner.index(
            'if ((benchmark_status != 0)); then\n    exit "$benchmark_status"'
        )
        self.assertLess(invocation, prior_exit)


class WholeRowReportingTests(unittest.TestCase):
    def test_report_is_atomic_and_names_separate_lanes(self) -> None:
        run = {
            "operations_per_second": 10_000.0,
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
            "errors": 0,
        }
        summary = throughput_summary(10_000.0)
        targets = {
            name: {"version": "1", "runs": [run], "summary": summary}
            for name in ("pg_local_cache", "valkey", "redis")
        }
        sql_result = {
            "summary": summary,
            "runs": [{"failed_batches": 0}],
        }
        report = {
            "generated_at_utc": "2026-08-01T00:00:00+00:00",
            "resp_full_row": {"targets": targets},
            "ordinary_sql": {
                name: {
                    "mapped_postgres": sql_result,
                    "stock_postgres": sql_result,
                }
                for name in whole_row.SQL_LANES
            },
            "resp_payload_width_sweep": {
                "64": {
                    "payload_text_bytes": 64,
                    "response_bytes_min": 160,
                    "response_bytes_max": 180,
                    "summary": summary,
                }
            },
            "gate": {"status": "PASS", "message": "test"},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "whole-row-failure.json").write_text("stale")
            (output / "whole-row-failure.md").write_text("stale")
            whole_row.write_report(report, output)
            markdown = (output / "whole-row.md").read_text()
            self.assertIn("Full-row RESP GET", markdown)
            self.assertIn("Ordinary SQL whole-row/projection lanes", markdown)
            self.assertIn("response-width sweep", markdown)
            self.assertTrue((output / "whole-row.json").is_file())
            self.assertFalse((output / ".whole-row.json.tmp").exists())
            self.assertFalse((output / "whole-row-failure.json").exists())


if __name__ == "__main__":
    unittest.main()
