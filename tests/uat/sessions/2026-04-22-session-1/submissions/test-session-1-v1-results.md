# UAT Session 1 — lancache_orchestrator Results

**Date:** 2026-04-22
**Tester:** Karl
**Features:** ID1 migrations, ID3 logging, infrastructure hardening

**Summary:** 12 passed, 3 failed, 1 skipped, 0 not tested

---

## Scenarios

| # | Scenario | Result | Notes |
|---|---|---|---|
| 1 | Fresh install — migrations apply to an empty DB | PASS | |
| 2 | Idempotent re-apply — running again is a no-op | FAIL | orch-uat.db was present. Output shown: `filesystem_type_unknown db_path=/tmp/orch-uat.db hint="WAL requires a local filesystem; detection returned 'unknown'..."` then `migrations_complete applied_count=1` |
| 3 | Strict mode — refuses to boot on unknown filesystem | PASS | |
| 4 | CHECKSUMS drift — tampered migration is detected | PASS | |
| 5 | Migration test suite passes (42 tests) | FAIL | `test_concurrent_runners_serialize` — `OperationalError('database is locked')`. 41 of 42 pass. |
| 6 | Boot log emits valid JSON on stdout | PASS | |
| 7 | Redaction — password and api_key values are masked | PASS | |
| 8 | log_level='WARN' raises ValueError (no silent fallback) | PASS | |
| 9 | request_context() clears contextvars even on exception | FAIL | `IndentationError: expected an indented block after 'try' statement on line 4` — heredoc lost indentation when rendered from HTML |
| 10 | Logging test suite passes (55 tests) | PASS | |
| 11 | Full project test suite + coverage report | PASS | |
| 12 | Build a wheel and verify packaged migrations are inside | PASS | |
| 13 | Docker image builds from the digest-pinned base (OPTIONAL) | SKIP | |
| 14 | Review the integration agent report | PASS | |
| 15 | Review the adversarial agent report — triage 7 new findings | PASS | **Fix all** |
| 16 | Review the docs-handoff agent report — decide what to refresh | PASS | **Refresh bible** |
