# ADR-0008: Migration Runner Architecture — Atomicity, Pinned Checksums, Package-Resource Loading

**Status:** Accepted
**Date:** 2026-04-22
**Phase:** 2 (Construction), Milestone B, Build Loop 1 (ID1)
**Supersedes:** Earlier ID1 implementation (commit `46f2fd2`, rolled back 2026-04-22)
**Related:** ADR-0001 (Orchestrator Architecture), ADR-0004 (Raw SQL — deferred)
**Feature:** ID1-sqlite-migrations

<!-- Last Updated: 2026-04-22 -->

## Context

ID1 (the SQLite migrations framework) was initially shipped on 2026-04-20 with
a minimal `run_migrations()` that ran the `.sql` file as `conn.executescript()`
and recorded a row in `schema_migrations`. UAT Session 1 (2026-04-22) found
8 bugs: 2 SEV-1, 6 SEV-2, plus a SEV-3 hardening gap (GH issues #3, #4, #5,
#6, #7, #8, #12, #13). The BL1 rewrite had to close all of them test-first.

This ADR records the three architectural decisions that shaped the rewrite, so
future migrations and a future maintainer can understand the "why."

## Decisions

### D1 — Single-transaction atomicity via `BEGIN IMMEDIATE`

**Context:** `sqlite3.Connection.executescript()` issues an implicit COMMIT
before running the script, so each DDL statement auto-commits. A mid-script
failure left partial schema + no `schema_migrations` row — the container then
refused to start forever because `CREATE TABLE platforms` failed on re-apply.

**Decision:** Open the connection with `isolation_level=None` (autocommit),
set pragmas that must run outside a transaction (`journal_mode = WAL`,
`foreign_keys`, `synchronous`, `temp_store`, `mmap_size`, `cache_size`,
`busy_timeout`), create `schema_migrations` (idempotent), then wrap the
entire read-applied-and-apply-pending pass in a single explicit
`BEGIN IMMEDIATE; ... COMMIT;`. Split the migration SQL on `;` ourselves
(after stripping `--` and `/* */` comments) and `conn.execute()` each
statement inside the transaction.

**Consequence:** Any failure during the apply pass triggers `ROLLBACK` (via
`contextlib.suppress(sqlite3.Error)` in the except block, in case SQLite has
already auto-rolled-back). The post-apply sanity check runs **inside the
transaction before COMMIT** (see ADR-0008 D3 below) so it participates in the
rollback. Concurrent runners serialize cleanly: the second runner's
`BEGIN IMMEDIATE` blocks on the reserved lock up to `busy_timeout` (5 s),
then re-reads `applied_map`, sees the first runner's work, and short-circuits
to a clean no-op commit.

**Trade-off:** All pending migrations apply-or-rollback as one unit. An error
in migration 3 rolls back 1 and 2 as well (in a multi-version upgrade). This
is acceptable because (a) most boots apply 0 or 1 migrations, and (b) a
partial upgrade is worse than a full rollback + retry.

**Trade-off 2:** Our SQL splitter does not honor semicolons or comment
delimiters inside string literals. This is tracked as a latent SEV-3
issue (#19) with a lint-test interim fix.

### D2 — Pinned checksums via `CHECKSUMS` manifest

**Context:** The initial runner only detected drift on migrations already
recorded in `schema_migrations`. Any migration file that hadn't yet been
applied on a given environment could be tampered with (a modified commit,
dependency-confusion of the source tree) — the runner would apply the
tampered file and record the tampered SHA as canonical. Dev/prod schema
drift through the normal lifecycle of a migration moving between
environments.

**Decision:** Ship a `CHECKSUMS` file alongside the migrations (inside the
`orchestrator.db.migrations` Python package). Format: three whitespace-
delimited columns per line: `<4-digit-id>  <sha256>  <filename>`, with `#`
comments supported. On every boot:

1. Load and parse the manifest.
2. For every migration file: compute SHA-256, cross-check against manifest
   (extras and missing entries are both hard errors).
3. Only then enter the transaction.

**Consequence:** An attacker who can modify the migration files can no longer
replace them unless they can also modify the pinned SHA in `CHECKSUMS` in
the same commit — which is visible in code review as a two-file change.
The manifest is regenerated intentionally when authoring a new migration.

**Trade-off:** Every new migration requires regenerating `CHECKSUMS`. This
is a small operator-experience cost in exchange for a meaningful supply-
chain defense.

**Future hardening:** Issue #21 tracks validating the filename field against
`_MIGRATION_NAME_RE` at parse time (defense-in-depth, no current exploit).
Issue #20 tracks replacing regex-derived post-apply expectations with an
explicit `expected_tables` manifest per migration.

### D3 — Package-resource migration loader (`importlib.resources`)

**Context:** Migrations were loaded from a filesystem path relative to
`__file__`, which resolved to a writable `migrations/` directory at repo
root (or `/app/migrations/` in the Docker image). An attacker with write
access to `/app/` could drop `0002_pwn.sql` into the directory and get
arbitrary DDL/DML execution on next container restart.

**Decision:** Move migrations into the `orchestrator.db.migrations`
subpackage (`src/orchestrator/db/migrations/`). The runner loads them via
`importlib.resources.files("orchestrator.db.migrations")`. `pyproject.toml`
ships `*.sql` and `CHECKSUMS` as package-data via
`[tool.setuptools.package-data]`. The Dockerfile no longer `COPY`s the old
`migrations/` directory separately — the `.sql` files are part of the
installed wheel.

**Consequence:** Migrations ship as read-only resources inside the wheel /
image layer, not a filesystem directory that can be mutated in place.
Dropping a file into `/app/src/orchestrator/db/migrations/` at runtime
would require write access to the Python site-packages tree — a much
stronger pre-condition than write access to a plain directory.

**Trade-off:** Tests that exercise the runner against arbitrary migration
sets need a `migrations_dir=Path(...)` parameter to override the package
source. The runner exposes this as a keyword-only argument on
`run_migrations()` and it is not intended for production use (defaults to
the packaged source).

## Additional notes (from re-audit hardening, commit `8ca6658`)

- **Network filesystem refusal** (`_assert_local_filesystem`): denylist
  covers classical (NFS, CIFS, SMB, AFS, coda, WebDAV), clustered
  (GlusterFS, Ceph, Lustre, BeeGFS, GPFS, OCFS2, GFS2, MooseFS), and
  FUSE-backed network / object-store mounts (fuse.sshfs, fuse.cifs,
  fuse.smb, fuse.glusterfs, fuse.s3fs, fuse.gcsfuse, fuse.goofys).
  Detection via `/proc/self/mountinfo` on Linux, `/usr/bin/stat -f %T`
  on macOS. On `"unknown"` (detection failure): default is warn+proceed,
  `ORCH_REQUIRE_LOCAL_FS=strict` fails closed.
- **No rollback runner.** Intentional: down-migrations are a whole feature
  (versioning, data-preservation policy, partial-rollback recovery) and
  out of MVP scope. `0001_initial_down.sql` was removed along with all
  doc references to `orchestrator-cli db rollback`. Will be revisited via
  a future ADR when a real downgrade scenario exists.

## Consequences

- ID1 closes 8 audit findings + 3 additional hardening items test-first.
- Future migrations must: (a) match `^\d{4}_[a-z0-9_]+\.sql$`, (b) have a
  corresponding `CHECKSUMS` entry with matching SHA-256, (c) avoid
  semicolons or comment delimiters inside string literals until #19 lands,
  (d) prefer plain `CREATE TABLE` over `CREATE TEMP` / `CREATE VIRTUAL`
  until #20 lands.
- The operator experience includes an `ORCH_REQUIRE_LOCAL_FS` env var
  documented under deployment.

## Related work

- Commit `3f4ea85` — initial rewrite closing issues #3, #4, #5, #6, #7, #8, #12, #13
- Commit `8ca6658` — hardening pass closing re-audit F1, F2, F6
- Audit artifact: `docs/security-audits/id1-sqlite-migrations-security-audit.md`
- Follow-up issues: [#19](https://github.com/kraulerson/lancache-orchestrator/issues/19), [#20](https://github.com/kraulerson/lancache-orchestrator/issues/20), [#21](https://github.com/kraulerson/lancache-orchestrator/issues/21)
