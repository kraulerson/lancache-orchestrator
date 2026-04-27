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

### Added
- **FastAPI app skeleton** (BL5 / Feature 5) — `create_app()` factory at
  `src/orchestrator/api/main.py`. Lifespan runs migrations + initializes
  the BL4 pool singleton on startup; closes the pool with the BL4 30 s
  hard timeout on shutdown. Run with
  `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`.
  See [ADR-0012](docs/ADR%20documentation/0012-fastapi-skeleton-architecture.md).
- **`GET /api/v1/health`** endpoint per Bible §8.4. Returns the 7-field
  response (status / version / uptime_sec / scheduler_running /
  lancache_reachable / cache_volume_mounted / validator_healthy /
  git_sha) with HTTP 200 if all subsystems healthy, 503 otherwise.
  **Note:** BL5 ship state intentionally returns 503 because three
  subsystems (`scheduler_running`, `lancache_reachable`,
  `validator_healthy`) are stub-false until BL6+ flips them as features
  land. Container HEALTHCHECK and k8s liveness probes should expect 503
  during this transition.
- **OpenAPI schema** at `/api/v1/openapi.json`, **Swagger UI** at
  `/api/v1/docs`, **ReDoc** at `/api/v1/redoc`. `bearerAuth`
  security_scheme registered so Swagger UI's Authorize button works.
- **`asgi-lifespan==2.1.0`** added to `requirements-dev.txt` for
  test-time lifespan integration.

### Security
- **TM-013 fingerprinting defense:** bearer-auth implemented as pure
  ASGI middleware (not FastAPI Depends), so 404s on non-exempt paths
  also require auth. Returns 401 with timing-safe `hmac.compare_digest`
  comparison (UTF-8-encoded bytes, length-tolerant).
- **OQ2 loopback enforcement:** path pattern
  `^/api/v1/platforms/[^/]+/auth$` additionally requires
  `request.client.host == "127.0.0.1"`. The route is reserved in BL5
  (the actual handler lands in F1/F2); the middleware logic is in
  place so BL6+ inherits enforcement automatically.
- **TM-012 log redaction:** rejected bearer tokens logged with
  `rejection_fingerprint` (8 hex of SHA-256, non-reversible). Field
  name avoids the "token"/"auth"/"bearer"/"secret" keywords because
  ID3's `_redact_sensitive_values` would auto-redact them. Verified by
  `test_no_raw_token_in_logs` and `test_auth_rejected_event_emits_with_sha256_prefix`.
- **TM-018 memory bomb defense:** ASGI middleware enforces 32 KiB
  request-body cap (Bible §9.2). Two paths: Content-Length proactive
  check (immediate 413 before any read); streaming check via
  `receive()` interception (interrupts mid-stream when accumulated
  bytes exceed cap). Streaming variant verified by direct middleware
  unit test against a fake downstream app.
- **CORS hardened:** `allow_credentials=False`. Bearer-token auth flows
  in `Authorization` header, not cookies — closes the
  `allow_origins=*` + `allow_credentials=true` footgun by constraint.
  `allow_headers` whitelist: `Authorization`, `Content-Type`,
  `X-Correlation-ID`. `expose_headers`: `X-Correlation-ID` (for
  Game_shelf to log + correlate API calls).
- **Correlation-ID propagation:** outermost middleware enters ID3's
  `request_context()` per-request. Echoed in response header. Every
  log line during request processing carries the correlation_id via
  structlog contextvar — downstream debugging can grep one CID for
  the full request trace.

- **Async DB pool** (`src/orchestrator/db/pool.py`, BL4 / Feature 4) —
  hybrid 1-writer-N-reader topology on top of `aiosqlite`. Defense-in-depth
  write serialization (`asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout`).
  Comprehensive API: `read_one`/`read_all`/`read_one_as`/`read_all_as`/
  `read_stream`/`execute_write`/`execute_many_write` single-statement
  helpers, `read_transaction`/`write_transaction` multi-statement contexts,
  `acquire_reader`/`acquire_writer` raw-connection escape hatches. Module-
  level singleton (`init_pool`/`get_pool`/`reload_pool`/`close_pool`).
  See [ADR-0011](docs/ADR%20documentation/0011-db-pool-architecture.md).
- **`migrate.verify_schema_current()`** — async helper that asserts the
  applied migration set matches the packaged manifest. Called by
  `Pool.create()` unless `skip_schema_verify=True` (which logs
  `pool.schema_verification_skipped` at WARNING).
- **`pool.schema_status()`** — read-only introspection surface for
  `/api/v1/health` consumers; returns `{applied, available, pending,
  unknown, current}`.
- **`pool.health_check()`** — concurrent per-connection probe with 1 s
  per-probe timeout. Reports writer + reader health, replacement counts,
  uptime.
- **5 new typed Settings fields** (`pool_readers`, `pool_busy_timeout_ms`,
  `db_cache_size_kib`, `db_mmap_size_bytes`, `db_journal_size_limit_bytes`)
  driving pool sizing and SQLite PRAGMA tunables. See ADR-0010 addendum.
- **`config.pool_readers_over_provisioned` diagnostic warning** — fires
  when `pool_readers > chunk_concurrency` (readers will idle).

