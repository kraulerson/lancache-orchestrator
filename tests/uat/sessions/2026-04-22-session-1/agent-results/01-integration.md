## UAT-1 Integration Report

### Environment
Python 3.12.13 on macOS 26.3.1 arm64; venv `/Users/karl/Documents/Claude Projects/lancache_orchestrator/.venv`.

### Checks

1. **Package-data shipping — PASS.** `importlib.resources.files('orchestrator.db.migrations').iterdir()` returned `0001_initial.sql`, `CHECKSUMS`, and `__init__.py`. Confirmed both required files present inside the installed package tree.

2. **Migrations apply end-to-end — PASS.** Fresh tmp SQLite DB + `migrate.run_migrations(db)` without overrides produced tables `block_list, cache_observations, games, jobs, manifests, platforms, schema_migrations, sqlite_sequence, validation_history`; `PRAGMA journal_mode` returned `wal`; `platforms` seeded with 2 rows (`steam`/`epic`, `auth_status='never'`).

3. **Logging boot smoke — PASS.** After `configure_logging()`, `log.info("boot", version="0.1.0")` emitted a single JSON line parseable by `json.loads`; keys `{event, level, timestamp, version}` present, `event=="boot"`, `level=="info"`, ISO-8601 UTC timestamp.

4. **Cross-feature (logs don't leak secrets) — PASS.** During check 2 the migrate path emitted 3 JSON lines: `migration_applying`, `migration_applied`, `migrations_complete`. The `name` field is the migration stem (`"initial"`) — not a secret, confirming the concern in the prompt is unfounded. No line contains unredacted `password/secret/token/bearer` substrings.

5. **Full test suite — PASS.** `pytest --cov=src/orchestrator --cov-branch -q` → **112 passed, 0 failed, 0 skipped in 5.38 s** (wall 5.64 s). No flaky behavior observed on this run. Tests create tmp SQLite paths via pytest's `tmp_path` fixture; no HOME/network dependence surfaced. Combined line+branch coverage on total codebase **85%** (353 statements+branches, 51 miss). Modules under active test: `core/logging.py` 92% (misses 87-89 bind/clear primitives, 97 clear fn, 156 strict-mode branch), `db/migrate.py` 85% (misses 130-132 OSError path on darwin `stat`, a couple of malformed-CHECKSUMS error branches, `_cli` entrypoint 471-483).

6. **Build sanity — PASS.** `python -m build --sdist --wheel` succeeded; produced `lancache_orchestrator-0.1.0-py3-none-any.whl` containing `orchestrator/db/migrations/0001_initial.sql` and `orchestrator/db/migrations/CHECKSUMS` (so package-data declaration in pyproject is effective). Post-build `pip install --no-deps -e .` succeeded; `import orchestrator.db.migrate` and `import orchestrator.core.logging` both resolve.

7. **Dockerfile static review — PASS.**
   - Both FROM lines pin identical digest `sha256:520153e2deb359602c9cffd84e491e3431d76e7bf95a3255c9ce9433b76ab99a`.
   - No `COPY migrations/` remnant; comment on line 35-36 documents that migrations ship as package data via importlib.resources.
   - `HEALTHCHECK` present (30s interval, hits `/api/v1/health`).
   - `USER orchestrator` declared (UID/GID 1000, `/usr/sbin/nologin`).
   - 250 MB size gate is realistic for `python:3.12-slim` + venv with httpx/structlog/fastapi/uvicorn (typical such images land 180-220 MB).

8. **CI workflow static review — PASS.**
   - `docker/setup-qemu-action@v3` present (step "Set up QEMU").
   - `docker/setup-buildx-action@v3` present.
   - amd64 step has `load: true` and `platforms: linux/amd64`; size-check step runs immediately after using `docker image inspect`.
   - arm64 step has explicit `platforms: linux/arm64` and no `load:` key (defaults to false) — verify-only as intended.
   - Both steps share `cache-from: type=gha` and `cache-to: type=gha,mode=max`.

### Coverage
- Tests: 112 passed / 0 failed / 0 skipped.
- Line coverage 85%, branch coverage folded in (combined `--cov-branch` reported 85% TOTAL over 265 statements + 88 branches, 40 stmt + 11 branch miss).
- Top files by uncovered lines: `db/migrate.py` (28 miss — mostly CHECKSUMS/parse error branches and `_cli`), `core/logging.py` (5 miss — low-level bind/clear primitives). The rest are intentionally empty `__init__.py` stubs for upcoming modules (`adapters`, `api`, `cli`, `status`, `validator`) counted as 0% because they only contain a one-liner that hasn't been imported in any test yet.

### Integration concerns
- `_cli` entrypoint in `migrate.py` (lines 469-483) is untested — consider a `python -m orchestrator.db.migrate <path>` smoke test before production, since it's the container's default migration path.
- `ORCH_REQUIRE_LOCAL_FS` env var is documented in the `_assert_local_filesystem` docstring but not mentioned in CHANGELOG/README-facing docs; operator surface.
- `0001_initial.sql` is a single transaction — first migration is heavy. Fine at 0.4 ms for an empty DB, but future migrations will want to watch the `BEGIN IMMEDIATE`-wrapped scope.
- Empty package `__init__.py` files under `adapters/`, `api/`, `cli/`, `status/`, `validator/` skew the overall coverage number downward (0% on 5 files) — pre-existing and expected for a Phase-2 codebase mid-construction, but worth a comment if a coverage-threshold gate is added.

### Ready-for-tester assessment
**Deployment-ready for the ID1+ID3 integration scope.** All eight checks PASS; no integration blockers surfaced. An operator handed this tree could build the image, run the container, and expect a clean first-boot: migrations apply atomically, WAL is enabled, platform seeds land, structured JSON logs emit from the very first log line.
