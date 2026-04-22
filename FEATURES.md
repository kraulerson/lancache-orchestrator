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

## Feature 2: ID3 — Structured Logging with Correlation IDs

**Phase Built:** 2 (Milestone B, Build Loop 2)
**Status:** Complete (2026-04-22)
**Summary:** JSON-line structured logging via structlog, with correlation-ID
scoping (`request_context()` context manager using token-based reset that
survives nesting and exceptions), reserved-key protection (user kwargs that
collide with framework-owned keys like `correlation_id` are rescued to
`user_<key>` with numbered-slot collision handling), recursive secret
redaction (any value under a key matching the sensitive-key regex becomes
`<redacted>`; cycle-safe), and strict log-level validation (unknown values
raise `ValueError` instead of silently becoming INFO).
**Key Interfaces:**
  - `src/orchestrator/core/logging.py` — `configure_logging()`,
    `request_context()`, `new_correlation_id()`, `bind_correlation_id()`,
    `clear_request_context()`, `RESERVED_KEYS`
**Related ADRs:**
  - [`ADR-0009 — Logging Framework Architecture`](ADR%20documentation/0009-logging-framework-architecture.md)
**Test Coverage:** Unit — 55 tests in `tests/core/test_logging.py`.
Regression coverage for all 4 UAT-1 findings (GH #9, #10, #14, #15) plus
the 4 re-audit hardening items. Parametrized across 12 compound-key shapes
for redaction and 5 valid log-levels for validation. Integration coverage
will follow once the API layer boots the logger at startup.
**Known Limitations:**
  - Value-content scanning is not implemented; relies on callers using
    descriptive field names for the key-based redactor to catch secrets.
  - `_redact_sensitive_values` rebuilds dicts even when nothing redactable;
    minor perf ([#22](https://github.com/kraulerson/lancache-orchestrator/issues/22)).
  - Low-level `bind_correlation_id()` / `clear_request_context()` primitives
    are retained for unusual cases but callers should prefer
    `request_context()`.

---

<!-- Copy the section above for each new feature. Number sequentially. -->
