# Security Audit — UAT-4 Remediation

**Cycle:** UAT-4 (BL6 + BL7 + `_query_helpers.py` regression coverage)
**Audit date:** 2026-05-20
**Auditor:** 5 parallel agents (sast-middleware, input-validation, sql-injection, threat-model, perf-correctness) + manual H-1 session + self-review
**Phase:** 2 (Construction), UAT cycle

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-BL7 UAT cycle covering:
- `src/orchestrator/api/_query_helpers.py` (the load-bearing parser/validator/SQL builder for ALL paginated F9 endpoints)
- `src/orchestrator/api/routers/games.py` (BL7's consumer of the helpers)
- `src/orchestrator/api/routers/platforms.py` (BL6 regression)
- BL5 middleware substrate (UAT-3 regression)

## Methodology

1. **5 parallel audit agents** writing structured reports to `tests/uat/sessions/2026-05-20-session-4/agent-results/`
2. **Manual H-1 session** at `tests/uat/sessions/2026-05-20-session-4/submissions/test-session-4-v1.md` — empirically confirmed all 3 candidate SEV-2 bugs live before fix
3. **Automated SAST**: `ruff check`, `ruff format --check`, `mypy --strict`, `semgrep --config p/owasp-top-ten --error`, `gitleaks detect`
4. **Property test**: existing Hypothesis SQL-injection test exercised against the revised builder

## Findings & resolution

**Initial counts:** 0 SEV-1, 4 SEV-2, 10 SEV-3, 7 SEV-4.

**Resolved in this cycle:** 4 SEV-2 + 8 SEV-3 (12 of 14 substantive findings).
**Deferred:** 2 SEV-3 + 7 SEV-4.

### SEV-2 (all resolved)

| ID | Title | Fix |
|---|---|---|
| **S2-A** | `applied_filters` echo wire format drift | `routers/games.py` builds `applied_filters` as plain `dict[str, dict[str, Any]]` from the parsed filters; `GamesMeta` type updated. `FilterCriterion` Pydantic model retained only for OpenAPI schema documentation. |
| **S2-B** | `?sort=,,,` silently dropped default | `_query_helpers.parse_sort`: if user_sort is empty after stripping all entries, the `default` applies — not just when `raw` is empty. |
| **S2-C** | `_in` cardinality unbounded | `_query_helpers.parse_filters`: `MAX_IN_VALUES = 100` cap with `QueryParamError → 400`. |
| **S2-D** | Oversized int → 500 instead of 400 | `_query_helpers._coerce_value`: signed 64-bit range check (`INT64_MIN` / `INT64_MAX`) on int values; also added to `parse_pagination` for offset. |

### SEV-3 (8 of 10 resolved)

| ID | Title | Fix |
|---|---|---|
| **S3-a** | String values had no content validators (XSS-in-echo risk) | Introduced `FilterFieldSpec.value_type = "timestamp"` typed-string variant with ISO 8601 regex + `datetime.fromisoformat` strict-parse validator. Applied to `last_prefilled_at` + `last_validated_at` on `/games`. |
| **S3-b** | `build_order_by_clause` missing defensive re-check | New `allow_list` parameter; raises `QueryParamError` on unknown field. Symmetric with `build_where_clause`. |
| **S3-c** | `build_where_clause` KeyError → 500 on unknown op | Explicit `if op in _OP_SQL` with `QueryParamError → 400` else-branch. |
| **S3-d** | `RecursionError` on deep JSON uncaught | `routers/games.py` metadata parse: added `RecursionError` to `except` tuple. |
| **S3-e** | `metadata` size unbounded before json.loads | `_MAX_METADATA_BYTES = 65536` (64 KiB) short-circuit; emits structured `api.games.metadata_oversized` log + returns null. |
| **S3-h** | `FilterAllowList` no identifier check | `__init__` validates every field name against `^[a-z_][a-z0-9_]*$`. |
| **S3-i** | `SortAllowList` no identifier check | Same `_validate_identifier` applied to `SortAllowList.__init__`. |
| **S3-j** | Reserved param namespace collision | `_validate_identifier` rejects `limit`/`offset`/`sort` as field names. |

### Deferred

- **S3-f** (spec §4.3 index claim wrong) — doc-only spec correction; next revision
- **S3-g** (COUNT/SELECT race) — documented as expected behavior for single-orchestrator deployment
- **All 7 SEV-4 items** — Phase 3 polish batch

## Verification

- **Regression tests**: 24 new tests in `tests/api/test_uat4_remediation.py`, all green
- **Full project suite**: 481 tests passing (was 457; +24)
- **ruff check + ruff format --check**: clean
- **mypy --strict**: clean across all api/ source files
- **semgrep `p/owasp-top-ten`**: 0 findings
- **gitleaks** (123 commits): no leaks

## Non-findings (cleared)

- **No SQL injection vector.** All values flow through SQLite `?` placeholders; field names interpolated only from hardcoded allow-list literals after identifier validation.
- **UAT-3 substrate regressions all hold**: CORS outermost, correlation_id regeneration, non-loopback bind warning, loopback-only schema/UI gates.
- **BL6 conventions intact**: `config` excluded, steam-first sort, bearer required.

## New threat candidates documented

- **TM-024** (schema enumeration via 400 messages): mitigated structurally by allow-list
- **TM-025** (applied_filters echo XSS amplifier): orchestrator-side mitigated by S3-a; XSS in Game_shelf is downstream integration concern
- **TM-026** (aggregate oracle via `meta.total`): bearer-required; accepted given current trust model
- **TM-027** (large-`_in` DoS): mitigated by S2-C
- **TM-028** (metadata JSON depth/size DoS): mitigated by S3-d + S3-e

## Conclusion

**APPROVED for merge.** Hardens `_query_helpers.py` against the load-bearing-shared-module risk: every future paginated F9 endpoint inherits these fixes for free.
