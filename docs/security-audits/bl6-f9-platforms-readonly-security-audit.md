# Security Audit — BL6 platforms read-only endpoint

**Feature:** BL6-F9-platforms-readonly (Build Loop 6, Milestone B)
**Module:** `src/orchestrator/api/routers/platforms.py` (~90 LoC) + `src/orchestrator/api/main.py` (+2 lines wiring)
**Audit date:** 2026-05-04
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST (semgrep OWASP top-10 + project custom rules) + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-04 -->

## Scope

Post-implementation security review of the first real F9 read endpoint:

- `src/orchestrator/api/routers/platforms.py` — `PlatformResponse` + `PlatformListResponse` Pydantic models with `extra="forbid"`, `list_platforms` GET handler reading via `Depends(get_pool_dep)`, structured 503 path on `PoolError`.
- `src/orchestrator/api/main.py` — added `from orchestrator.api.routers.platforms import router as platforms_router` and `app.include_router(platforms_router)`.
- `tests/api/test_platforms_router.py` — 23 tests across 7 classes (~280 LoC).

The audit inherits the BL5 + UAT-3 substrate: middleware-based bearer auth, CORS-outermost stack, correlation-ID propagation, ID3 log redaction. Auth, body-cap, and CORS handling are not in scope here — they were audited in BL5 and re-validated in UAT-3.

## Methodology

1. **Automated SAST:** `semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py` — 0 findings.
2. **gitleaks** on the full repo (105 commits) — 0 findings.
3. **ruff check + ruff format** on changed files — clean.
4. **mypy --strict** on changed files — clean.
5. **Manual review** against the spec's locked decisions D1-D8 + the project threat model TMs that apply at the API surface (TM-001 auth, TM-012 redaction, TM-013 fingerprinting).

## Findings

**SEV-1:** 0
**SEV-2:** 0
**SEV-3:** 0
**SEV-4:** 0

No findings. Rationale below for each TM walk and design decision.

## Threat-model walk

### TM-001 — Unauthorized API access
**Verdict: MITIGATED.** Path is NOT in `AUTH_EXEMPT_PATHS`; BL5's `BearerAuthMiddleware` enforces. Tests `TestPlatformsAuth` cover missing header (401), wrong token (401), valid token (200). Auth-state matrix from UAT-3's auth-lifespan audit applies unchanged.

### TM-012 — Credential redaction in logs
**Verdict: MITIGATED.** The endpoint logs only `api.platforms.read_failed` with `reason=str(e)` — `e` is a `PoolError` whose message comes from the BL4 pool's structured exception layer (no raw SQL/params/credentials per ADR-0011). Correlation_id propagated from the outer middleware. No raw row contents (including `last_error` text) reach any log call.

### TM-013 — Fingerprinting via differential responses
**Verdict: MITIGATED for the BL5 substrate; not amplified by this endpoint.** The endpoint produces only two response shapes: 200 with the canonical envelope, and 503 with `{"detail": "database unavailable"}`. No path-dependent timing differences (single SQL query, deterministic ORDER BY, same response shape regardless of input).

### Decisions D1-D8 walk

- **D1 (`config` excluded):** Verified — SELECT statement omits `config`; `PlatformResponse` has no `config` field; `extra="forbid"` blocks accidental construction. Test `TestPlatformsConfigExclusion::test_config_not_in_response_when_set` writes a sensitive-looking JSON into the DB row's `config` and asserts neither the field name nor the value reaches the wire. **Defense-in-depth:** even if a future migration added `config` to the SELECT by mistake, `extra="forbid"` would raise during model construction in development → caught in CI.
- **D2 (wrapped envelope):** Verified — `PlatformListResponse` shape; `TestPlatformsHappyPath::test_response_envelope_shape` asserts exactly one top-level key `"platforms"`.
- **D3 (`last_error` truncation):** Verified — `_LAST_ERROR_TRUNCATE = 200`; truncation is `[:200]` on the raw string. Tests at boundaries 100, 200, 201, 5000 + null. Defense-in-depth on top of upstream redaction (which doesn't yet exist; F1/F2 will write structured errors). Note: 200-char truncation is by codepoint, not bytes — for UTF-8 multi-byte content a partial-codepoint cut isn't possible (Python str slicing is codepoint-aware), so no malformed-UTF-8-on-wire risk.
- **D4 (Steam-first sort):** Verified via SQL `ORDER BY CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name`. Tests assert at index 0 (steam) and index 1 (epic).
- **D5 (no ETag):** Not implemented; documented in spec as YAGNI for v1.
- **D6 (PoolError → 503):** Verified — `except PoolError` with structured 503 body and `api.platforms.read_failed` log event. Tests pin both the wire response and the log emission.
- **D7 (bearer required):** Verified — path NOT added to `AUTH_EXEMPT_PATHS` in `dependencies.py`.
- **D8 (`extra="forbid"`):** Verified on both `PlatformResponse` and `PlatformListResponse`. `TestPlatformsResponseSchema` covers extra-field rejection + 3 Literal-narrowing rejection cases (invalid name, auth_status, auth_method).

## Non-findings (explicitly cleared)

- **No SQL injection vector.** The single SQL is a parameter-free literal string with no user-input interpolation. The CASE clause is hardcoded.
- **No timing oracle on auth.** Auth handling is delegated to the middleware (audited in BL5/UAT-3); this router executes only after auth has passed.
- **No row-count fingerprinting.** The endpoint always returns exactly 2 platforms (schema CHECK constraint + seed); response size is constant at ~400 bytes regardless of any state.
- **No DoS via response size.** Two rows max; `last_error` capped at 200 chars; total response well under any reasonable response-size limit.
- **No log-volume amplification.** Successful reads emit only the standard `api.request.received` + `api.request.completed` from `CorrelationIdMiddleware`; the router itself emits only on the 503 error path.
- **No ETag/304 oracle.** No conditional response handling (D5 deferred ETag).
- **No CORS interaction risk.** The router doesn't override CORS; the BL5+UAT-3 stack handles it (CORS outermost since UAT-3 S2-F).

## Test coverage

23 tests in `tests/api/test_platforms_router.py`, all passing. Branch coverage on `routers/platforms.py` ≥ 95% (target met per spec §4).

## Verification artifacts

- `pytest tests/api/ -q`: 99 tests passing (76 prior + 23 new).
- `pytest -q`: 387 tests passing project-wide (was 364; +23).
- `ruff check`: clean.
- `ruff format --check`: clean.
- `mypy --strict src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py`: clean.
- `semgrep --config p/owasp-top-ten --error`: 0 findings.
- `gitleaks detect --source .` (full history, 105 commits): no leaks found.

## Conclusion

**APPROVED for merge.** Zero findings across automated and manual review. The endpoint inherits BL5+UAT-3's hardened substrate, adds no new attack surface beyond the read of two seeded rows, and respects every locked decision D1-D8. The conventions established here (envelope shape, response strictness, error semantics) are sound for propagation to the next F9 endpoints (`/games`, `/jobs`, `/manifests`, etc.).
