# Security Audit — ID1 SQLite Migrations Framework

**Feature:** ID1-sqlite-migrations (Build Loop 1, Milestone B)
**Module:** `src/orchestrator/db/migrate.py` + `src/orchestrator/db/migrations/`
**Audit date:** 2026-04-22
**Auditor persona:** Senior Security Engineer (independent read-only sub-agent)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-04-22 -->

## Scope

Post-implementation security review of the rewritten ID1 migrations framework,
covering:

- `src/orchestrator/db/migrate.py` (runner, 440 lines)
- `src/orchestrator/db/migrations/__init__.py`
- `src/orchestrator/db/migrations/0001_initial.sql` (moved from repo root)
- `src/orchestrator/db/migrations/CHECKSUMS` (new manifest)
- `pyproject.toml` (package-data for `.sql` + `CHECKSUMS`)
- `Dockerfile` (removed `COPY migrations/`)
- `tests/db/test_migrate.py` (42 regression + baseline tests)

## Methodology

Two passes:

1. **Verification pass.** For each of the 8 bugs from the UAT-1 audit
   (2026-04-22, GH issues #3–#8 + #12, #13), read the new code and confirm the
   claimed fix actually holds. Don't trust the commit message.
2. **Hunt pass.** Attack the new design with fresh eyes — splitter edge cases,
   transaction lifecycle, fstype detection corners, post-apply semantics,
   package-resource allowlist, CHECKSUMS parser robustness.

Read-only static review. Limited targeted pytest runs. No live exploits.

## Verification of UAT-1 findings

| GH Issue | Severity | Status | Evidence |
|---|---|---|---|
| [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3) — atomicity | SEV-1 | **CLOSED** | `BEGIN IMMEDIATE` at `migrate.py:374` wraps the full read+apply+COMMIT; `ROLLBACK` via `suppress(sqlite3.Error)` at `migrate.py:415-417`. Exercised by `test_atomic_failure_leaves_no_partial_state`. |
| [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4) — gap migrations | SEV-1 | **CLOSED** | `_assert_no_gaps` (`migrate.py:249-265`) computes `applied \| available` and requires a contiguous range from 1; orphan applied-but-missing rejected. Three regression tests. |
| [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5) — drift on unapplied | SEV-2 | **CLOSED** | `_verify_checksum_manifest` (`migrate.py:212-241`) runs pre-apply, cross-checks every file against the pinned SHA-256 in `CHECKSUMS`; extras and missing entries are hard errors. |
| [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6) — schema_migrations tamper | SEV-2 | **CLOSED** | `_verify_expected_objects` fires both pre-apply (when applied set is non-empty) and post-apply. Post-apply now runs *inside* the transaction before COMMIT (see F6 below). |
| [#7](https://github.com/kraulerson/lancache-orchestrator/issues/7) — dead rollback code | N/A | **CLOSED** | `0001_initial_down.sql` deleted; no `rollback_to` / `rollback` symbols; `_discover_migrations("down")` removed. |
| [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8) — concurrent-runner race | SEV-2 | **CLOSED** | Single `BEGIN IMMEDIATE` wraps read+apply; `PRAGMA busy_timeout = 5000` (`migrate.py:366`) blocks the losing thread, which then re-reads `applied_map` and no-ops. Verified by `test_concurrent_runners_serialize`. |
| [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12) — WAL on network FS | SEV-2 | **CLOSED** (with caveats, see F1/F2) | `_assert_local_filesystem` called at `migrate.py:345` before any PRAGMA. Detects via `/proc/self/mountinfo` on Linux and `/usr/bin/stat -f %T` on macOS. |
| [#13](https://github.com/kraulerson/lancache-orchestrator/issues/13) — MIGRATIONS_DIR via `__file__` | SEV-3 | **CLOSED** | `importlib.resources.files("orchestrator.db.migrations")` at `migrate.py:139`. `pyproject.toml` ships `*.sql` + `CHECKSUMS` as package-data. |

## New findings from the hunt pass

| # | Severity | Title | Location | Status |
|---|---|---|---|---|
| F1 | SEV-3 | Fail-open on unknown fstype | `migrate.py:81-116` | **FIXED** (commit `8ca6658`) — default is warn+proceed, `ORCH_REQUIRE_LOCAL_FS=strict` upgrades to fail-closed. |
| F2 | SEV-3 | `_NETWORK_FSTYPES` not exhaustive | `migrate.py:32-53` | **FIXED** (commit `8ca6658`) — added GlusterFS, Ceph, Lustre, BeeGFS, GPFS, OCFS2, GFS2, MooseFS, fuse.glusterfs/s3fs/gcsfuse/goofys. |
| F3 | SEV-3 | Statement splitter mis-handles `;` and comments inside string literals | `migrate.py:273-278` | **TRACKED** — [#19](https://github.com/kraulerson/lancache-orchestrator/issues/19). Latent (no current migration triggers). |
| F4 | SEV-3 | `_CREATE_TABLE_RE` misses `CREATE TEMP` / `VIRTUAL` / `DROP TABLE` | `migrate.py:55-58, 286-295` | **TRACKED** — [#20](https://github.com/kraulerson/lancache-orchestrator/issues/20). |
| F5 | SEV-3 | CHECKSUMS parser accepts path separators in filename field | `migrate.py:196-208` | **TRACKED** — [#21](https://github.com/kraulerson/lancache-orchestrator/issues/21). Not currently exploitable (downstream uses basename compare). |
| F6 | SEV-4 | Post-apply sanity ran after COMMIT | `migrate.py:413, 419` | **FIXED** (commit `8ca6658`) — verify now runs inside try block before COMMIT, so failure triggers ROLLBACK. |

## Non-findings (explicitly checked, clean)

- **Package-resource allowlist regex** (`_MIGRATION_NAME_RE`) is fully anchored; `.bak`, uppercase, hidden dotfiles all rejected.
- **SHA case normalization** — manifest + file both hexlower-compared; mixed-case pinning works.
- **CHECKSUMS whitespace tolerance** — `str.split()` collapses any whitespace runs.
- **Autocommit + PRAGMA ordering** — `isolation_level=None` at connect; WAL PRAGMA set outside any transaction; `BEGIN IMMEDIATE` then starts a real transaction.
- **ROLLBACK on broken connection** — `suppress(sqlite3.Error)` + `finally: conn.close()` releases the busy lock cleanly.
- **macOS `/usr/bin/stat` subprocess** — absolute path, fixed argv, 2s timeout, no shell; no injection surface even though `target` is user-derivable.
- **Linux longest-mount-prefix** — bind mounts over network mounts correctly resolve to the bind mount's fstype (which is what the actual I/O goes through).
- **Correlation-ID predictability** — out of scope for ID1 (covered under ID3 audit).

## Decision

**ID1 is cleared to advance through the Build Loop.** All SEV-1 and SEV-2 findings are closed and exercised by regression tests. The three SEV-3 findings that remain (F3/F4/F5) are latent future-migration footguns, not live exploits — tracked as follow-up issues for resolution before the Phase 2→3 gate.

## Follow-up tracking

- [#19](https://github.com/kraulerson/lancache-orchestrator/issues/19) — statement splitter literal-semicolon robustness (F3)
- [#20](https://github.com/kraulerson/lancache-orchestrator/issues/20) — explicit expected-tables manifest (F4)
- [#21](https://github.com/kraulerson/lancache-orchestrator/issues/21) — CHECKSUMS filename validation (F5)

## Sign-off

- Implementation: commit `3f4ea85` (initial rewrite) + `8ca6658` (F1/F2/F6 hardening)
- Test suite: `tests/db/test_migrate.py` (42 tests; 57 project-wide)
- Lint/type: ruff clean, mypy --strict clean
- Gates: pre-commit hooks (gitleaks, semgrep, ruff, mypy) all green
