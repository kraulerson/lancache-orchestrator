# UAT-2 Consolidated Findings + Triage Matrix

**Session:** UAT-2 (covers BL3 ID4 Settings + BL4 DB pool)
**Date:** 2026-04-26
**Agents dispatched:** 5 parallel (SAST, threat-model, data-isolation, input-validation, logging-redaction)
**Status:** awaiting orchestrator triage on SEV-2 items

## Severity counts

| Severity | Count | Source agents |
|---|---|---|
| SEV-1 | 0 | — |
| SEV-2 | 6 | input-validation (all 6) |
| SEV-3 | ~14 | data-isolation (4 medium), input-validation (11), logging-redaction (3 recs) |
| SEV-4 | ~30+ | SAST (3), threat-model (4 new threats), input-validation (14), data-isolation (12 test-coverage) |

Per Bible §12.2: **SEV-1 cannot be deferred. SEV-2 can be deferred during Phase 2 but must be resolved or feature removed at Phase 2→3 gate.** SEV-3 / SEV-4 can be deferred freely with documented rationale.

---

## SEV-2 findings (require triage decision)

| # | Title | Source | Recommendation | Rationale |
|---|---|---|---|---|
| **V-1** | `Pool.create(readers_count=0)` hangs forever — `asyncio.Queue(maxsize=0)` is unbounded but no readers opened, first read blocks indefinitely | input-validation | **Fix Now** | Easy fix (add `if readers_count < 1: raise PoolInitError(...)` in `Pool.__init__` or `_async_create`). The Settings layer clamps to ≥1 so the singleton path is safe, but Pool.create() is also called from tests with explicit values — the floor must be enforced at the Pool layer too. |
| **V-2** | Symlink `database_path` to NFS bypasses `_assert_local_filesystem`. `_detect_filesystem_type` checks the symlink's mount, not the target's. WAL-on-NFS would silently corrupt | input-validation | **Fix Now** | Migration runner is the single boot-time gatekeeper for "no WAL on NFS" — bypass voids the entire defense. Fix: `os.path.realpath(path)` before the FS-type lookup. ~3-line change in migrate.py. |
| **V-3** | `/dev/null` as `database_path` is silently accepted; sqlite opens, all writes vanish | input-validation | **Defer to Post-MVP** | Pathological config; operator footgun, not security. Document in HANDOFF.md ("don't point database_path at character devices"). Cost-of-fix > value at MVP. |
| **V-4** | `read_one_as(NotADataclass, ...)` raises raw `TypeError` instead of wrapped `QueryError` — broken contract | input-validation | **Fix Now** | API-contract violation. Fix: `if not is_dataclass(cls): raise TypeError(...)` at top of helper, or wrap in `_row_to_dataclass`. ~2 lines. Should also have a regression test. |
| **V-5** | `ORCH_TOKEN` with embedded `\x00` / `\r\n` accepted (only ASCII whitespace stripped) | input-validation | **Fix Now** | Token containing NUL or CRLF could cause downstream issues (HTTP header injection if echoed back, log-line truncation, parser confusion). Fix: validator rejects tokens with control chars. ~3 lines. |
| **V-6** | `_template_only` doesn't normalize hex literals (`0xDEADBEEF`) and only partially normalizes scientific notation (`1.5e10` → `?.5e1?`, digits leak) | data-isolation | **Fix Now** | TM-012 invariant ("no raw values in logs") leaks for hex/sci-notation literals. Single regex tweak: extend `_LITERAL_RE` with `\b0[xX][0-9a-fA-F]+\b` and tighten the numeric-literal rule. Property tests in `test_pool_property.py` should grow to cover. |

**Recommended triage:** Fix Now V-1, V-2, V-4, V-5, V-6 (5 fixes — none individually large, total ≈ 1-2 hours). Defer V-3 to Post-MVP.

---

## SEV-3 findings (defer with rationale unless escalated)

Counts only; full details in per-agent reports.

| Source | Count | Examples |
|---|---|---|
| data-isolation | 4 | G3.1 ROLLBACK-fail chaos test, G5.1 health_check / replace race, G1.3 token-scrubber heuristic, G6.3 (escalated to V-6 SEV-2) |
| input-validation | 11 | Path-traversal on `database_path`/session paths, missing `api_host` validation, `cors_origins` non-JSON parsing surprise, migration filename case sensitivity, `pool_busy_timeout_ms=0` accepted, more |
| logging-redaction | 3 (recs) | Hypothesis test on `_wrap_aiosqlite_error` echo, `_redact_sensitive_values` traversal of opaque attrs, `pool.background_task_failed` scrubbing |

**Recommended:** defer all SEV-3 findings to follow-up issues (file as `area:db` / `area:settings` SEV-3). They're hardening / test-coverage items, not active vulnerabilities.

---

## SEV-4 findings (file as follow-ups, do not fix this UAT)

- SAST X1 (bandit dev-dep) — already SEV-4 follow-up from BL3 (#26 area)
- SAST X2 (subprocess timeout zombie) — operational hygiene
- SAST X3 (mountinfo silent-skip on malformed lines) — acceptable under threat model
- Threat-model TM-NEW-2 (replacement-storm CRITICAL log doesn't auto-degrade) — already partially addressed by storm-guard return; rest is doc
- Threat-model TM-NEW-3 (bg-task exception logging may be lost on OOM-kill) — accepted; ops doc in Phase 4 HANDOFF
- Threat-model TM-NEW-4 (concurrent-process startup race on verify_schema_current) — fail-fast under ADR-0001 single-container; doc-only
- Threat-model: extend Semgrep `no-credential-log` keyword list (`bearer`, `api_key`, `jwt`, `session_secret`)
- 14 input-validation SEV-4s (Unicode digits in `cache_levels`, etc. — operator-surprise items)
- 12 data-isolation test-coverage gaps (deepcopy, str(s), tx._conn write smuggle, mid-stream IO error, etc.)

**Recommended:** consolidate into ~3-4 follow-up issues:
1. **Strengthen `_template_only` regex coverage** — already filed as #39 (BL4); add hex+sci-notation cases (overlap with V-6 if Fix Now)
2. **Pool.py branch coverage 81% → 100%** — already filed as #42; absorb the 12 data-isolation test gaps
3. **Operations docs for Phase 4 HANDOFF** — TM-NEW-2/3/4 + bandit dev-dep + Pipfile drift (#43)
4. **Path-traversal hardening for path-typed Settings fields** — new SEV-3 issue covering V-3 (deferred), database_path symlink, session paths

---

## Non-findings cleared

- 35 `.execute(` call sites — all parameterized except the 4 `# nosem`-suppressed PRAGMA / control statements (justified)
- 5 regexes timed against 50K-char pathological inputs — all linear (≤0.0026s)
- pickle blocked + asyncio singletons race-free
- 22 of 22 `_log.*` sites in pool.py + 5/5 in settings.py — all scrub via `_template_only`+`_shape` or `_redact_sensitive_values`
- TM-001/005/012/021 — strong mitigations across the board
- 7 isolation boundaries — secret/non-secret, reader/writer, txn/txn, schema, replacement, snapshot, singleton — all hold

---

## Per-agent reports

- `sast-cross-check.md`
- `threat-model-walk.md`
- `data-isolation.md`
- `input-validation.md`
- `logging-redaction.md`
