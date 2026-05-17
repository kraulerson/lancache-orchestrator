# Security Audit — UAT-2 Remediation

**Feature:** UAT-2-remediation (synthetic build_loop wrapper for the
six SEV-2 fixes triaged from the UAT-2 session)
**Audit date:** 2026-04-26
**Auditor:** UAT-2 parallel agent dispatch (5 agents) — see consolidated report

<!-- Last Updated: 2026-04-26 -->

## Scope

This is a remediation commit, not a new feature. The audit work was
the **UAT-2 session itself**: 5 parallel agents (SAST cross-check,
threat-model walk, data-isolation probe, input-validation fuzz,
logging-redaction empirical) audited BL3 Settings + BL4 DB pool and
surfaced 6 SEV-2 findings. This commit ships the fixes for those
findings test-first.

## Findings audited and resolved

All 6 SEV-2 items are addressed in this commit:

| # | Title | Source agent | Fix |
|---|---|---|---|
| V-1 | `Pool.create(readers_count=0)` deadlocks on first read | input-validation | `Pool.__init__` raises `PoolInitError` when readers_count < 1 |
| V-2 | Symlink `database_path` to NFS bypasses local-fs guard | input-validation | `_assert_local_filesystem` resolves symlinks via `Path.resolve(strict=False)` before any FS-type / device check |
| V-3 | Character/block devices silently accepted as `database_path` | input-validation | `_assert_local_filesystem` rejects `S_ISCHR` / `S_ISBLK` paths with clear `MigrationError` |
| V-4 | `read_one_as` / `read_all_as` raise raw `TypeError` on non-dataclass cls | input-validation | New `_check_is_dataclass` helper raises a clear `TypeError` at all 6 entry points (Pool / ReadTx / WriteTx × read_one_as / read_all_as) |
| V-5 | `ORCH_TOKEN` with embedded NUL/CR/LF/TAB silently accepted | input-validation | `_check_token_length` post-strip body scan rejects all bytes in 0x00–0x1F + 0x7F |
| V-6 | `_template_only` doesn't normalize hex / partially-leaks sci-notation | data-isolation | `_LITERAL_RE` extended with `0xHEX` alternative + sci-notation exponent on numeric branch; 6 new property tests |

## Test coverage

17 new regression tests added across 4 files:
- `tests/db/test_pool.py` — V-1 (2 tests), V-4 (3 tests)
- `tests/db/test_migrate.py` — V-2 (1 test, monkeypatched fs detection), V-3 (1 test, `/dev/null` reject)
- `tests/core/test_settings.py` — V-5 (4 tests: NUL, CRLF, embedded TAB, clean-token-with-trailing-whitespace happy path)
- `tests/db/test_pool_property.py` — V-6 (6 tests: hex / lowercase hex / capital-X hex / sci-notation positive / negative / explicit-positive)

`REDACTION_TOKEN_SHAPES` parametrize fixture in `test_settings.py` had its `embedded-newline` shape replaced with a `symbol-rich` shape (V-5 makes the embedded-newline shape unrepresentable; coverage shifts from "redaction holds" to "rejection happens" — and the V-5 tests cover the rejection path).

Full project suite: **281 passed, 3 deselected** (the 3 are
`@pytest.mark.slow` integration tests, unchanged).

## Tooling state

- ruff check + format — clean across `src/` and `tests/`
- mypy --strict — clean across `src/` (15 source files)
- semgrep p/owasp-top-ten + project custom rules — clean (no new findings)

## Non-findings (deferred per UAT-2 triage)

- ~14 SEV-3 + ~30+ SEV-4 items consolidated into 4 follow-up issues:
  - Path-traversal hardening for path-typed Settings fields (covers V-3 deferred + symlink/relative-path scenarios)
  - `pool.py` branch coverage 81% → 100% (#42, BL4 follow-up; absorbs the 12 data-isolation test gaps)
  - Operations docs for Phase 4 HANDOFF (TM-NEW-2/3/4 + bandit dev-dep + Pipfile drift)
  - Semgrep `no-credential-log` keyword extension (`bearer`, `api_key`, `jwt`, `session_secret`)

## Cross-references

- UAT-2 session: `tests/uat/sessions/2026-04-26-session-2/`
- Consolidated findings + triage matrix: `tests/uat/sessions/2026-04-26-session-2/agent-results/_consolidated.md`
- Per-agent reports: `tests/uat/sessions/2026-04-26-session-2/agent-results/{sast-cross-check,threat-model-walk,data-isolation,input-validation,logging-redaction}.md`
- BL3 audit baseline: `docs/security-audits/id4-settings-security-audit.md`
- BL4 audit baseline: `docs/security-audits/db-pool-security-audit.md`

## Sign-off

UAT-2 remediation is cleared to ship. 0 SEV-1 / 0 SEV-2 outstanding
post-fix. SEV-3/4 follow-ups will be filed as separate issues after
this commit lands.
