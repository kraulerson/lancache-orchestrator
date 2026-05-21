# UAT-5 Consolidated Findings (2026-05-20)

Three parallel agents + manual session. Dedup'd; severity is the higher of competing claims.

## Fix Now (this UAT remediation PR)

| ID | Source | Sev | Description | Effort |
|---|---|---|---|---|
| **U5-1** | agent-1 S2-A | SEV-2 | Bearer auth `errors="ignore"` silently strips non-ASCII bytes from token; no upper-bound length cap → latent timing/equality surface | ~15 LoC + 3 tests |
| **U5-2** | agent-1 S2-B | SEV-2 | Pydantic `Literal[...]` columns crash to 500 on out-of-literal DB value (games.platform/status, jobs.kind/platform/state/source) | ~25 LoC + 2 tests |
| **U5-3** | agent-1 S2-C | SEV-2 | `len(raw_meta) > _MAX_METADATA_BYTES` outside try/except; non-buffer pool return → unhandled TypeError | ~10 LoC + 1 test |
| **U5-4** | agent-2 BUG-1 | SEV-2 | `_coerce_value` accepts `NaN`/`Infinity` strings; passes to JSON serializer → 500 | ~3 LoC + 2 tests |
| **U5-5** | agent-3 F1 + manual M1 | SEV-2 | `/api/v1/platforms?password=foo` returns 200 (others 400); platforms has no allow-list validation | ~10 LoC + 1 test |
| **U5-6** | agent-3 F2 + manual M2 | SEV-2 | `/api/v1/platforms` envelope is `{platforms}` only; other 3 endpoints have `{entity, meta}` | ~20 LoC + 1 test |
| **U5-7** | manual M3 | SEV-3 | OPTIONS preflight returns 400 instead of 200/204; needs middleware investigation | ~5-20 LoC + 1 test, depends on root cause |
| **U5-8** | agent-2 SMELL-3 | SEV-3 | `?include=foo` silently ignored on `/games` and `/jobs` (no IncludeAllowList); convention enforcement drift | ~10 LoC + 2 tests |

## Defer — file as `triage:fold-in-bl` (next relevant BL)

| ID | Source | Sev | Description |
|---|---|---|---|
| U5-D1 | agent-2 SMELL-1 | SEV-3 | `sort=id:asc,id:desc` accepted; user-supplied sort needs dedup |
| U5-D2 | agent-3 F4 | SEV-3 | `SortFieldResponse` duplicated in 3 routers (extract to shared) |
| U5-D3 | agent-3 F5 | SEV-3 | Dead `FilterCriterion` model in games.py (comment claims it surfaces in OpenAPI but doesn't) |
| U5-D4 | agent-3 F6 | SEV-3 | 3 different constant names for the 200-char error truncation |
| U5-D5 | agent-1 S3-A | SEV-3 | `offset > total` correctness invariant unprotected (no test) |
| U5-D6 | agent-1 S3-E | SEV-3 | Inverted range `?gte > lte` silently returns empty (contract undocumented) |
| U5-D7 | agent-1 S3-G | SEV-3 | Multi-field user sort + tie-breaker field overlap untested |
| U5-D8 | agent-1 S3-I | SEV-3 | `?platform=steam&platform=epic` first-wins silently (duplicate query keys) |

## Defer — file as `triage:phase-3`

| ID | Source | Sev | Description |
|---|---|---|---|
| U5-P1 | agent-1 S3-B-N (assorted) | SEV-3 | ~14 hardening test gaps; cluster into a Phase 3 test-hardening sweep |
| U5-P2 | agent-1 S4-A-L | SEV-4 | ~12 minor robustness/cosmetic |
| U5-P3 | agent-3 F7, F8 | SEV-4 | docstring count drift, truncated comment |
| U5-P4 | agent-2 SMELL-2 | SEV-4 | Contradictory filters return empty silently |
| U5-P5 | agent-2 SMELL-4 | SEV-4 | int() leniency on `+1`, `1_000` |
| U5-P6 | agent-2 SMELL-5 | SEV-4 | `/health` returns 503 + `status:ok` (will fix when validators land in F-series) |
| U5-P7 | agent-3 F3 | SEV-2-docs | `applied_filters` echo convention undocumented (add to spec; not a code change) |

## Verified clean (no action)

- All UAT-4 regressions (S2-A through S3-a) still hardened
- Auth: empty/wrong/oversized/case-insensitive Bearer all → 401
- OQ2 loopback enforcement (`X-Forwarded-For` spoof rejected)
- Body cap (32 KB default) on GET → 413
- Correlation ID server-regeneration (UAT-3 OQ4)
- Identifier validation, INT64 cap, _in cap of 100, timestamp strict-parse
- Middleware order: CORS outermost (UAT-3 ADR-0012 D5)
- All `extra="forbid"` on 18 response models
- Allow-list consistency across endpoints, OpenAPI schema integrity
- Fixture scoping, FK paths, log namespacing, no sensitive-data leakage

## Counters

| | Count |
|---|---|
| New SEV-2 found | 6 |
| New SEV-3 found | ~10 (after dedup) |
| SEV-2 to Fix Now | 6 |
| SEV-3 to Fix Now | 2 (U5-7 OPTIONS + U5-8 ?include= defense) |
| Total Fix Now | 8 items |
| Total to defer | ~25 (12 fold-in-bl + 13 phase-3) |
