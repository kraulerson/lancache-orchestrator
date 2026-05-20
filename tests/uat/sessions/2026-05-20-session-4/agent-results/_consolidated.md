# UAT-4 Consolidated Findings & Triage Matrix

**Date:** 2026-05-20
**Branch:** `feat/uat-4-session`
**Scope:** BL6 (`/platforms`) + BL7 (`/games` + shared `_query_helpers.py`) on the BL5+UAT-3 substrate
**Agents:** sast-middleware, input-validation, sql-injection, threat-model, perf-correctness

## Tooling sweep (all clean)

| Tool | Result |
|---|---|
| ruff check | 0 |
| ruff format --check | 0 |
| mypy --strict | 0 |
| semgrep p/owasp-top-ten | 0/152 |
| gitleaks (123 commits) | 0 |

## Severity tally (cross-agent deduplicated)

- **SEV-1:** 0
- **SEV-2:** 4 unique
- **SEV-3:** 10 unique
- **SEV-4:** 7 unique

## SEV-2 unique findings (load-bearing for next BL)

| # | Title | Source agent(s) | Live? | Live exploit? |
|---|---|---|---|---|
| **S2-A** | `applied_filters` echo emits all 7 operator keys (6 null) per filtered field — contract drift vs spec §3.2 | perf-correctness | LIVE | API contract bug; locks the wrong convention for every future paginated endpoint |
| **S2-B** | `?sort=,,,` silently drops the default sort. Empty entries from comma-split skip validation; only the tie-breaker survives | input-validation | LIVE | Operator typo → unintended ordering with no error signal |
| **S2-C** | `_in=` operator has no cardinality cap. Bearer-authed attacker can amplify with 1000+ values → SQLite variable-limit error → log volume | sast-middleware + sql-injection | LIVE | DoS amplification (bearer required, so insider only) |
| **S2-D** | Oversized integers (`?size_bytes_gte=` + 25 nines) pass validation, bind to SQLite, raise OverflowError → 500 instead of 400 | input-validation | LIVE | Bug, but limited blast radius (500 with structured log) |

## SEV-3 unique findings

| # | Title | Source |
|---|---|---|
| S3-a | `_query_helpers._coerce_value` doesn't validate string-typed values; `last_prefilled_at_gte=<script>...` round-trips into echo verbatim | sast |
| S3-b | `build_order_by_clause` lacks defensive re-validation that `build_where_clause` has — caller-trust gap for the shared module | sast |
| S3-c | `build_where_clause` raises `KeyError`→500 instead of `QueryParamError`→400 on unknown ops | sast |
| S3-d | `metadata` JSON parse can hit `RecursionError` on deeply-nested input — uncaught → 500 | threat-model |
| S3-e | No size cap on `metadata` raw bytes before `json.loads` — 1 MB blob × limit=500 = real cost | threat-model + perf |
| S3-f | Spec §4.3's `idx_games_last_prefilled` claim is wrong — partial index isn't used for unqualified ORDER BY; planner does TEMP B-TREE SORT | perf-correctness |
| S3-g | COUNT(*) and SELECT use separate reader-connections + WAL snapshots → `total` can disagree with rows under concurrent writes | perf-correctness |
| S3-h | `FilterAllowList` accepts arbitrary keys — no identifier-regex check on construction. Defensive recheck in `build_where_clause` is cosmetic until this is fixed | sql-injection |
| S3-i | `SortField` dataclass has no validator on `field`. Future endpoint authors who hand-build SortFields can inject | sql-injection |
| S3-j | Reserved param namespace (`limit`/`offset`/`sort`) silently swallows colliding field names — future endpoint declaring a field with these names has unreachable filter | input-validation |

## SEV-4 (compact)

- Empty `_in` (`?status_in=`) produces silent empty-list filter (S4-a)
- Duplicate filter keys (`?status=a&status=b`) — last wins, no warning (S4-b)
- Trailing-comma `_in` produces empty-string value silently (S4-c)
- `applied_filters` + `total` is a binary-search aggregate oracle (currently dominated by direct list read; meaningful if a future read-aggregate-only token is introduced) (S4-d)
- Field literally named `foo_gte` is unaddressable as eq — operator-suffix-in-field-name silently locks naming policy (S4-e)
- OpenAPI schema generation correctness check pending — Pydantic `alias="in"` rendering verified ok, but worth empirical check (S4-f)
- Test coverage gaps: cross-combination tests (filter + sort + pagination + echo assertion), `applied_sort` order stability across runs (S4-g)

## New threat candidates (beyond TM-001..TM-023)