### Security
- **No raw SQL or parameter values reach log output** (TM-012). Every
  `pool.*` log emission uses `_template_only(sql)` (literals replaced
  with `?`) and `_shape(params)` (parameter type names only, never values).
  Hypothesis property tests in `tests/db/test_pool_property.py` exercise
  the scrubbers across arbitrary value shapes; capsys-based regression
  tests verify end-to-end log scrubbing through the structlog JSONRenderer.
- **Reader connections are read-only at the SQLite layer.** `PRAGMA
  query_only=ON` applied after open; writes through a reader handle fail
  with `OperationalError("readonly database")`. Defense-in-depth alongside
  the application-level reader/writer split.
- **PRAGMA verification at boot.** Each of 9 PRAGMAs (busy_timeout,
  foreign_keys, synchronous, temp_store, cache_size, mmap_size,
  journal_size_limit, plus reader-only query_only) is set then read back;
  mismatch raises `PoolInitError(role=...)` and aborts pool startup.
  Defends against silent SQLite ABI changes that could drop a PRAGMA.
- **Connection-replacement storm guard.** Per-role 60-second sliding
  window; >3 replacements trips the guard and refuses further auto-recovery
  (pool transitions to degraded; operator must `reload_pool()`).
  Prevents disk-failure storms from amplifying into infinite-reconnect
  CPU/IO loops.
- **Background-task error logging** (SEV-3 finding from Phase 2.4 audit,
  fixed inline). Replacement and safe-close tasks now register a done
  callback that logs `pool.background_task_failed` at ERROR with task
  name, error message, and error type. Without this, replacement
  failures would have been silently swallowed by asyncio defaults.
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
- `src/orchestrator/core/settings.py` (ID4) — typed application configuration
  via pydantic-settings `BaseSettings`. 16 fields covering API (`api_host`,
  `api_port`, `cors_origins`, `log_level`, `orchestrator_token`), database
  (`database_path`, `require_local_fs`), platform sessions
  (`steam_session_path`, `epic_session_path`), Lancache cache topology
  (`lancache_nginx_cache_path`, `cache_slice_size_bytes`, `cache_levels`,
  `chunk_concurrency`), and miscellaneous (`manifest_size_cap_bytes`,
  `epic_refresh_buffer_sec`, `steam_upstream_silent_days`). Defaults
  sourced from Bible §7.2/§7.3/§9, Spike F, and the Lancache deployment
  params memory.
- `orchestrator.core.settings.get_settings()` — `@lru_cache` singleton
  accessor. `reload_settings()` provided as a test / SIGHUP escape hatch.
- Four diagnostic `@model_validator(mode="after")` warnings:
  `config.secret_shadowed_by_env` (env and `/run/secrets` both set),
  `config.api_bound_non_loopback` (`api_host` isn't loopback),
  `config.cors_wildcard` (`"*"` in `cors_origins`),
  `config.chunk_concurrency_unvalidated` (`chunk_concurrency > 32`, the
  Spike F gate ceiling).
- `tests/core/conftest.py` — shared autouse `_isolated_env` fixture that
  scrubs `ORCH_*` env vars, chdirs to `tmp_path` (blocks host `.env`
  discovery), resets structlog defaults + contextvars (matching the ID3
  test pattern), and clears the `get_settings()` cache before and after
  every test in `tests/core/`.
- `tests/core/test_settings.py` (67 tests) — full coverage of required
  fields, the 15 optional defaults, field validators, source precedence,
  secret-loading paths, 5-shape × 3-serialization redaction parametrize,
  4 warnings + 1 negative case, singleton behavior, and 2 SEV-2
  regression tests (pickle-block, ValidationError scrubbing).
- `docs/security-audits/id4-settings-security-audit.md` records the
  audit trail.
- ADR-0010 documents the flat-layout / source-order / singleton /
  redaction-layer / validation-scope decisions.

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
- Settings module redaction primitives: `SecretStr` is supplemented by
  a `__reduce__` override that blocks pickling (pydantic's default
  pickler serialises `_secret_value` cleartext, which any future DX
  sugar like multiprocessing task args or Celery would write to an
  attacker-readable queue). `Settings.__init__` intercepts pydantic's
  `ValidationError` for token-field failures and re-raises as
  `ValueError` with a scrubbed message — pydantic core otherwise
  echoes the raw rejected token in `input_value`, which a rotation-
  failure startup would land in the systemd journal. **Caught and
  fixed before ship** by the BL3 re-audit pass. (Audit A1 + A2)

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
  `docs/security-audits/id1-sqlite-migrations-security-audit.md`,
  `docs/security-audits/id3-structured-logging-security-audit.md`, and
  `docs/security-audits/id4-settings-security-audit.md`.
- FEATURES.md now documents Feature 1 (ID1 migrations), Feature 2
  (ID3 structured logging), and Feature 3 (ID4 settings module) with
  links, known limitations, and test-coverage summaries.
- New ADR: [`ADR-0010 — Settings Module Design`](docs/ADR%20documentation/0010-settings-module-design.md).
- Design spec at `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
  and implementation plan at `docs/superpowers/plans/2026-04-23-id4-settings-module.md`
  record the 14-decision brainstorm and 11-task execution trail for BL3.
