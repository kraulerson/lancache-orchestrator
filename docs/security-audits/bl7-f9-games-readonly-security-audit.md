# Security Audit — BL7 games read-only endpoint

**Feature:** BL7-F9-games-readonly (Build Loop 7, Milestone B)
**Module:** `src/orchestrator/api/routers/games.py` (~250 LoC) + `src/orchestrator/api/_query_helpers.py` (~290 LoC) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-20
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST (semgrep OWASP top-10) + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-implementation security review of:
- `routers/games.py` — handler + Pydantic models + per-endpoint filter/sort allow-lists
- `_query_helpers.py` — shared parser/validator/SQL-builder primitives that future paginated endpoints will reuse

The audit inherits the BL5+BL6+UAT-3 substrate (bearer auth, CORS-outermost stack, correlation_id propagation, ID3 redaction). Auth, body-cap, CORS, and middleware behavior are not in scope here.

## Methodology

1. **Automated SAST**: `semgrep --config p/owasp-top-ten --error` — 0 findings
2. **gitleaks**: full repo scan (110 commits) — 0 findings
3. **ruff check + ruff format**: clean across all 6 files
4. **mypy --strict**: clean across all 3 source files
5. **Property-based SQL-injection test**: Hypothesis test in `test_query_helpers.py::TestSqlInjectionResistance` exercises `build_where_clause` with random and adversarial inputs (including `"'; DROP TABLE games; --"`); asserts no user value ever appears literally in the SQL string
6. **Manual review** against TM-005 (SQL injection), TM-012 (log redaction), spec §6 risk register

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings. Rationale below.

## Threat-model walk

### TM-005 — SQL injection via API surface
**Verdict: MITIGATED.** Two-layer defense:
1. Field names are interpolated into SQL but ONLY after `parse_filters`/`parse_sort` validate them against the endpoint's `FilterAllowList`/`SortAllowList`. Identifiers outside the allow-list raise `QueryParamError` → 400 before any SQL touches them. The defensive re-check in `build_where_clause` is a layered invariant.
2. User values flow EXCLUSIVELY through SQLite parameter binds. Verified by `TestSqlInjectionResistance` property test (Hypothesis): under random and adversarial input including `"'; DROP TABLE games; --"`, the SQL string contains only `?` placeholders for values.

The `S608` warnings from semgrep on the f-string SQL construction in `list_games` are tagged `# noqa: S608` with comment referencing the security invariants (interpolated values are allow-list-validated identifiers only; user values use `?` placeholders).

### TM-012 — Credential redaction in logs
**Verdict: MITIGATED.** Endpoint emits:
- `api.games.read_failed` with `reason=str(e)` on PoolError. `PoolError` messages come from BL4's structured exception hierarchy (no raw SQL, params, or credentials — ADR-0011).
- `api.games.metadata_parse_failed` with `game_id` only — no metadata content. The actual malformed JSON string never reaches a log call.

### TM-013 — Fingerprinting via differential responses
**Verdict: MITIGATED for the BL5 substrate; not amplified.** Three response shapes only: 200 with canonical envelope, 400 with `{detail}`, 503 with `{detail}`. Filter/sort timing depends on data; not on auth state.

## Decisions D1-D12 walk

- **D1 offset pagination**: Verified via `TestGamesPagination`. Limit/offset enforced at parser, parameterized into SQL.
- **D2 rich meta**: Verified via `TestGamesAppliedEcho`. `applied_filters` echo uses `FilterCriterion` model with `extra="forbid"`.
- **D3 default=50, max=500, reject 400**: Verified via `TestGamesPagination::test_limit_above_max_returns_400`.
- **D4 operator-suffix syntax**: Verified via per-field test classes covering `=`, `_in`, `_gte`, `_lte`. Unknown op or field → 400.
- **D5 tie-breaker + de-dup**: Verified via `TestGamesSort::test_user_id_sort_dedupes_tie_breaker`.
- **D6 metadata included as JSON**: Verified via `TestGamesMetadata`. Malformed JSON → null + structured log.
- **D7 last_error truncated to 200**: Verified via `TestGamesLastErrorTruncation`.
- **D8 empty result returns 200**: Verified via `TestGamesEmptyDb`.
- **D9 unknown field/op → 400**: Verified via `TestGamesErrors`.
- **D10 Pydantic extra="forbid"**: All response models set it.
- **D11 bearer required**: Verified via `TestGamesAuth`. `/api/v1/games` not in `AUTH_EXEMPT_PATHS`.
- **D12 PoolError → 503**: Verified via `TestGamesPoolFailure`. Structured log with correlation_id propagated.

## Non-findings (explicitly cleared)

- **No SQL injection vector.** All values parameterized. Field names allow-list validated.
- **No timing oracle on auth.** Auth handled by BL5 middleware; reached only after auth passes.
- **No fingerprinting via 200/400/503 shape.** Response body shape is consistent within each status.
- **No DoS via response size.** `limit ≤ 500` enforced at parser; `metadata` bounded; `last_error` truncated.
- **No log-volume amplification on hot path.** Success path emits only middleware's `api.request.received` + `api.request.completed`; router emits only on 503 or metadata-parse-failure paths.

## Test coverage

70 tests total: 32 helpers + 38 router. Hypothesis property test for SQL injection. Branch coverage on both modules ≥95%.

## Verification artifacts

- `pytest -q`: 457 tests passing project-wide (was 387; +70)
- `ruff check` + `ruff format --check`: clean
- `mypy --strict`: clean
- `semgrep --config p/owasp-top-ten --error`: 0 findings
- `gitleaks detect`: no leaks

## Conclusion

**APPROVED for merge.** Zero findings across automated and manual review. The endpoint inherits BL5+BL6+UAT-3's hardened substrate and adds no new attack surface beyond a parametric SQL builder whose injection resistance is pinned by both unit tests and a property-based test. The `_query_helpers.py` conventions established here propagate to future paginated F9 endpoints with the same security guarantees.
