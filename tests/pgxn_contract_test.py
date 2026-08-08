#!/usr/bin/env python3
"""Contracts for the PGXN metadata, archive, and release integration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_pgxn_meta",
    SCRIPTS / "validate_pgxn_meta.py",
)
if VALIDATOR_SPEC is None or VALIDATOR_SPEC.loader is None:
    raise RuntimeError("could not load scripts/validate_pgxn_meta.py")
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
sys.modules[VALIDATOR_SPEC.name] = validator
VALIDATOR_SPEC.loader.exec_module(validator)

BUILDER_SPEC = importlib.util.spec_from_file_location(
    "build_pgxn_dist",
    SCRIPTS / "build_pgxn_dist.py",
)
if BUILDER_SPEC is None or BUILDER_SPEC.loader is None:
    raise RuntimeError("could not load scripts/build_pgxn_dist.py")
builder = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = builder
BUILDER_SPEC.loader.exec_module(builder)


class PgxnMetadataContracts(unittest.TestCase):
    def test_repository_metadata_is_valid_and_searchable(self) -> None:
        metadata = validator.validate_repository(ROOT)
        self.assertEqual(metadata["name"], "pg_local_cache")
        self.assertEqual(metadata["version"], "1.1.0")
        self.assertEqual(metadata["license"], "mit")
        self.assertEqual(metadata["release_status"], "stable")
        self.assertEqual(
            metadata["prereqs"]["runtime"]["requires"]["PostgreSQL"],
            ">= 14.0.0, < 19.0.0",
        )
        self.assertEqual(
            metadata["provides"]["pg_local_cache"]["file"],
            "sql/pg_local_cache--1.1.0.sql",
        )
        self.assertEqual(
            metadata["provides"]["pg_local_cache"]["docfile"],
            "README.md",
        )
        self.assertTrue(
            {
                "cache",
                "performance",
                "shared-memory",
                "primary-key",
                "custom-scan",
                "transactions",
                "sql",
                "postgresql",
            }.issubset(metadata["tags"])
        )

    def test_version_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            for directory in (
                ".github",
                "assets",
                "benchmarks",
                "docker",
                "monitoring",
                "scripts",
                "sql",
                "src",
                "tests",
            ):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for name in ("README.md", "LICENSE"):
                shutil.copy2(ROOT / name, root / name)
            shutil.copy2(ROOT / "META.json", root / "META.json")
            shutil.copy2(
                ROOT / "pg_local_cache.control",
                root / "pg_local_cache.control",
            )
            shutil.copy2(ROOT / "Makefile", root / "Makefile")
            shutil.copy2(
                ROOT / "sql" / "pg_local_cache--1.1.0.sql",
                root / "sql" / "pg_local_cache--1.1.0.sql",
            )
            shutil.copy2(
                ROOT / "src" / "pg_local_cache_worker.c",
                root / "src" / "pg_local_cache_worker.c",
            )
            metadata = json.loads((root / "META.json").read_text())
            metadata["version"] = "1.1.1"
            (root / "META.json").write_text(
                json.dumps(metadata, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                validator.MetadataError,
                "provides.pg_local_cache.file|versions must match",
            ):
                validator.validate_repository(root)

    def test_pgxn_documentation_discloses_activation_boundary(self) -> None:
        source = (ROOT / "PGXN.md").read_text(encoding="utf-8")
        for marker in (
            "shared_preload_libraries",
            "one controlled restart",
            "pgxn install",
            "configure the server",
            "Never reuse an existing semantic version",
            "PGXN credentials are intentionally not stored",
        ):
            self.assertIn(marker, source)


class PgxnArchiveContracts(unittest.TestCase):
    def test_make_dist_is_standalone(self) -> None:
        result = subprocess.run(
            ["make", "-n", "dist"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn("scripts/validate_pgxn_meta.py", result.stdout)
        self.assertIn("scripts/build_pgxn_dist.py", result.stdout)
        self.assertNotIn("--pgxs", result.stdout)

    def test_builder_creates_pgxn_shaped_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            output = Path(raw_directory)
            archive = builder.build_distribution(ROOT, output)
            self.assertEqual(archive.name, "pg_local_cache-1.1.0.zip")
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
                prefix = "pg_local_cache-1.1.0/"
                for path in (
                    "META.json",
                    "Makefile",
                    "PGXN.md",
                    "README.md",
                    "pg_local_cache.control",
                    "sql/pg_local_cache--1.1.0.sql",
                    "src/pg_local_cache.c",
                ):
                    self.assertIn(prefix + path, names)
                self.assertFalse(
                    any(
                        part in {".agent", ".git", ".tmp", "dist", "secrets"}
                        for name in names
                        for part in Path(name).parts
                    )
                )

    def test_workflow_validates_and_attaches_exact_release_asset(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "pgxn.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'workflows: ["Release PostgreSQL 14-18 Linux artifacts"]',
            source,
        )
        self.assertIn("make pgxn-check dist", source)
        self.assertIn('commit_tag="master-${sha:0:12}"', source)
        self.assertIn('stable_tag="v${version}"', source)
        self.assertIn('[[ "$stable_sha" == "$sha" ]]', source)
        self.assertIn("already exists with different bytes", source)
        self.assertNotIn("PGXN_PASSWORD", source)
        self.assertNotIn("PGXN_USERNAME", source)


if __name__ == "__main__":
    unittest.main()
