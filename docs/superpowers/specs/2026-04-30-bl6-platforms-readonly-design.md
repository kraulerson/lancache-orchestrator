# BL6-F9 — `GET /api/v1/platforms` (read-only) — Design Spec

**Date:** 2026-04-30
**Phase:** 2 (Construction), Milestone B, Build Loop 6
**Feature:** F9 partial — first read-only `/api/v1/*` endpoint
**Branch:** `feat/bl6-platforms-readonly`
**Depends on:** BL3 (Settings), BL4 (DB pool), BL5 (FastAPI skeleton + UAT-3 remediation)
**Unblocks:** F1/F2 (Steam/Epic auth) — operator can verify auth state via this read endpoint after running `POST /api/v1/platforms/{name}/auth`

---

## 1. Goal

Expose the BL5 FastAPI skeleton's first real domain endpoint: a read-only listing of every configured platform's auth + sync status. Game_shelf UI consumes this to render the platform-status panel; operator CLI can poll it for diagnostics.

This endpoint also locks the API conventions every future F9 read endpoint (games, jobs, manifests, stats, block_list) will inherit:

- Wrapped envelope shape: `{"<resource>": [...], (optionally) "meta": {...}}`
- Pydantic response model with `extra="forbid"`
- `Depends(get_pool_dep)` for DB access (Bible §10.5)
- `JSONResponse(content=body.model_dump())` return idiom (per BL5 health)
- 503 with `{"detail": "database unavailable"}` on `PoolError` (consistent with `/health`)
- Bearer-required (NOT in `AUTH_EXEMPT_PATHS`)

---

## 2. Locked decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D1** | `config` field exposure | **Excluded entirely** | Least-blast-radius. Config will hold credentials when F1/F2 land; no operator workflow currently needs config readback through the read endpoint. Add a separate `GET /api/v1/platforms/{name}/diagnostics` later if a real need emerges. |
| **D2** | Response envelope | **Wrapped: `{"platforms": [...]}`** | Locks the convention for every future F9 endpoint. Same shape generalizes to `{"games": [...], "meta": {...}}` for paginated lists later. Resource-name key beats generic `"data"` for grep-during-debug. |
| **D3** | `last_error` field | **Truncate to 200 chars at API layer** | Defense-in-depth on top of upstream redaction. Caps any leak that slips through F1/F2/F3 sync-error scrubbing. 200 chars is enough for operator triage, not enough for a typical credential blob. Full string preserved in structured logs only. |
| **D4** | Sort order | **Steam first, then alphabetical** | `ORDER BY CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name`. Operator preference; pin Steam at the top of the Game_shelf platform panel. CASE expression keeps order stable if a future migration adds a third platform. |
| **D5** | ETag / caching | **None for v1 (YAGNI)** | Two rows × ~200 bytes each ≈ 400-byte response; no realistic poll rate is bandwidth-constrained. Add ETag in a follow-up only if Game_shelf surfaces a real need. |
| **D6** | Pool-error semantics | **503 with structured body** | Consistent with `/api/v1/health` pattern. Catches `PoolError`/`PoolNotInitializedError`/`QueryError`. Default Starlette 500 would be a less informative wire signal. |
| **D7** | Auth | **Bearer required (NOT exempt)** | Platform auth/sync status is operator-only data. Path is added to no exempt list; BL5's `BearerAuthMiddleware` enforces. |
| **D8** | Response model strictness | **`extra="forbid"`** | Future migration adding a column without updating the model is caught immediately by the test suite (mismatch raises during model validation). |

---

## 3. Architecture

### 3.1 File layout

```
src/orchestrator/api/routers/platforms.py    NEW — ~80 LoC
src/orchestrator/api/main.py                 +1 line (app.include_router)
tests/api/test_platforms_router.py           NEW — ~250 LoC, ~15 tests
docs/ADR documentation/...                   no new ADR; this spec + CHANGELOG suffice
```

No schema migration required — table already exists from `0001_initial.sql`.

### 3.2 Module: `src/orchestrator/api/routers/platforms.py`

```python
"""GET /api/v1/platforms — list platform auth/sync status (BL6 / Feature 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


_LAST_ERROR_TRUNCATE = 200
_log = structlog.get_logger(__name__)


class PlatformResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["steam", "epic"]
    auth_status: Literal["ok", "expired", "error", "never"]
    auth_method: Literal["steam_cm", "epic_oauth"]
    auth_expires_at: str | None
    last_sync_at: str | None
    last_error: str | None


class PlatformListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[PlatformResponse]


router = APIRouter(prefix="/api/v1", tags=["platforms"])


@router.get(
    "/platforms",
    response_model=PlatformListResponse,
    responses={
        200: {"description": "List of all configured platforms"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy", "model": dict},
    },
    summary="List all platforms",
    description=(
        "Returns the auth and sync status of every configured platform. "
        "Always returns exactly two rows (steam, epic). Steam is pinned "
        "first in the response order. The `config` field is intentionally "
        "not exposed via this endpoint."
    ),
)
async def list_platforms(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        rows = await pool.read_all(
            "SELECT name, auth_status, auth_method, auth_expires_at, "
            "last_sync_at, last_error FROM platforms "
            "ORDER BY CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name"
        )
    except PoolError as e:
        _log.error("api.platforms.read_failed", reason=str(e))
        return JSONResponse(
            content={"detail": "database unavailable"},
            status_code=503,
        )

    items = [
        PlatformResponse(
            name=row["name"],
            auth_status=row["auth_status"],
            auth_method=row["auth_method"],
            auth_expires_at=row["auth_expires_at"],
            last_sync_at=row["last_sync_at"],
            last_error=(
                row["last_error"][:_LAST_ERROR_TRUNCATE]
                if row["last_error"] else None
            ),
        )
        for row in rows
    ]
    body = PlatformListResponse(platforms=items)
    return JSONResponse(content=body.model_dump())
```

