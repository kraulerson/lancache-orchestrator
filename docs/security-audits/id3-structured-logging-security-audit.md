# Security Audit — ID3 Structured Logging

**Feature:** ID3-structured-logging (Build Loop 2, Milestone B)
**Module:** `src/orchestrator/core/logging.py`
**Audit date:** 2026-04-22
**Auditor persona:** Senior Security Engineer (independent read-only sub-agent)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-04-22 -->

## Scope

Post-implementation security review of the rewritten ID3 structured-logging
framework, covering:

- `src/orchestrator/core/logging.py` (the rewrite)
- `tests/core/test_logging.py` (52 regression + baseline tests)
- `pyproject.toml` (per-file ruff S101/S105/S106 ignore for the test file)
- `.semgrep/orchestrator-rules.yaml` (`no-credential-log` rule excludes the
  test file; redaction tests necessarily use credential-named kwargs)

## Methodology

Two passes:

1. **Verification pass.** For each of the 4 UAT-1 findings (GH issues #9, #10,
   #14, #15), read the new code and confirm the claimed fix actually holds.
2. **Hunt pass.** Attack the new design — `request_context` nesting,
   `_protect_reserved_keys` collision behavior, `_redact_sensitive_values`
   regex correctness + cycle safety, `configure_logging` validation edge
   cases, structlog cache interactions.

Read-only static review with limited live reproduction against the project venv.

## Verification of UAT-1 findings

| GH Issue | Severity | Status | Evidence |
|---|---|---|---|
| [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9) — CID context bleed | SEV-2 | **CLOSED** | `request_context` uses token-based reset (`bind_contextvars` + `reset_contextvars`) so nested blocks restore the enclosing context. Exception-safe via `finally`. |
| [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10) — reserved-key clobber | SEV-2 | **CLOSED** | `_protect_reserved_keys` rescues user kwargs that collide with contextvars-bound reserved keys to `user_<key>`; falls back to numbered suffixes (`user_<key>_2`, `_3`, …) on collision. `event` is unclobberable at the Python call-site level (structlog's `meth(event, **kw)` → TypeError). |
| [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14) — no PII redaction | SEV-3 | **CLOSED** | `_redact_sensitive_values` recursively walks dicts/lists/tuples, replaces values of keys matching `_SENSITIVE_KEY_RE` with `<redacted>`. Letter-class boundaries correctly handle compound keys like `user_pwd`, `my_pin`, `otp_code`, `creds_list`. Cycle-safe via `frozenset[int]` seen-set. |
| [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15) — log_level silent fallback | SEV-3 | **CLOSED** | `configure_logging` validates input against `_VALID_LOG_LEVELS` (case-insensitive, stripped); raises `ValueError` on anything else. Parametrized tests cover `WARN`, `VERBOSE`, `TRACE`, `""`, `FATAL`. |

## Re-audit hunt findings

| # | Severity | Title | Status |
|---|---|---|---|
| N1 | SEV-3 | Nested `request_context` destroyed outer CID on inner exit | **FIXED** (commit `13e0843`) — token-based reset preserves the enclosing context. Regression test `test_nested_request_context_restores_outer_cid`. |
| N2 | SEV-3 | `_protect_reserved_keys` silently overwrote pre-existing `user_<key>` | **FIXED** (commit `13e0843`) — collision-aware rename to `user_<key>_2`, `_3`, … Test `test_protect_reserved_keys_collision_uses_numbered_slot`. |
| N3 | SEV-3 | **Short-token regex leaked credentials in compound keys** — `\b(?:pwd\|pin\|…)\b` silently failed because `\b` over `\w` treats `_` as word-char | **FIXED** (commit `13e0843`) — replaced with letter-class boundaries `(?:^\|[^a-zA-Z])…(?:[^a-zA-Z]\|$)`. 12-case parametrized regression test + 4-case false-positive guard (`pinnacle`, `spinner`, `pinhead`, `saltwater` pass through). Confirmed the original regex leaked `user_pwd`, `my_pin`, `otp_code`, `creds_list`, `nonce_bytes`, `salt_value`, `user_sid` — actual credential leak, caught before shipping. |
| N4 | SEV-3 | Circular event_dict → `RecursionError` crashed the log call | **FIXED** (commit `13e0843`) — `_walk` tracks `id(obj)` in a `frozenset[int]` seen-set; substitutes `"<cyclic>"` on repeat. Test `test_cyclic_event_dict_does_not_recurse_infinitely`. |
| N5 | SEV-4 | `_walk` rebuilds dicts even when nothing redactable | **DEFERRED** — tracked as [#22](https://github.com/kraulerson/lancache-orchestrator/issues/22). Minor perf; no correctness impact. |

## Non-findings (explicitly checked, clean)

- **`configure_logging` validation bypass** — `""`, zero-width space, unicode dodges all raise `ValueError`.
- **Exception safety of `clear_request_context`** — `structlog.contextvars.clear_contextvars()` does not raise under normal use; `finally` is safe.
- **Asyncio task CID isolation** — relies on Python's native contextvars; structlog's `contextvars` module uses `ContextVar` under the hood. Test `test_cid_isolation_across_asyncio_tasks` exercises real concurrent tasks.
- **`event=` call-site clobber** — blocked by structlog's bound-logger signature (`meth(event, **kw)`); `TypeError` at call time. Covered by `test_user_kwarg_event_blocked_at_python_level`.
- **`level` / `timestamp` user-kwarg collision** — `_protect_reserved_keys` is a no-op (they aren't bound in contextvars); downstream `add_log_level` / `TimeStamper` overwrite. Stdlib wins, user's value is silently dropped — acceptable behavior, documented.
- **`structlog.reset_defaults()` + `cache_logger_on_first_use=True`** — `reset_defaults` re-initializes `_CONFIG` including the cache. Autouse fixture isolates tests.
- **Semgrep `no-credential-log` exclusion for `tests/core/test_logging.py`** — narrowly scoped, paired with ruff `S105/S106` ignore for the same file. Does not weaken production coverage.
- **Redaction marker collision with sensitive-key regex** — `<redacted>` contains no substring matching the regex; no second-pass over-redaction risk.
- **`migrate.py` logger usage** — uses `structlog.get_logger()` with no credential kwargs; no leak path.

## Decision

**ID3 is cleared to advance through the Build Loop** after the N1–N4 hardening
pass (commit `13e0843`). All SEV-2 and SEV-3 findings are closed and exercised
by regression tests. The remaining SEV-4 perf finding (N5) is tracked as a
follow-up issue to resolve opportunistically.

## Follow-up tracking

- [#22](https://github.com/kraulerson/lancache-orchestrator/issues/22) — `_redact_sensitive_values` allocation short-circuit (N5, SEV-4)

## Sign-off

- Implementation: commits `15203c6` (initial rewrite) + `13e0843` (hardening pass)
- Test suite: `tests/core/test_logging.py` (55 tests; 112 project-wide)
- Lint/type: ruff clean, mypy --strict clean
- Gates: pre-commit hooks (gitleaks, Semgrep, ruff, mypy) all green
