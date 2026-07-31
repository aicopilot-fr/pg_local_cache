#!/usr/bin/env python3
"""Source-level tests for the comparative benchmark harness."""

from __future__ import annotations

from array import array
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_compare", ROOT / "benchmarks" / "compare.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load benchmarks/compare.py")
compare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compare
SPEC.loader.exec_module(compare)


def config(**overrides: object) -> object:
    values: dict[str, object] = {
        "duration": 1.0,
        "warmup_seconds": 0.0,
        "repetitions": 3,
        "concurrency": 2,
        "pipeline": 4,
        "keys": 16,
        "value_size": 8,
        "max_latency_samples": 1000,
        "min_ops": 10_000.0,
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


class RespCodecTests(unittest.TestCase):
    def connection_for(self, response: bytes) -> tuple[object, socket.socket]:
        client, server = socket.socketpair()
        client.settimeout(1)
        connection = object.__new__(compare.RespConnection)
        connection.target = compare.Target("test", "local", 0, "version")
        connection.socket = client
        connection.buffer = bytearray()
        connection.position = 0
        server.sendall(response)
        return connection, server

    def test_command_encoder_is_binary_safe(self) -> None:
        encoded = compare.RespConnection.encode_command(
            "SET", b"key", b"a\x00b"
        )
        self.assertEqual(
            encoded,
            b"*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$3\r\na\x00b\r\n",
        )

    def test_response_decoder_handles_all_supported_shapes(self) -> None:
        connection, server = self.connection_for(
            b"+OK\r\n:42\r\n$3\r\na\x00b\r\n"
            b"$-1\r\n*2\r\n+one\r\n:2\r\n"
        )
        try:
            self.assertEqual(connection.read_response(), "OK")
            self.assertEqual(connection.read_response(), 42)
            self.assertEqual(connection.read_response(), b"a\x00b")
            self.assertIsNone(connection.read_response())
            self.assertEqual(connection.read_response(), ["one", 2])
        finally:
            connection.close()
            server.close()

    def test_response_decoder_raises_server_errors(self) -> None:
        connection, server = self.connection_for(b"-ERR broken\r\n")
        try:
            with self.assertRaisesRegex(compare.RespError, "ERR broken"):
                connection.read_response()
        finally:
            connection.close()
            server.close()


class ReportingTests(unittest.TestCase):
    def test_latency_reservoir_samples_the_whole_stream(self) -> None:
        samples = array("d")
        generator = mock.Mock()
        generator.randrange.side_effect = [0, 3]

        compare.add_reservoir_sample(samples, 1.0, 1, 2, generator)
        compare.add_reservoir_sample(samples, 2.0, 2, 2, generator)
        compare.add_reservoir_sample(samples, 3.0, 3, 2, generator)
        compare.add_reservoir_sample(samples, 4.0, 4, 2, generator)

        self.assertEqual(list(samples), [3.0, 2.0])
        self.assertEqual(
            generator.randrange.call_args_list,
            [mock.call(3), mock.call(4)],
        )

    def test_latency_percentile_weights_completed_operations(self) -> None:
        samples = [(1.0, 1.0), (100.0, 9.0)]
        self.assertEqual(compare.weighted_percentile(samples, 50), 100.0)
        self.assertEqual(compare.weighted_percentile(samples, 95), 100.0)

    def test_summaries_include_range_cv_and_latency_medians(self) -> None:
        runs = [
            {
                "operations_per_second": 100.0,
                "p50_ms": 1.0,
                "p95_ms": 2.0,
                "p99_ms": 3.0,
            },
            {
                "operations_per_second": 200.0,
                "p50_ms": 2.0,
                "p95_ms": 3.0,
                "p99_ms": 4.0,
            },
            {
                "operations_per_second": 300.0,
                "p50_ms": 3.0,
                "p95_ms": 4.0,
                "p99_ms": 5.0,
            },
        ]
        summary = compare.summarize_resp_runs(runs)
        self.assertEqual(summary["median_operations_per_second"], 200.0)
        self.assertEqual(summary["minimum_operations_per_second"], 100.0)
        self.assertEqual(summary["maximum_operations_per_second"], 300.0)
        self.assertGreater(summary["coefficient_of_variation_percent"], 0)
        self.assertEqual(summary["median_p99_ms"], 4.0)

    def test_pgbench_script_matches_pipeline_depth(self) -> None:
        script = compare.pgbench_script(config(pipeline=3, keys=99))
        self.assertTrue(script.startswith("\\startpipeline\n"))
        self.assertTrue(script.endswith("\\endpipeline\n"))
        self.assertEqual(script.count("SELECT value"), 3)
        self.assertEqual(script.count("random(1, 99)"), 3)

    def test_worker_batches_cover_every_workflow_key(self) -> None:
        width = 6
        for key_count in (1024, 16384):
            frames = [
                f"{index:0{width}d}".encode()
                for index in range(key_count)
            ]
            for pipeline in (1, 32):
                current = config(
                    concurrency=16,
                    keys=key_count,
                    pipeline=pipeline,
                )
                current.validate()
                observed: set[bytes] = set()
                for worker in range(current.concurrency):
                    batches = compare.build_worker_batches(
                        frames, worker, current
                    )
                    for batch in batches:
                        self.assertEqual(
                            len(batch), width * current.pipeline
                        )
                        observed.update(
                            batch[offset : offset + width]
                            for offset in range(
                                0, len(batch), width
                            )
                        )
                self.assertEqual(observed, set(frames))

    def test_configuration_rejects_incomplete_or_overloaded_lanes(self) -> None:
        with self.assertRaisesRegex(ValueError, "divisible"):
            config(keys=17, concurrency=4).validate()
        with self.assertRaisesRegex(ValueError, "exceed"):
            config(
                keys=256,
                concurrency=256,
                pipeline=1,
                pg_local_cache_workers=1,
            ).validate()
        with self.assertRaisesRegex(ValueError, "equal request weight"):
            config(keys=24, concurrency=4, pipeline=4).validate()

    def test_pgbench_output_parser_reports_value_operations(self) -> None:
        output = """\
transaction type: test.sql
number of transactions actually processed: 1000
number of failed transactions: 0 (0.000%)
latency average = 1.250 ms
tps = 800.000000 (without initial connection time)
"""
        completed = subprocess.CompletedProcess([], 0, output, "")
        with mock.patch.object(
            compare.subprocess, "run", return_value=completed
        ) as run:
            result = compare.run_pgbench_once(
                config(pipeline=4), Path("/tmp/test.sql"), 1, 42
            )
        self.assertEqual(result["successful_operations"], 4000)
        self.assertEqual(result["operations_per_second"], 3200.0)
        self.assertEqual(result["failed_batches"], 0)
        kwargs = run.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertNotIn("environment", kwargs)

    def test_reports_are_written_atomically(self) -> None:
        run = {
            "operations_per_second": 10000.0,
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
            "errors": 0,
            "client_cpu_quota_utilization_percent": 50.0,
        }
        summary = compare.summarize_resp_runs([run])
        report = {
            "generated_at_utc": "2026-07-31T00:00:00+00:00",
            "workload": {
                "duration_seconds": 120,
                "warmup_seconds": 15,
                "repetitions": 1,
                "concurrency": 16,
                "pipeline": 32,
                "keys": 16384,
                "value_size": 128,
                "max_latency_samples": 1_000_000,
                "server_cpus": 4,
                "client_cpus": 4,
                "client_memory": "1g",
                "pg_local_cache_workers": 4,
            },
            "resp_targets": {
                name: {"version": "1", "runs": [run], "summary": summary}
                for name in ("pg_local_cache", "valkey", "redis")
            },
            "postgres_reference": {
                "operations_per_batch": 32,
                "summary": compare.summarize_throughput_runs(
                    [{"operations_per_second": 5000.0}]
                ),
            },
            "images": {
                name: {"image": name, "identity": "sha256:test"}
                for name in ("pg_local_cache", "valkey", "redis")
            },
            "gate": {"status": "PASS", "message": "test"},
        }
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "failure.json").write_text(
                "stale", encoding="utf-8"
            )
            (Path(directory) / "failure.md").write_text(
                "stale", encoding="utf-8"
            )
            compare.write_report(report, Path(directory))
            markdown = (Path(directory) / "comparison.md").read_text()
            self.assertIn("RESP warm GET", markdown)
            self.assertIn("Direct stock PostgreSQL reference", markdown)
            self.assertTrue((Path(directory) / "comparison.json").is_file())
            self.assertFalse((Path(directory) / ".comparison.json.tmp").exists())
            self.assertFalse((Path(directory) / "failure.json").exists())
            self.assertFalse((Path(directory) / "failure.md").exists())

    def test_failure_report_survives_early_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                compare.os.environ,
                {"PGLC_BENCH_OUTPUT_DIR": directory},
                clear=False,
            ):
                (Path(directory) / "comparison.json").write_text(
                    "stale", encoding="utf-8"
                )
                (Path(directory) / "comparison.md").write_text(
                    "stale", encoding="utf-8"
                )
                try:
                    raise RuntimeError("intentional failure")
                except RuntimeError as error:
                    compare.write_failure_report(error)
            failure = (Path(directory) / "failure.json").read_text()
            self.assertIn("intentional failure", failure)
            self.assertTrue((Path(directory) / "failure.md").is_file())
            self.assertFalse((Path(directory) / ".failure.json.tmp").exists())
            self.assertFalse((Path(directory) / "comparison.json").exists())
            self.assertFalse((Path(directory) / "comparison.md").exists())


if __name__ == "__main__":
    unittest.main()
