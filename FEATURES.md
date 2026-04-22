# Feature Reference

<!--
  This document is a living index of all features built during Phase 2.
  Update at Step 2.5 of every Build Loop iteration alongside the CHANGELOG and Bible.
  Purpose: Give someone a quick orientation to what the app does without reading the Bible.
  For detailed analysis, follow the links to ADRs and interface docs.
-->

## Feature 1: ID1 — SQLite Migrations Framework

**Phase Built:** 2 (Milestone B, Build Loop 1)
**Status:** Complete (2026-04-22)
**Summary:** A minimal-dependency migrations runner that applies numbered `.sql`
files to the SQLite database on container startup, with atomic apply (single
`BEGIN IMMEDIATE` / `COMMIT`), SHA-256 pinning via a `CHECKSUMS` manifest,
gap detection, post-apply schema-object sanity check, concurrent-runner
serialization, and refusal to run on network filesystems (WAL incompatible).
Migrations ship as Python package data (read-only) loaded via `importlib.resources`.
**Key Interfaces:**
  - `src/orchestrator/db/migrate.py` — runner (`run_migrations`, `MigrationError`)
  - `src/orchestrator/db/migrations/` — packaged `*.sql` files + `CHECKSUMS`
  - Env var: `ORCH_REQUIRE_LOCAL_FS=strict` (opt-in fail-closed on unknown FS)
**Related ADRs:**
  - [`ADR-0008 — Migration Runner Architecture`](ADR%20documentation/0008-migration-runner-architecture.md)
**Test Coverage:** Unit — 42 tests in `tests/db/test_migrate.py`. Regression coverage for all 8 UAT-1 findings (GH #3–#8, #12, #13) plus the 3 re-audit hardening items. Integration/E2E tests will cover this indirectly once the API layer boots migrations on startup.
**Known Limitations:**
  - Statement splitter does not honor `;` or comment delimiters inside string literals ([#19](https://github.com/kraulerson/lancache-orchestrator/issues/19)).
  - `_CREATE_TABLE_RE` misses `CREATE TEMP` / `CREATE VIRTUAL` / does not subtract on `DROP TABLE` ([#20](https://github.com/kraulerson/lancache-orchestrator/issues/20)).
  - No rollback runner (intentional for MVP); `down`-direction migrations out of scope.

---

<!-- Copy the section above for each new feature. Number sequentially. -->