### 3.3 Wire format

```json
{
  "platforms": [
    {
      "name": "steam",
      "auth_status": "never",
      "auth_method": "steam_cm",
      "auth_expires_at": null,
      "last_sync_at": null,
      "last_error": null
    },
    {
      "name": "epic",
      "auth_status": "never",
      "auth_method": "epic_oauth",
      "auth_expires_at": null,
      "last_sync_at": null,
      "last_error": null
    }
  ]
}
```

Six fields per platform. `config` excluded. `last_error` truncated to 200 chars.

### 3.4 Wiring in `main.py`

```python
from orchestrator.api.routers.platforms import router as platforms_router
# ...
app.include_router(health_router)
app.include_router(platforms_router)  # BL6
```

---

## 4. Test plan

Target: ≥95% branch coverage on `routers/platforms.py`. ~15 tests in `tests/api/test_platforms_router.py`.

### 4.1 Test classes

| Class | Tests | Coverage |
|---|---|---|
| `TestPlatformsHappyPath` | seeded steam+epic returned; correct order (steam first); 6-field response; envelope shape; 200 status | Happy path |
| `TestPlatformsAuth` | unauth → 401; wrong token → 401; valid token → 200 | Auth integration |
| `TestPlatformsLastErrorTruncation` | 199-char unchanged; 200-char unchanged; 201-char truncated; 5000-char truncated; null → null | D3 |
| `TestPlatformsConfigExclusion` | `config` field never in response even when DB row has non-null config; sensitive content in DB doesn't leak | D1 |
| `TestPlatformsResponseSchema` | `extra="forbid"` rejects unknown fields; Pydantic Literal narrowing on auth_status/auth_method | D8 |
| `TestPlatformsPoolFailure` | `PoolError` → 503 + `{"detail": "database unavailable"}`; structured `api.platforms.read_failed` log with correlation_id propagated | D6 |
| `TestPlatformsOrdering` | steam at index 0, epic at index 1; verify CASE clause via direct query inspection | D4 |
| `TestPlatformsSortStability` | hand-write rows in alpha order then non-alpha order → steam still first | D4 stability |

### 4.2 Test fixtures

Reuse `tests/api/conftest.py` fixtures:
- `unit_app` — fast-path app with `dependency_overrides[get_pool_dep] = populated_pool`
- `client` — `httpx.AsyncClient` against `unit_app`
- `populated_pool` — fresh DB with seeded `steam`/`epic` rows

For tests that need to mutate platform rows (e.g., `last_error` truncation), use `populated_pool.write_transaction()` to UPDATE before the GET.

### 4.3 Out of scope for BL6

- ETag / Last-Modified handling (D5, deferred)
- Concurrent-read stress (covered by BL4 pool tests)
- CORS allowed-origin mechanics (covered by BL5 + UAT-3 tests)
- The `POST /api/v1/platforms/{name}/auth` handler (F1/F2 — separate BL)

---

## 5. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Future migration adds a column to `platforms` and the response model isn't updated | Medium | `extra="forbid"` on `PlatformResponse` causes Pydantic to raise during construction; CI tests catch it |
| Future F1/F2 writes credentials into `last_error` (e.g., raw exception with refresh token in URL) | Medium | D3 200-char truncation caps blast; ID3's `_redact_sensitive_values` redacts at log-write time. Defense-in-depth, not single-layer. |
| `config` field accidentally added back to the response model | Low | `TestPlatformsConfigExclusion` test asserts `config` not in response keys; `extra="forbid"` blocks accidental construction |
| Pool returns a row with a name not in the Literal type | Low | Schema CHECK constrains to ('steam', 'epic'); migration to add a third platform requires explicit Literal expansion |
| Pool error during test causes test flakiness | Low | All tests use `populated_pool` fixture which is per-test-deterministic; no shared mutable pool state |
| Steam-first ordering is unstable if the rows are written in different order at migration time | Low | Explicit `ORDER BY CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name` is deterministic regardless of insert order |

---

## 6. Documentation deltas

- **CHANGELOG.md:** add to `[Unreleased]` → `### Added` (under BL6 heading)
- **README.md:** if a "What endpoints are available?" section exists, add `/api/v1/platforms` to the list
- **FEATURES.md:** add F9-platforms-readonly entry per existing convention
- **ADR:** none — this spec + the CHANGELOG entry constitute the design record. ADR is reserved for cross-cutting architectural decisions (BL5 was; this isn't).

---

## 7. Cross-references

- **Data model contract:** `docs/phase-1/data-model.md` (platforms table schema + invariants)
- **DB pool API:** ADR-0011 (`read_all` returns list of `aiosqlite.Row` objects with `.keys()` access)
- **API substrate:** ADR-0012 + UAT-3 addendum (middleware stack, AUTH_EXEMPT_PATHS, response idiom)
- **Threat model:** TM-001 (auth — handled by middleware), TM-012 (log redaction — `_log.error` uses ID3's redactor)
- **Bible:** §8.4 (health endpoint pattern this mirrors), §10.5 (no direct singleton imports)

---

## 8. Open follow-ups (deferred, not blocking)

- Diagnostics endpoint (`GET /api/v1/platforms/{name}/diagnostics`) — full `last_error`, recent sync attempts, structured error category. Lands when Game_shelf surfaces a real need.
- ETag support — when Game_shelf shows real polling load.
- Per-platform read endpoint (`GET /api/v1/platforms/{name}`) — currently no need; Game_shelf reads the whole list.
- `meta` envelope field — wire when first paginated endpoint (`/games`) lands; backfill `/platforms` to include `{"meta": {"count": 2}}` for client uniformity at that time.
