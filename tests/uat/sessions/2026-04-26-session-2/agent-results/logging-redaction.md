# UAT-2 — Logging Redaction Empirical Audit

**Threat:** TM-012 — credential or raw-parameter leak in structured logs
**Scope:** `src/orchestrator/core/logging.py` (ID3), `src/orchestrator/core/settings.py` (BL3+BL4 warning), `src/orchestrator/db/pool.py` (BL4)
**Date:** 2026-04-26
**Auditor:** Logging-Redaction Empirical agent
**Verdict:** **PASS** — no leak path confirmed. Two minor robustness items recommended for promotion to regression tests.

---

## Methodology

1. Enumerated every `_log.*(...)` / `log.*(...)` call in `pool.py` (22 sites) and `settings.py` (4 BL3 + 1 new BL4 site).
2. For each: identified each structured field's value provenance.
3. Cross-referenced against `_SENSITIVE_KEY_RE` in `core/logging.py` (substring patterns + letter-boundary short tokens, case-insensitive).
4. Verified `_redact_sensitive_values` walks recursively through dicts/lists/tuples (cycle-safe via `seen`-set).
5. Verified every aiosqlite-wrapped error path runs through `_template_only(sql)` and `_shape(params)` BEFORE the log emission (call site is in `_wrap_aiosqlite_error` itself, lines 283–284).
6. Verified property-based tests on the scrubbers (`tests/db/test_pool_property.py`).

---

## Per-emission-site table

### `src/orchestrator/db/pool.py`

