# Security Audit — BL9 manifests read-only endpoint

**Feature:** BL9-F9-manifests-readonly (Build Loop 9, Milestone B)
**Module:** `src/orchestrator/api/routers/manifests.py` (~240 LoC) + `src/orchestrator/api/_query_helpers.py` (+30 LoC for IncludeAllowList + parse_includes) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-20
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-implementation security review of:
- `routers/manifests.py` — handler + Pydantic models + per-endpoint filter/sort/include allow-lists; opt-in game expansion via separate follow-up query
- `_query_helpers.py` — new `IncludeAllowList` dataclass + `parse_includes` function (~30 LoC); `"include"` added to `_RESERVED_PARAM_NAMES`
- 2-line wire-up in `main.py`
- New `manifests_pool_seeded` fixture in `tests/api/conftest.py`

The audit inherits the BL5+BL6+BL7+BL8+UAT-3+UAT-4 substrate. All existing security guarantees apply unchanged; this audit focuses on the new surface introduced by BL9 (the include primitive + the follow-up games lookup).

## Implementation deviation from spec D7

The spec proposed a conditional `LEFT JOIN games` for `?include=game`. During implementation the JOIN approach surfaced an ambiguous `id` column conflict (both `manifests` and `games` have an `id` column, and the `id:asc` tie-breaker emitted by the unqualified `build_order_by_clause` resolved ambiguously under the JOIN).

The implementation switched to a separate follow-up query strategy:
1. Page-1 query: `SELECT ... FROM manifests WHERE ... ORDER BY ... LIMIT ? OFFSET ?` (unchanged, identical convention to BL7/BL8).
2. If `?include=game` and rows are non-empty: gather distinct `game_id` values from the page, then `SELECT id, title, platform, app_id FROM games WHERE id IN (?,?,...)`.
3. Build a `{game_id: GameSummary}` dict, attach per-row in response build.

**Security/correctness impact:**
- Same security posture (allow-list-validated identifiers, parameterized values, `?` placeholders for the IN list).
- Better: the SQL builders source-of-truth stays conventional (unqualified identifiers) — no special-case "qualify ORDER BY when JOIN active" branch.
- Correctness: `manifests.game_id` is `NOT NULL FK -> games(id) ON DELETE CASCADE` per the schema, so every row's `game_id` resolves to exactly one games row. No null-handling edge case from the `LEFT JOIN`'s null-side either.
- Cost: one extra round-trip when `?include=game` is requested. Acceptable at expected scale (per-page IN list is bounded by `limit <= MAX_LIMIT=500`, so the games lookup is at most ~5-500 keys).

The user-visible wire behavior (D4 through D8) is identical to what the spec described.

## Methodology

1. **Automated SAST**: `semgrep --config p/owasp-top-ten --error` — 0 findings on both new/modified files
2. **gitleaks**: full repo scan (130 commits, 4.2 MB) — `no leaks found`
3. **ruff check + ruff format**: clean
4. **mypy --strict**: clean (3 source files)
5. **UAT-4 property-based SQL-injection test** in `test_query_helpers.py::TestSqlInjectionResistance` still passes — covers `build_where_clause`/`build_order_by_clause` that BL9 uses
6. **Manual review** of the new `parse_includes` + separate-query paths against TM-005 (SQL injection) and TM-013 (fingerprinting)

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings.

## Decisions D1-D8 walk

- **D1 raw BLOB excluded**: `_MANIFEST_COLUMNS` constant explicitly lists 6 manifest columns; `raw` not in the list. Schema additions would require code change to expose.
- **D2 default sort fetched_at:desc**: verified via `TestManifestsSort::test_default_fetched_at_desc` + `test_default_applied_sort_has_tie_breaker`
- **D3 version eq + _in**: verified via `TestManifestsFilters::test_version_eq` + `test_version_in`
- **D4 ?include=game always-present field, null when absent**: verified via `TestManifestsIncludeGame::test_no_include_game_is_null` + `test_include_game_populated`
- **D5 IncludeAllowList + parse_includes**: identifier validation at construction + reserved-name check; `?include` reserved in `_RESERVED_PARAM_NAMES`; tests in `TestParseIncludes`
- **D6 GameSummary shape (title, platform, app_id)**: verified via `TestManifestsIncludeGame::test_include_game_matches_seeded_game_row`
- **D7 game expansion implementation**: switched from LEFT JOIN to separate follow-up query (see "Implementation deviation" above). Allow-list scoped to manifests fields only; the games lookup uses fixed allow-listed identifiers (`id, title, platform, app_id`) and a `?` placeholder for each game_id in the IN list.
- **D8 applied_includes sorted echo**: verified via `TestManifestsAppliedEcho::test_applied_includes_*`

## Threat-model walk

- **TM-005 SQL injection**: MITIGATED. The new games-lookup SQL is hardcoded (`SELECT id, title, platform, app_id FROM games WHERE id IN (...)`); only `?` placeholders for game_ids from the prior page result (themselves either INTEGER PK values from the database or signed-64-bit-validated values that passed through `_coerce_value`). WHERE/ORDER BY for the manifests query continues to use the UAT-4-hardened builders.
- **TM-012 log redaction**: MITIGATED. The only new log event is `api.manifests.read_failed` with `reason=str(e)` from BL4's structured PoolError — no row data reaches a log call.
- **TM-013 fingerprinting**: MITIGATED. Three response shapes only: 200 with canonical envelope, 400 with `{detail}`, 503 with `{detail}`. `?include=game` adds a single `game` field per row; doesn't change response shape categories.

## Non-findings (cleared)

- **No SQL injection vector.** All values parameterized; identifiers from hardcoded literals only.
- **No timing oracle on auth.** Middleware-gated.
- **No log-volume amplification.** Success path emits only middleware events.
- **No DoS via response size.** `limit <= 500`; `raw` BLOB excluded; `game` summary is 3 small fields (~80 bytes/row max).
- **No identifier collision** in the manifests query — single table, unqualified identifiers match all existing endpoints.
- **No `?include=` injection.** Keys validated as identifiers + against per-endpoint allow-list at construction time AND request time.
- **No N+1 amplification.** Games lookup is a single `WHERE id IN (?,?,...)` query keyed by the distinct game_ids on the page (at most `limit` keys, but typically fewer due to multiple manifests per game).

## Test coverage

42 tests total: 34 router + 8 helper (parse_includes coverage).

## Verification artifacts

- `pytest -q`: 560 tests passing project-wide (518 prior + 34 router + 8 helper)
- `ruff check` + `ruff format --check`: clean
- `mypy --strict`: clean (3 source files)
- `semgrep --config p/owasp-top-ten --error`: 0 findings (152 rules, 2 files)
- `gitleaks detect`: no leaks (130 commits, 4.2 MB)

## Conclusion

**APPROVED for merge.** Zero findings. BL9 introduces the `?include=` opt-in expansion convention via a thin (~30 LoC) extension to `_query_helpers.py`. The new primitive follows the same identifier-validation + reserved-name discipline as the existing `FilterAllowList`/`SortAllowList`. Game expansion uses a separate follow-up lookup (cleaner than the originally-spec'd JOIN, no SQL-builder special cases). The convention is now available for future endpoints that need FK expansion (`/jobs?include=game`, etc.) without further helper changes.