- **TM-024**: schema enumeration via 400 error messages (`?password=foo` vs `?platform=foo` differential) — verified by threat-model agent
- **TM-025**: applied_filters echo as XSS amplifier if downstream client (Game_shelf) renders unsafely (integration concern, not orchestrator-side)
- **TM-026**: aggregate oracle via `meta.total` × filter probes
- **TM-027**: large-`_in` DoS amplification (covered by S2-C fix)
- **TM-028**: metadata JSON depth/size DoS (covered by S3-d/S3-e fix)

## Most surprising findings

1. **`applied_filters` echo serializes wrong (S2-A).** Pydantic `FilterCriterion` model has all 7 op fields; with default values `None`, model_dump emits all of them. The spec example showed compact `{"eq": "steam"}` but the actual wire format is `{"eq": "steam", "in": null, "gte": null, ...}`. The test suite never asserted on the exact dict shape — only on individual keys — so this slipped through.

2. **`?sort=,,,` silently bypasses defaults (S2-B).** Because the input string is non-empty, `parse_sort` enters the user-sort branch instead of applying default; the comma-split produces empty entries; the strip+continue loop produces an empty list; then only the tie-breaker is appended. Default sort never runs.

3. **Spec §4.3 index claim is empirically wrong (S3-f).** `idx_games_last_prefilled` partial index is NOT used for `?sort=last_prefilled_at:desc` without a `WHERE last_prefilled_at IS NOT NULL` clause. The "Recently Prefilled" Game_shelf panel hits a full table scan.

4. **Most surprising non-finding: SQL injection.** Zero actual injection vectors. The defense-in-depth re-checks are cosmetic today (S3-h, S3-i) but the structural defense (hardcoded callers + parameterized binds) holds.

## Triage matrix (Orchestrator decision needed)

### SEV-2 — recommendation column

| ID | Title | Rec | Effort |
|---|---|---|---|
| S2-A | applied_filters wire-format drift | **Fix Now** — locks convention for every future endpoint; can't be backward-broken later | small (build applied_filters as plain dict; drop FilterCriterion from serialization but keep for OpenAPI) |
| S2-B | `?sort=,,,` silent default-bypass | **Fix Now** — correctness bug; one-line parser fix (treat empty stripped entries as error or fall through to default) | tiny |
| S2-C | `_in=` cardinality | **Fix Now** — caps blast radius; one-line in `_coerce_value`/`parse_filters` | tiny |
| S2-D | Oversized integer → 500 | **Fix Now** — range check at parse boundary; converts 500 → 400 | tiny |

### SEV-3 — recommendation column

| ID | Title | Rec |
|---|---|---|
| S3-a | String-value validators (timestamp ISO format etc.) | Fix Now — small, prevents XSS-via-echo class entirely |
| S3-b | `build_order_by_clause` defensive re-check | Fix Now — symmetry with `build_where_clause`; <10 lines |
| S3-c | `KeyError → 500` on unknown op in builder | Fix Now — symmetry with parser |
| S3-d | metadata RecursionError uncaught | Fix Now — add to except tuple |
| S3-e | metadata raw-bytes cap before json.loads | Fix Now — defense-in-depth before F5/F6 write hot paths |
| S3-f | spec §4.3 index claim wrong | Doc fix — update spec; defer index creation until profiling shows real cost |
| S3-g | COUNT/SELECT racing snapshots | Document as expected behavior in spec — single-user single-orchestrator drift is bounded |
| S3-h | `FilterAllowList` identifier regex check | Fix Now — makes the defensive recheck real, hardens future endpoints |
| S3-i | `SortField` field validator | Fix Now — same rationale as S3-h |
| S3-j | Reserved param namespace collision | Fix Now — `FilterAllowList.__init__` raises if a reserved name is in the field list |

### SEV-4 — defer to Phase 3 or fold into other BLs

All 7 SEV-4 items are polish/coverage; defer to a Phase 3 hardening pass.

## Per memory `feedback_default_to_most_capable.md`

Recommend **Fix Now on all 4 SEV-2 + 8 of 10 SEV-3** (deferring S3-f as doc-only and S3-g as documented-behavior). This is the most-capable option:
- API contract drift (S2-A) MUST be fixed before next paginated endpoint inherits
- Other SEV-3 items are mostly defense-in-depth on the shared `_query_helpers.py` — same rationale as fixing S2-A; future endpoints will inherit whatever we ship
- Total effort: ~6 hours of work; mostly small fixes with regression tests

Single combined remediation commit on `feat/uat-4-session` per UAT-3 pattern.

## File index

- `sast-middleware.md` (25 KB) — tooling + middleware regression + new threat candidates
- `threat-model.md` (29 KB) — TM-001..023 walk + 8 beyond-TM scenarios
- `input-validation.md` (28 KB) — 7 input vector matrices
- `sql-injection.md` (24 KB) — SQL surface deep-dive + property test proposals
- `perf-correctness.md` (28 KB) — index utilization, race window, OpenAPI correctness
