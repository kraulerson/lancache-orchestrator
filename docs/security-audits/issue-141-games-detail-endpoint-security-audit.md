# Security Audit — #141 GET /api/v1/games/{id} detail endpoint

**Feature:** issue-141-games-detail-endpoint (single-game read endpoint; the library was list-only)
**Modules:**
- `src/orchestrator/api/routers/games.py` — new `GET /api/v1/games/{game_id}`, `GameDetailResponse`, shared `_GAME_ROW_SELECT` projection + `_row_to_game_response` helper (extracted from the list loop, now reused by both)
**Audit date:** 2026-07-03
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy + full API suite (472)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-03 -->

## Scope

Adds a per-game detail read: `GET /api/v1/games/{game_id}` returning `{"game": {...}}` with the identical field set a list row carries (including `blocked` and the latest validation chunk counts). The row projection (`_GAME_ROW_SELECT`) and the row→model builder (`_row_to_game_response`) were factored out of the existing, already-tested list endpoint so the two paths cannot drift; the list endpoint now calls the same helper (behaviour-preserving — all pre-existing list tests still pass).

## Methodology

1. **SAST-lite.** `ruff check` (flake8-bandit `S`) — clean (S608 on the SQL constant reviewed, see below).
2. **Type safety.** `mypy src/orchestrator/api/routers/games.py` — clean.
3. **Tests.** Full `tests/api` suite green (472); 9 new detail tests (envelope, field-set parity, epic, 404, 400-non-int, 401, blocked, chunk counts, 503).
4. **Threat-model cross-check:** SQL injection (TM-005), authz, info-disclosure (TM-011), availability.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **No SQL injection (TM-005).** `game_id` is a FastAPI path parameter typed `int` — a non-integer never reaches the handler (the global `RequestValidationError` handler returns 400 first). The value flows to SQLite through a `?` placeholder (`pool.read_one(detail_sql, [game_id])`). The only interpolated fragment in `detail_sql` is the module constant `_GAME_ROW_SELECT`, itself built from the static `_GAMES_COLUMNS` literal — no request-derived string is ever concatenated into SQL text. The `# noqa: S608` on the constant is justified: the sole interpolation is a compile-time literal.
- **Authz unchanged.** The route sits under the same router/bearer middleware as the list endpoint; `test_no_token_returns_401` confirms an unauthenticated request is rejected before the handler runs.
- **No information disclosure (TM-011).** All error paths return fixed JSON bodies (`game not found` / `database unavailable` / `game record invalid`) — no exception text, stack trace, or SQL is echoed. A `PoolError` is caught and mapped to a clean 503; a malformed row is caught by `_row_to_game_response` (returns None) and mapped to a clean 500 with a structured server-side log, never leaking the offending value to the client.
- **Availability.** Single indexed primary-key lookup (`WHERE games.id = ?`), no pagination, no fan-out — strictly cheaper than the list endpoint. No new unbounded work.
- **No new data exposure.** The detail endpoint returns exactly the fields the list endpoint already returns for the same row; no previously-hidden column is surfaced.

## Decision

No findings. Ship.
