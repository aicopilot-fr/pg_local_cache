#!/usr/bin/env python3
"""Build and verify the source archive uploaded to PGXN."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

from validate_pgxn_meta import MetadataError, validate_repository


def _run(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MetadataError(f"required program is missing: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or f"exit {error.returncode}"
        raise MetadataError(f"{' '.join(command)} failed: {detail}") from error
    return result.stdout.strip()


def _verify_clean_head(root: Path, revision: str, allow_dirty: bool) -> str:
    resolved = _run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=root)
    if revision == "HEAD" and not allow_dirty:
        status = _run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
        )
        if status:
            raise MetadataError(
                "working tree is not clean; commit the release inputs or pass --allow-dirty"
            )
    return resolved


def _verify_archive(
    archive: Path,
    *,
    distribution: str,
    version: str,
    expected_meta: dict[str, object],
) -> None:
    prefix = f"{distribution}-{version}/"
    required = {
        f"{prefix}LICENSE",
        f"{prefix}META.json",
        f"{prefix}Makefile",
        f"{prefix}PGXN.md",
        f"{prefix}README.md",
        f"{prefix}pg_local_cache.control",
        f"{prefix}sql/pg_local_cache--{version}.sql",
        f"{prefix}src/pg_local_cache.c",
        f"{prefix}src/pg_local_cache_sql.c",
        f"{prefix}src/pg_local_cache_worker.c",
    }
    forbidden_parts = {".agent", ".git", ".tmp", "benchmark-results", "dist", "secrets"}

    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if not names:
            raise MetadataError("PGXN archive is empty")
        if len(names) != len(set(names)):
            raise MetadataError("PGXN archive contains duplicate paths")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise MetadataError(f"PGXN archive contains unsafe path {name!r}")
            if not name.startswith(prefix):
                raise MetadataError(
                    f"PGXN archive path {name!r} is outside {prefix!r}"
                )
            if forbidden_parts.intersection(path.parts):
                raise MetadataError(f"PGXN archive contains private path {name!r}")
        missing = sorted(required.difference(names))
        if missing:
            raise MetadataError(
                "PGXN archive is missing required files: " + ", ".join(missing)
            )
        try:
            archived_meta = json.loads(package.read(f"{prefix}META.json"))
        except (KeyError, UnicodeError, json.JSONDecodeError) as error:
            raise MetadataError(f"could not read archived META.json: {error}") from error
        if archived_meta != expected_meta:
            raise MetadataError("archived META.json differs from the validated source")


def build_distribution(
    root: Path,
    output_directory: Path,
    revision: str = "HEAD",
    *,
    allow_dirty: bool = False,
) -> Path:
    root = root.resolve()
    metadata = validate_repository(root)
    distribution = str(metadata["name"])
    version = str(metadata["version"])
    resolved = _verify_clean_head(root, revision, allow_dirty)

    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / f"{distribution}-{version}.zip"

    with tempfile.TemporaryDirectory(prefix="pg_local_cache_pgxn_") as raw:
        temporary = Path(raw) / destination.name
        _run(
            [
                "git",
                "archive",
                "--format=zip",
                f"--prefix={distribution}-{version}/",
                f"--output={temporary}",
                resolved,
            ],
            cwd=root,
        )
        _verify_archive(
            temporary,
            distribution=distribution,
            version=version,
            expected_meta=metadata,
        )
        staged = destination.with_suffix(".zip.tmp")
        shutil.copyfile(temporary, staged)
        staged.replace(destination)

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"{destination}  sha256:{digest}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--allow-dirty", action="store_true")
    arguments = parser.parse_args()
    try:
        build_distribution(
            arguments.root,
            arguments.output_dir,
            arguments.revision,
            allow_dirty=arguments.allow_dirty,
        )
    except (MetadataError, OSError, UnicodeError, zipfile.BadZipFile) as error:
        print(f"PGXN distribution build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