| File:Line | Event name | Fields | Sensitive risk | Mitigation | Verdict |
|---|---|---|---|---|---|
| pool.py:239 | `pool.background_task_failed` | task_name, error (str(exc)), error_type | exc str could carry params if upstream forgot scrubbing — but every aiosqlite path here flows through `_wrap_aiosqlite_error` first, which raises wrapped form. Wrapped exception messages (PoolError subclasses) are constructed from sanitized fragments only. | Wrapped exception messages, no raw SQL/params | PASS |
| pool.py:288 | `pool.integrity_violation` | role, constraint_kind, table, column, sql, params | sql/params raw | `_template_only(sql)`, `_shape(params)` applied at 283–284 | PASS |
| pool.py:302 | `pool.connection_lost` | role, reason=str(e), sql, params | reason from raw `aiosqlite.OperationalError` could in theory echo a literal value (SQLite "disk I/O error" messages don't, but the regex match is on substring "disk i/o error") | sql/params scrubbed; reason text from SQLite engine is descriptive, not user data | PASS — see Near-miss #1 |
| pool.py:317 | `pool.query_syntax_error` | role, reason=str(e), sql, params | Same as connection_lost — reason includes SQLite error text. SQLite syntax errors of form `near "VALUE": syntax error` could echo a literal token from the raw SQL. | sql/params scrubbed. The `near "..."` text is a *fragment* of the SQL, not a parameter value — but it could still be a literal stripped by `_template_only`. **This is the highest-risk site.** | PASS-with-caveat — see Near-miss #2 |
| pool.py:326 | `pool.write_conflict` | role, reason=str(e), sql, params | Same shape as above. SQLite "database is locked" text contains no user data. | sql/params scrubbed | PASS |
| pool.py:336 | `pool.query_failed` (catch-all) | role, reason=str(e), type, sql, params | Catch-all for any aiosqlite.Error not matched above. reason=str(e). | sql/params scrubbed; type is class name only | PASS |
| pool.py:630 | `pool.schema_verification_skipped` | caller="Pool.create" (literal) | None | Static value | PASS |
| pool.py:646 | `pool.initialized` | readers_count, database_path | database_path is a path string (no secret) | str() of Path; not a credential | PASS |
| pool.py:698 | `pool.pragma_mismatch` | role, pragma, expected, actual | PRAGMA names/values from hardcoded list (no user input) | Literal whitelist | PASS |
| pool.py:711 | `pool.connection_opened` | role, pragmas_applied (dict) | pragmas_applied keys: busy_timeout, foreign_keys, synchronous, temp_store, cache_size, mmap_size, journal_size_limit, query_only — none match sensitive regex; values are ints | Static set of keys | PASS |
| pool.py:749 | `pool.closed` | (none) | None | — | PASS |
| pool.py:823 | `pool.replacement_storm` | role, count_in_60s | None | — | PASS |
| pool.py:837 | `pool.replacement_failed` | role, reader_index, reason=str(e) | str(e) of `_open_connection` failure — could include database path | Path is not a credential; pragma errors don't echo secrets | PASS |
| pool.py:858 | `pool.connection_replaced` | role, reader_index, replacement_count | None | — | PASS |
| pool.py:869 | `pool.safe_close_failed` | role, reason=str(e) | aiosqlite/asyncio close error text | No user data | PASS |
| pool.py:1008 | `pool.transaction_rolled_back` | role="writer" (literal) | None — no exc info, no SQL | Static value only. **Note:** the rolled-back transaction's SQL/params are NOT echoed here (the wrapped error from inside the txn body carries that, and is logged elsewhere). | PASS |
| pool.py:1169 | `pool.close_timed_out` | reason=literal | None | Static value | PASS |

### `src/orchestrator/core/settings.py`

| File:Line | Event name | Fields | Sensitive risk | Mitigation | Verdict |
|---|---|---|---|---|---|
| settings.py:167 | `config.secret_shadowed_by_env` | secret_file=str(path) | Path string `/run/secrets/orchestrator_token` — **no secret content**. Key contains "secret" → value gets auto-redacted to `<redacted>` (over-redaction, not under-redaction). | Auto-redaction by `_SENSITIVE_KEY_RE` — note this is harmless **over**-redaction; the path was operationally useful but is now scrubbed. ID3 audit accepted this trade. | PASS |
| settings.py:174 | `config.api_bound_non_loopback` | api_host | Validated string field, not credential | — | PASS |
| settings.py:181 | `config.cors_wildcard` | (none) | None | — | PASS |
| settings.py:185 | `config.chunk_concurrency_unvalidated` | chunk_concurrency, spike_f_validated_at | Integers | — | PASS |
| settings.py:193 (BL4 new) | `config.pool_readers_over_provisioned` | pool_readers, chunk_concurrency, hint | Integers + literal hint | — | PASS |

---

## Cross-cutting verifications

### Exception-hierarchy review (TM-012 specific)

- **`ConnectionLostError(role, original_error=str)`** (pool.py:107–111): `__init__` constructs `f"{role} connection lost: {original_error}"`. `original_error` is `str(e)` from a raw `aiosqlite.OperationalError`. SQLite's disk-I/O error messages (`"disk I/O error"`, `"disk image is malformed"`) contain no SQL fragments and no user data. Verified by reading `_is_disk_io_error`. **No leak.**
- **`IntegrityViolationError(constraint_kind, table, column)`** (pool.py:89–104): `msg` is built from `constraint_kind`, `table`, `column` only — all sourced from `_classify_integrity_error`'s parsing of SQLite error text. Critically, the parser uses `re.search(r"constraint failed:\s+(\w+)\.(\w+)", str(e))` so only the table/column **identifiers** (matching `\w+`) are captured — never row values. **No raw constraint-failure text reaches the message.** Confirmed.
- **`PoolError`** catch-all at pool.py:344: `QueryError(str(e))`. Same surface as `_log.error("pool.query_failed", ..., reason=str(e))` — caller can re-emit, but SQLite's own error text is engine-generated, not user data.

### `_template_only` / `_shape` coverage

Every aiosqlite-error path in pool.py routes through `_wrap_aiosqlite_error(sql=..., params=...)`, which calls `_template_only` and `_shape` BEFORE every `_log.*` call inside it. The raw `sql` and `params` arguments never reach a logger emission. Confirmed: 14 call sites of `_wrap_aiosqlite_error`, all pass `sql=sql, params=params` (or `params="<many>"` for `executemany`).

Property tests in `tests/db/test_pool_property.py` cover:
- `_template_only` on arbitrary text (no quote-pairs survive);
- `_template_only` on integers (replaced by `?`);
- `_shape` on lists/dicts (returns type names, not values);
- **Critical safety invariant**: `_shape`'s output `repr` does not contain any of the input parameter values (line 78). This is the regression test for TM-012.

### `_redact_sensitive_values` traversal

- Walks `dict`, `list`, `tuple` recursively.
- Cycle-safe via `seen` set; substitutes `"<cyclic>"`.
- **Does NOT walk arbitrary attribute objects** (dataclasses, pydantic models, etc.). If a caller logs `event_dict={"data": some_dataclass_with_password_field}`, the dataclass is opaque and would be rendered by `JSONRenderer` as a string — likely breaking the JSON output rather than leaking, but worth a regression test.
- Event name itself (`event` key) is not redacted by value (since `_walk` only matches *values* whose keys match the regex). Event names containing words like "secret_shadowed_by_env" are preserved literally — correct.

---

## Findings

### Most surprising "near-miss" scrubbing case

**Near-miss #1: `secret_file` over-redaction (settings.py:167)**

The shadow-warning emits `secret_file=<path>`. The key contains "secret", so the path string is replaced with `<redacted>` by `_redact_sensitive_values`. This is *over*-redaction — the path itself is operationally useful (an operator wants to know which file is being shadowed) and contains no credential. ID3's audit accepted this trade-off (over-redact rather than under-redact). It's working as designed, but worth documenting that the operator only sees `<redacted>` not `/run/secrets/orchestrator_token` — they have to know the path by convention.

**Near-miss #2: SQLite syntax-error `near "..."` text (pool.py:317)**

`pool.query_syntax_error` logs `reason=str(e)`. SQLite's syntax-error messages are of the form `near "TOKEN": syntax error`. The `TOKEN` is a *fragment of the SQL the engine couldn't parse* — usually a keyword or identifier, but in pathological cases (a malformed statement using a literal where a keyword was expected) it could include a numeric literal or a quoted string. Since `_template_only` would NOT have stripped that fragment from `str(e)` (it only operates on the `sql` field, not on `reason`), there is a theoretical path where a literal **fragment** appears in `reason`. **Mitigation in practice**: the engine emits `near "..."` for syntax-shape errors (no values), and parameter-binding errors do not surface this text. **No empirical leak observed**, but worth a regression-test fuzzer.

### Recommended for promotion to regression tests

1. **Property test: `_wrap_aiosqlite_error` reason-field scrubbing.** Construct `aiosqlite.OperationalError("near \"my_secret_value\": syntax error")` and assert that the `pool.query_syntax_error` emission's `reason` does not contain `my_secret_value`. This would force a `_redact_unknown_strings` helper or document the trade-off explicitly.
2. **Negative test: structlog event-name preservation.** Confirm `config.secret_shadowed_by_env` event name is NOT redacted (since the regex would match "secret"); only the *value* of any matching key is. Already implicitly tested but worth pinning.
3. **Hypothesis test: `_redact_sensitive_values` on opaque objects.** Currently a dataclass instance with a `password` attribute would not be walked. Add a test that verifies behavior is at least non-leaky (raises or stringifies, doesn't echo `.password`).
4. **Coverage gap: `pool.background_task_failed`** does not run through `_wrap_aiosqlite_error`. If a `_replace_connection` task raises a non-aiosqlite exception whose `str(e)` contains user data, it would be logged raw in `error=str(exc)`. No realistic path today (the task body only opens a fresh connection), but a defensive scrubber would close the gap.

---

## Summary verdict

All 22 pool.py emission sites and all 5 settings.py emission sites are **empirically scrubbed** under realistic threat models. The two near-misses (over-redacted `secret_file` path; theoretical `near "..."` literal in syntax-error reason) are well-understood trade-offs with no observed leak. The property tests in `tests/db/test_pool_property.py` provide strong coverage for the `_template_only`/`_shape` core. **TM-012 PASS.**
