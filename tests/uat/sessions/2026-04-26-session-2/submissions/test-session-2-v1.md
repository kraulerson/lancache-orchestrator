# UAT Test Session — 2 (v1) — Submission

**Date:** 2026-04-26
**Features Under Test:** BL3 ID4 Settings module + BL4 DB pool
**Tester:** Karl (Orchestrator)
**Format:** H-1 lightweight
**Run via:** local shell, venv-active, `feat/uat-2-session` branch

---

## Pre-flight

| # | Check | Actual | Pass |
|---|---|---|---|
| P1 | venv active | `/Users/karl/Documents/Claude Projects/lancache_orchestrator/.venv/bin/python` | ✓ |
| P2 | branch | `feat/uat-2-session` | ✓ |
| P3 | clean tree | only `M .claude/process-state.json` (auto-bumped by checklist), `?? .claude/settings.json.pre-wire-backup`, `?? tests/uat/sessions/2026-04-26-session-2/` | ✓ |
| P4 | unit test baseline | 250 passed, 3 deselected | ✓ |

**Pre-flight all-pass: ✓**

---

## Scenarios

| # | Scenario | Pass | Notes |
|---|---|---|---|
| 1 | Settings construction with valid env | ✓ | Output: `127.0.0.1 8765 8` and `SecretStr('**********')`. Pydantic-settings UserWarning about `/run/secrets` is known noise (BL3 follow-up tracked). |
| 2 | Diagnostic warnings on misconfiguration | ✓ | Both `config.api_bound_non_loopback` (api_host=0.0.0.0) and `config.cors_wildcard` warnings fired correctly. |
| 3 | Run migrations on a fresh DB | ✓ | Note: actual `schema_migrations.name` is `0001_initial` (template said `initial` — template bug on AI side, not code). PRAGMA journal_mode = `wal`. |
| 4 | Pool init from Settings + basic query | ✓ | Inline `python -c` had bash-quoting issues (template fragility); helper script `/tmp/uat2-scenario4.py` PASSED with `row={'name': 'steam'}`. Pool correctly opened 1 writer + 8 readers, schema-verified at init. |
| 5 | Reader query_only enforcement | ✓ | `BLOCKED: attempt to write a readonly database` — confirms `PRAGMA query_only=ON` defense holds at the SQLite layer. |
| 6 | schema_status + health_check shapes | ✓ | `schema_status = {applied:[1], available:[1], pending:[], unknown:[], current:True}`. `health_check = {writer:{healthy:True, replacements:0}, readers:{total:8, healthy:8, replacements:0}, uptime_sec:0}`. Shape matches docs. |
| 7 | Bad env var rejected | ✓ | `ORCH_TOKEN='short'` raised `ValueError: orchestrator_token validation failed: ...` — message did NOT echo `'short'` (BL3 A2 scrubbing fix verified). `ORCH_POOL_READERS=99` raised `ValidationError: ... Input should be less than or equal to 32` — boundary enforcement works. |
| 8 | Cleanup | ✓ | tmp DB removed, env vars unset. Clean exit. |

**All 8 scenarios pass.**

---

## Bugs Found

| # | Severity | Feature | Description |
|---|---|---|---|
| _none_ | — | — | No bugs surfaced by the manual session. |

---

## AI-side template bugs (informational, not project bugs)

- **T1 — Template Scenario 3:** Expected `1|initial` from `schema_migrations`; actual is `1|0001_initial`. Cosmetic — fix in test-session-2-v2 if there's a re-run.
- **T2 — Template Scenario 4 inline `python -c`:** Multi-line `async def` inside a single `-c` invocation hits zsh dquote/heredoc parsing quirks. Helper script `/tmp/uat2-scenario4.py` works. Future templates: drop the inline form, link only to scripts.

---

## Overall Notes

Foundational modules behaved exactly as the BL3 + BL4 audits / specs claim:
- SecretStr redaction holds across construction, repr, and validation-error paths
- 4 diagnostic warnings (api-bind, CORS, secret-shadow, chunk-concurrency) + new BL4 (pool_readers_over_provisioned) all fire on cue
- Migrations apply cleanly with WAL+FK+correct journal mode
- Pool boots in <50 ms with 8 readers, all PRAGMAs verified
- Reader query_only PRAGMA enforced at SQLite layer (defense-in-depth alongside the application-level reader/writer split)
- Schema-drift detection works (current=True on a fresh apply)
- Boundary validation rejects bad inputs with scrubbed error messages
