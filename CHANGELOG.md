# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/) with extended categories
for handoff clarity. Categories are ordered by impact severity.

<!--
  Category definitions:
  - Security: Vulnerability fixes, dependency patches for CVEs, auth changes
  - Data Model: Schema migrations, data format changes, rollback notes
  - Added: New features, new endpoints, new commands
  - Changed: Modifications to existing behavior
  - Fixed: Bug fixes (reference BUGS.md entry if applicable)
  - Removed: Removed features, deprecated endpoints
  - Infrastructure: CI/CD changes, dependency updates, configuration changes, tooling
  - Documentation: Significant doc updates (new ADRs, updated threat model, revised user guide)
-->

## [Unreleased]

### Security
- Migrations runner now refuses to boot on network filesystems (NFS, CIFS,
  SMB, GlusterFS, Ceph, Lustre, BeeGFS, GPFS, OCFS2, GFS2, MooseFS, plus
  FUSE-backed `sshfs`/`cifs`/`smb`/`glusterfs`/`s3fs`/`gcsfuse`/`goofys`).
  Opt-in `ORCH_REQUIRE_LOCAL_FS=strict` upgrades unknown-fs to hard failure
  for deployments where silent WAL corruption is worse than refusing to
  start. (Issues [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12), re-audit F1+F2)
- Pinned SHA-256 checksums for every packaged migration in a new
  `CHECKSUMS` manifest. Tamper of an unapplied migration is now detected
  before apply. Supply-chain defense: an attacker modifying a migration
  file must also modify the manifest in the same commit. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- Post-apply schema-object sanity check derived from each migration's SQL
  now runs inside the transaction before COMMIT, so a failure triggers
  ROLLBACK. Prevents the boot-loop failure mode where `schema_migrations`
  claims migrations are applied but the expected tables are missing. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6), re-audit F6)

### Data Model
- `0001_initial.sql` relocated to the `orchestrator.db.migrations` Python
  subpackage. Runner now loads migrations via `importlib.resources.files()`
  rather than a `__file__`-relative filesystem path — mitigates the
  "attacker-writable app dir → arbitrary DDL on restart" class of risk.
  (Issue [#13](https://github.com/kraulerson/lancache-orchestrator/issues/13))
- Header comment in `0001_initial.sql` corrected — previous version
  falsely claimed atomicity that the implementation didn't deliver.

### Added
- `MigrationError` typed exception for all migrations-framework failures.
- `tests/db/test_migrate.py` (42 tests) covering every UAT-1 finding and
  every re-audit hardening item.
- `docs/security-audits/id1-sqlite-migrations-security-audit.md` records
  the full pre- and post-fix audit trail.
- ADR-0008 documents the atomicity / checksum / packaging decisions.

### Changed
- `run_migrations()` rewritten: explicit `BEGIN IMMEDIATE` wraps the whole
  read+apply pass; PRAGMAs run outside any transaction; per-statement
  `conn.execute()` inside the transaction (instead of `executescript()`,
  which auto-commits and defeated atomicity). (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Gap migrations are now rejected with a hard error naming the missing ID,
  instead of being silently skipped. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Concurrent runners serialize cleanly via `PRAGMA busy_timeout = 5000`
  combined with the single `BEGIN IMMEDIATE`; the losing runner no-ops
  after re-reading `applied_map`. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))

### Fixed
- Migration atomicity — see Security/Changed entries above. (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Silent-skip of gap / out-of-order migrations. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Drift detection on unapplied migrations. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- `schema_migrations` tamper bypass. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6))
- Concurrent-runner race. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- WAL journal-mode unconditionally set without FS probe. (Issue [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12))

### Removed
- `migrations/0001_initial_down.sql` and all doc references to
  `orchestrator-cli db rollback`. Rollback is intentionally out of MVP
  scope; re-introducing it will require a dedicated ADR covering
  versioning and data-preservation policy. (Issue [#7](https://github.com/kraulerson/lancache-orchestrator/issues/7))
- Top-level `migrations/` directory (contents moved into the package).

### Infrastructure
- `pyproject.toml`: added `[tool.setuptools.package-data]` to ship
  `*.sql` + `CHECKSUMS` inside the `orchestrator.db.migrations` package.
- `Dockerfile`: removed the `COPY migrations/ /app/migrations/` step
  (migrations now ride along inside the installed wheel).
- `.semgrep/orchestrator-rules.yaml`: `no-sync-sqlite` rule now excludes
  `tests/db/test_migrate.py` (tests for the synchronous migrate runner
  necessarily import `sqlite3`).

### Documentation
- New ADR: [`ADR-0008 — Migration Runner Architecture`](docs/ADR%20documentation/0008-migration-runner-architecture.md).
- New audit artifact: `docs/security-audits/id1-sqlite-migrations-security-audit.md`.
- FEATURES.md now documents Feature 1 (ID1) with links, known limitations,
  and test-coverage summary.
