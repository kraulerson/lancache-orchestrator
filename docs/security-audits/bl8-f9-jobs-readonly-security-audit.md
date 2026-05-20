# Security Audit — BL8 jobs read-only endpoint

**Feature:** BL8-F9-jobs-readonly (Build Loop 8, Milestone B)
**Module:** `src/orchestrator/api/routers/jobs.py` (~225 LoC) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-20
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-implementation security review of:
- `routers/jobs.py` — handler + Pydantic models + per-endpoint filter/sort allow-lists
- 2-line wire-up in `main.py`
- New `jobs_pool_seeded` fixture in `tests/api/conftest.py`

The audit inherits the BL5+BL6+BL7+UAT-3+UAT-4 substrate. The shared `_query_helpers.py` module's security guarantees apply unchanged.

## Methodology

1. **Automated SAST**: `semgrep --config p/owasp-top-ten --error` — 0 findings
2. **gitleaks**: full repo scan (126 commits) — 0 findings
3. **ruff check + ruff format**: clean across all 4 files
4. **mypy --strict**: clean across `routers/jobs.py` + `main.py`
5. **UAT-4 property-based SQL-injection test** in `test_query_helpers.py::TestSqlInjectionResistance` still passes — covers the shared `build_where_clause`/`build_order_by_clause` that BL8 uses
6. **Manual review** against TM-005 (SQL injection), TM-012 (log redaction), spec §6 risk register

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings. BL8 is structurally identical to BL7 — same composition of UAT-4-hardened helpers, different per-endpoint allow-list. The 12 UAT-4 fixes apply transparently.

## Decisions D1-D14 walk

- **D1 default sort id:desc**: verified via `TestJobsSort::test_default_id_desc` + `test_default_applied_sort_dedup_no_tie_breaker`
- **D2 payload included as JSON**: verified via `TestJobsPayloadAndError::test_well_formed_payload_parsed`; oversized + malformed + non-dict cases all return null
- **D3 `_is_null` deferred**: no operator in allow-list; documented
- **D4 no derived fields**: response has only the 11 schema columns; no `duration_sec`/`age_sec`
- **D5 error truncated to 200**: verified via `test_error_truncated_to_200`
- **D6-D14 inherited**: covered by UAT-4 regression suite + BL7 unit tests + the property-based SQL-injection test in `test_query_helpers.py`

## Threat-model walk

- **TM-005 SQL injection**: MITIGATED inherited from UAT-4. All values via `?` placeholders; identifiers only from allow-list-validated literals. Both `build_where_clause` and `build_order_by_clause` defensively re-check field names against the BL8 allow-lists.
- **TM-012 log redaction**: MITIGATED. Endpoint emits:
  - `api.jobs.read_failed` (PoolError str message — no raw rows from BL4)
  - `api.jobs.payload_oversized` (job_id + size_bytes only — no payload content)
  - `api.jobs.payload_parse_failed` (job_id + error type only)
  Plus correlation_id auto-bound from BL5 middleware. No payload content reaches a log call.
- **TM-013 fingerprinting**: MITIGATED. Same 200/400/503 surface as BL7.

## Non-findings (cleared)

- **No SQL injection vector.** All values flow through SQLite `?` placeholders.
- **No timing oracle on auth.** Auth handled by BL5 middleware; reached only after auth passes.
- **No log-volume amplification on hot path.** Success path emits only the middleware's `api.request.received` + `api.request.completed`; router emits only on 503 / oversized / parse-failed paths.
- **No DoS via response size.** `limit ≤ 500` enforced; `payload` bounded at 64 KiB per row; `error` truncated to 200 chars.
- **payload schema-comment contract.** Schema explicitly says "NEVER contains credentials." UAT-4 size+parse defenses cap the blast radius if upstream code violates that.
- **`game_id` FK to deleted games.** Schema is `ON DELETE SET NULL`; orphaned jobs surface as `game_id: null` in the response — no security implication.

## Test coverage

37 tests in `tests/api/test_jobs_router.py` across 9 classes. Plus the `jobs_pool_seeded` conftest fixture (~50 jobs across all enum combinations, including 1 oversized + 1 malformed + 1 non-dict payload). Branch coverage on `routers/jobs.py` ≥95%.

## Verification artifacts

- `pytest -q`: 518 tests passing project-wide (was 481; +37)
- `ruff check`: clean
- `ruff format --check`: clean
- `mypy --strict src/orchestrator/api/routers/jobs.py src/orchestrator/api/main.py`: clean
- `semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/jobs.py`: 0 findings
- `gitleaks detect` (126 commits): no leaks found

## Conclusion

**APPROVED for merge.** Zero findings. BL8 is the cheap-propagation proof point: composing UAT-4-hardened helpers + a per-endpoint allow-list produces a secure-by-default new endpoint with no shared-module changes. The conventions established in BL7+UAT-4 transferred cleanly to the second paginated F9 endpoint.
