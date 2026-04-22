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
- Correlation-ID leak fix: `request_context()` now uses structlog's
  token-based reset, so nested context managers restore the outer block's
  CID rather than wiping all contextvars. Eliminates cross-request bleed
  via pooled workers that was the core risk behind issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9).
- User kwargs that collide with framework-owned reserved keys
  (`correlation_id`, `level`, `timestamp`, `event`, `logger`, `logger_name`)
  are now rescued to `user_<key>` (with numbered-slot collision handling)
  rather than silently overriding. Protects audit-trail integrity against
  attacker-controlled input reaching `log.info(**user_dict)`. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Recursive secret-value redaction: any log-event key matching the
  sensitive-key regex (password, passwd, passphrase, token, jwt, secret,
  authorization, bearer, cookie, session, api_key, apikey, credential,
  private_key, privkey, signature, plus letter-bounded pwd/pin/otp/mfa/
  tfa/sid/creds/salt/nonce) has its value replaced with `<redacted>`
  before the JSONRenderer sees it. Walks nested dicts and lists.
  Cycle-safe — a self-referential structure is substituted with
  `<cyclic>` rather than blowing the stack. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14),
  re-audit N3+N4)
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
- `orchestrator.core.logging.request_context()` context manager for
  scoped correlation-ID binding. Supersedes the raw `bind_correlation_id()`
  + `clear_request_context()` pair, which remain as low-level primitives.
- Public `RESERVED_KEYS` constant exported from `orchestrator.core.logging`.
- `tests/core/test_logging.py` (55 tests) covering every UAT-1 + re-audit
  logging finding.
- `docs/security-audits/id3-structured-logging-security-audit.md` records
  the logging audit trail.
- ADR-0009 documents the scoped-context / reserved-key / redaction /
  log-level-validation decisions.

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
- `configure_logging(log_level=...)` now validates input against
  `{DEBUG, INFO, WARNING, ERROR, CRITICAL}` (case-insensitive, stripped).
  Raises `ValueError` on anything else instead of silently falling back
  to INFO — operator typos in `LOG_LEVEL` surface at startup rather than
  at incident time. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))

### Fixed
- Migration atomicity — see Security/Changed entries above. (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Silent-skip of gap / out-of-order migrations. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Drift detection on unapplied migrations. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- `schema_migrations` tamper bypass. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6))
- Concurrent-runner race. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- WAL journal-mode unconditionally set without FS probe. (Issue [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12))
- Correlation-ID context bleed across pooled workers. (Issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9))
- Reserved-key clobber from user kwargs. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Missing PII/secret redaction in log values. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14))
- `log_level` silent fallback to INFO on typo. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))
- Short-token redaction regex silently failed on `user_pwd` / `my_pin` /
  `otp_code` / `creds_list` etc. shapes because Python `\b` uses `\w`
  boundaries and `_` is `\w`. Replaced with letter-class boundaries.
  **Caught and fixed before ship** by the BL2 re-audit pass. (Re-audit N3)

### Removed
- `migrations/0001_initial_down.sql` and all doc references to
  `orchestrator-cli db rollback`. Rollback is intentionally out of MVP
  scope; re-introducing it will require a dedicated ADR covering
  versioning and data-preservation policy. (Issue [#7](https://github.com/kraulerson/lancache-orchestrator/issues/7))
- Top-level `migrations/` directory (contents moved into the package).

### Infrastructure
- `pyproject.toml`: added `[tool.setuptools.package-data]` to ship
  `*.sql` + `CHECKSUMS` inside the `orchestrator.db.migrations` package.
  Per-file ruff `S101/S105/S106` ignore for `tests/core/test_logging.py`
  (redaction tests necessarily include fake credential literals as inputs).
- `Dockerfile`: removed the `COPY migrations/ /app/migrations/` step
  (migrations now ride along inside the installed wheel).
- `.semgrep/orchestrator-rules.yaml`: `no-sync-sqlite` rule now excludes
  `tests/db/test_migrate.py`. `no-credential-log` rule now excludes
  `tests/core/test_logging.py` — redaction tests verify the processor by
  logging literal credential-named kwargs and asserting the value becomes
  `<redacted>`.

### Documentation
- New ADR: [`ADR-0008 — Migration Runner Architecture`](docs/ADR%20documentation/0008-migration-runner-architecture.md).
- New ADR: [`ADR-0009 — Logging Framework Architecture`](docs/ADR%20documentation/0009-logging-framework-architecture.md).
- New audit artifacts:
  `docs/security-audits/id1-sqlite-migrations-security-audit.md` and
  `docs/security-audits/id3-structured-logging-security-audit.md`.
- FEATURES.md now documents Feature 1 (ID1 migrations) and Feature 2
  (ID3 structured logging) with links, known limitations, and test-coverage
  summaries.
