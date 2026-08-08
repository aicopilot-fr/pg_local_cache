# PGXN packaging and installation

`pg_local_cache` is packaged as a regular PGXS extension. The repository ships
PGXN 1.0 metadata, a repository validator, and a deterministic
`pg_local_cache-<version>.zip` builder.

The package becomes available through `pgxn install pg_local_cache` only after a
maintainer uploads a release archive to PGXN Manager. Until then, use the
GitHub release assets documented in `README.md`.

## Install from PGXN

Requirements:

- PostgreSQL 14–18 server development files for the target installation;
- a C compiler and GNU Make;
- the PGXN client;
- permission to write to the target PostgreSQL library and extension
  directories.

Choose the same `pg_config` used by the PostgreSQL server:

```bash
pgxn install \
  --pg_config /usr/lib/postgresql/16/bin/pg_config \
  --sudo -- \
  pg_local_cache
```

`pgxn install` downloads, compiles, and copies the extension files. It does not
configure the server, restart PostgreSQL, or attach application tables.

`pg_local_cache` must be present in `shared_preload_libraries` before
`CREATE EXTENSION` because its shared-memory layout is allocated at postmaster
startup. Preserve every existing preload entry, configure the extension memory
and database settings, perform one controlled restart, and then run:

```sql
CREATE EXTENSION pg_local_cache;
SELECT local_cache.attach_table('public.items'::regclass);
```

Use the
[existing-server installation guide](https://profundium.github.io/pg_local_cache/docs/INSTALL_EXISTING.html)
for preflight, memory sizing, restart, verification, HA, and rollback. The PGXN
client replaces only the source download/build/copy part of that procedure.

Do not use `pgxn load` before the preload configuration and restart. Loading the
SQL objects without the preloaded module cannot initialize the shared cache.

## Validate and build the distribution

The local validator checks the PGXN 1.0 required fields plus repository-specific
version contracts:

```bash
make pgxn-check
```

Build the upload archive without requiring `pg_config`:

```bash
make dist
```

The output is:

```text
dist/pg_local_cache-<version>.zip
```

The builder:

- packages the exact committed revision with `git archive`;
- places every file under `pg_local_cache-<version>/`;
- verifies that `META.json`, the control file, current SQL file, C sources,
  license, and documentation are present;
- rejects dirty release inputs, unsafe archive paths, duplicate entries, and
  private build directories;
- prints the archive SHA-256 digest.

PGXN Manager performs its own metadata validation on upload. A maintainer with
the PGXN client installed can also run the official validator:

```bash
pgxn validate-meta
```

## Maintainer release checklist

1. Update the extension version in all version-bearing files.
2. Run the full CI matrix.
3. Create an immutable `v<version>` tag at the exact release commit.
4. Confirm that the tag and current commit are identical:

   ```bash
   VERSION="$(python3 scripts/validate_pgxn_meta.py --print-version)"
   test "$(git rev-parse "v${VERSION}^{commit}")" = "$(git rev-parse HEAD)"
   ```

5. Build and inspect the distribution:

   ```bash
   make dist
   unzip -t "dist/pg_local_cache-${VERSION}.zip"
   sha256sum "dist/pg_local_cache-${VERSION}.zip"
   ```

6. Upload that exact ZIP through PGXN Manager.

Never reuse an existing semantic version for a different commit. If
`v<version>` already points elsewhere, bump the extension and distribution
version before uploading to PGXN.

The `PGXN package` GitHub workflow validates pull requests and, after the main
release workflow succeeds, adds the ZIP to the immutable commit release. It adds
the same asset to `v<version>` only when that stable tag points to the exact
release commit. PGXN credentials are intentionally not stored in GitHub Actions;
the final PGXN Manager upload remains an explicit maintainer action.
