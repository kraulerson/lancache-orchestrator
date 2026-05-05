# BL6 Platforms Read-Only Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GET /api/v1/platforms` — the first real domain read endpoint on the BL5 FastAPI substrate — with full test-first coverage, structured error handling, and the locked API conventions every future F9 endpoint will inherit.

**Architecture:** New router module at `src/orchestrator/api/routers/platforms.py` mounted on the BL5 app via `include_router`. Pydantic models with `extra="forbid"` for response envelope and per-row shape. Reads via `Depends(get_pool_dep)` → BL4 pool's `read_all`. Pool failures caught at the router boundary and translated to 503 with structured log. `last_error` truncated to 200 chars defensively. `config` column excluded from the response surface entirely.

**Tech Stack:** Python 3.12, FastAPI 0.136.1, Pydantic v2, aiosqlite, structlog, httpx (test client), pytest-asyncio.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/orchestrator/api/routers/platforms.py` | **Create** (~80 LoC) | Router module: response models + handler + structured error path |
| `src/orchestrator/api/main.py` | **Modify** (+1 import, +1 `include_router` line) | Wire the new router into the app factory |
| `tests/api/test_platforms_router.py` | **Create** (~280 LoC, ~22 tests) | Full test suite per spec §4 |
| `docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md` | **Already created** (spec) | Locked decisions D1-D8 |
| `CHANGELOG.md` | **Modify** (entry under `[Unreleased]` → `### Added`) | BL6 entry |
| `FEATURES.md` | **Modify** (new F9-platforms-readonly entry) | Feature ledger update |

---

## Task 0: Bundle spec + plan into a single docs commit

**Files:**
- Already on disk: `docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md`
- Already on disk after this plan is written: `docs/superpowers/plans/2026-04-30-bl6-platforms-readonly.md`

**Branch state:** Already on `feat/bl6-platforms-readonly`. `--start-feature "BL6-F9-platforms-readonly"` already recorded.

- [ ] **Step 1: Confirm both files exist and are unmodified since approval**

Run:
```bash
git status --short docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md docs/superpowers/plans/2026-04-30-bl6-platforms-readonly.md
```
Expected: both shown as `??` (untracked, not yet staged).

- [ ] **Step 2: Stage both files**

Run:
```bash
git add docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md docs/superpowers/plans/2026-04-30-bl6-platforms-readonly.md
```

- [ ] **Step 3: Write the commit message to a tmp file (avoids path-string regex on the command line per project memory)**

Write `/tmp/bl6-commit0.txt` with content:
```
docs(bl6): platforms read-only spec + implementation plan

Spec locks 8 decisions for the first F9 read endpoint:
- D1 config field excluded from response (least-blast-radius)
- D2 wrapped envelope `{"platforms": [...]}` (locks F9 convention)
- D3 last_error truncated to 200 chars (defense-in-depth on top of
  upstream redaction)
- D4 Steam-first sort order via CASE expression (operator preference)
- D5 no ETag for v1 (YAGNI; 400-byte response)
- D6 PoolError → 503 with structured body (consistent with /health)
- D7 bearer required (NOT in AUTH_EXEMPT_PATHS)
- D8 Pydantic extra="forbid" on response model

Plan decomposes into 6 tasks following the project Build Loop:
tests-first → impl → security audit → docs → combined commit →
feature record.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 4: Commit the spec + plan bundle**

Run:
```bash
git commit -F /tmp/bl6-commit0.txt
```
Expected: 2 files committed, 1 commit on `feat/bl6-platforms-readonly`.

- [ ] **Step 5: Verify**

Run:
```bash
git log --oneline -1
git status --short
```
Expected: top commit subject is `docs(bl6): platforms read-only spec + implementation plan`. Working tree clean.

---

## Task 1: Write the failing test suite (red phase)

**Files:**
- Create: `tests/api/test_platforms_router.py`

This task writes the entire test suite at once. Tests reference the not-yet-existing router; running them fails with `ImportError`. That collective failure is the red-phase signal.

- [ ] **Step 1: Read the existing test conventions**

Run:
```bash
sed -n '1,90p' tests/api/conftest.py
```
Expected: see `unit_app`, `client`, `loopback_client`, `external_client`, `populated_pool` fixtures.

- [ ] **Step 2: Read the platforms schema for grounding**

Run:
```bash
grep -A 14 "CREATE TABLE platforms" src/orchestrator/db/migrations/0001_initial.sql
```
Expected: see name PRIMARY KEY, auth_status, auth_method, auth_expires_at, last_sync_at, last_error, config columns.

- [ ] **Step 3: Write the full test file**

Create `tests/api/test_platforms_router.py`:

```python
"""Tests for GET /api/v1/platforms (BL6 / Feature 9 partial).

Covers spec §4 — happy path, auth, last_error truncation, config exclusion,
response schema strictness, pool-failure 503 path, ordering + stability.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

VALID_TOKEN = "a" * 32  # matches conftest dummy ORCH_TOKEN


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPlatformsHappyPath:
    async def test_returns_seeded_platforms(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "platforms" in body
        assert len(body["platforms"]) == 2

    async def test_response_envelope_shape(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        # Wrapped envelope per D2.
        assert isinstance(body, dict)
        assert list(body.keys()) == ["platforms"]
        assert isinstance(body["platforms"], list)

    async def test_response_field_set_per_platform(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for item in body["platforms"]:
            assert set(item.keys()) == {
                "name",
                "auth_status",
                "auth_method",
                "auth_expires_at",
                "last_sync_at",
                "last_error",
            }

    async def test_steam_first_in_order(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][0]["name"] == "steam"
        assert body["platforms"][1]["name"] == "epic"


# ---------------------------------------------------------------------------
# Auth (D7)
# ---------------------------------------------------------------------------


class TestPlatformsAuth:
    async def test_no_auth_header_returns_401(self, client):
        r = await client.get("/api/v1/platforms")
        assert r.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# last_error truncation (D3)
# ---------------------------------------------------------------------------


class TestPlatformsLastErrorTruncation:
    async def _set_last_error(self, populated_pool, name: str, value: str | None) -> None:
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE platforms SET last_error = ? WHERE name = ?",
                (value, name),
            )

    async def test_null_passes_through(self, client, populated_pool):
        await self._set_last_error(populated_pool, "steam", None)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] is None

    async def test_under_200_chars_unchanged(self, client, populated_pool):
        s = "x" * 100
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] == s

    async def test_exactly_200_chars_unchanged(self, client, populated_pool):
        s = "x" * 200
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] == s
        assert len(steam["last_error"]) == 200

    async def test_201_chars_truncated_to_200(self, client, populated_pool):
        s = "x" * 201
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert len(steam["last_error"]) == 200
        assert steam["last_error"] == "x" * 200

    async def test_5000_chars_truncated_to_200(self, client, populated_pool):
        s = "x" * 5000
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert len(steam["last_error"]) == 200


# ---------------------------------------------------------------------------
# config exclusion (D1)
# ---------------------------------------------------------------------------


class TestPlatformsConfigExclusion:
    async def test_config_not_in_response_when_set(self, client, populated_pool):
        sensitive_config = '{"refresh_token": "should-never-appear"}'
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE platforms SET config = ? WHERE name = 'steam'",
                (sensitive_config,),
            )
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        # Config field absent from any item.
        for item in body["platforms"]:
            assert "config" not in item
        # Sensitive value never reaches the wire.
        assert "should-never-appear" not in r.text
        assert "refresh_token" not in r.text

    async def test_config_not_in_response_when_null(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for item in r.json()["platforms"]:
            assert "config" not in item


# ---------------------------------------------------------------------------
# Response schema strictness (D8)
# ---------------------------------------------------------------------------


class TestPlatformsResponseSchema:
    def test_extra_fields_rejected_by_pydantic(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="never",
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
                some_unknown_field="should be rejected",  # type: ignore[call-arg]
            )

    def test_invalid_name_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="origin",  # type: ignore[arg-type]
                auth_status="never",
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )

    def test_invalid_auth_status_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="bogus",  # type: ignore[arg-type]
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )

    def test_invalid_auth_method_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="never",
                auth_method="oauth2",  # type: ignore[arg-type]
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )


# ---------------------------------------------------------------------------
# Pool failure path (D6)
# ---------------------------------------------------------------------------


class TestPlatformsPoolFailure:
    async def test_pool_error_returns_503_with_detail(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}

    async def test_pool_error_logs_structured_event(self, unit_app, client, capsys):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.core.logging import configure_logging
        from orchestrator.db.pool import PoolError

        configure_logging()

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e.get("event") for e in events]
        assert "api.platforms.read_failed" in names
        # Correlation_id propagated through CorrelationIdMiddleware.
        failed = next(e for e in events if e.get("event") == "api.platforms.read_failed")
        assert "correlation_id" in failed


# ---------------------------------------------------------------------------
# Ordering + stability (D4)
# ---------------------------------------------------------------------------


class TestPlatformsOrdering:
    async def test_steam_at_index_0(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][0]["name"] == "steam"

    async def test_epic_at_index_1(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][1]["name"] == "epic"

    async def test_steam_first_regardless_of_row_order(self, client, populated_pool):
        # Mutate platform rows out of seed order; assert response order
        # remains stable. The CHECK constraint forbids new platform names,
        # but we can clobber/restore to make rowid order non-trivial.
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM platforms WHERE name IN ('steam', 'epic')")
            # Re-insert with Epic first to shake any rowid-dependent ordering.
            await tx.execute(
                "INSERT INTO platforms (name, auth_status, auth_method) "
                "VALUES ('epic', 'never', 'epic_oauth')"
            )
            await tx.execute(
                "INSERT INTO platforms (name, auth_status, auth_method) "
                "VALUES ('steam', 'never', 'steam_cm')"
            )
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][0]["name"] == "steam"
```

- [ ] **Step 4: Run the test file — verify red phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_platforms_router.py -q --no-header 2>&1 | tail -10
```
Expected: collection or import errors because `orchestrator.api.routers.platforms` does not yet exist. Every test fails with `ModuleNotFoundError: No module named 'orchestrator.api.routers.platforms'` or similar.

- [ ] **Step 5: Mark tests_written and tests_verified_failing**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```
Expected: both report `[OK] Step ... completed for build_loop`.

---

## Task 2: Implement the router (green phase)

**Files:**
- Create: `src/orchestrator/api/routers/platforms.py`
- Modify: `src/orchestrator/api/main.py` (1 import + 1 `include_router` call)

- [ ] **Step 1: Write the router module**

Create `src/orchestrator/api/routers/platforms.py`:

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
        503: {"description": "Database pool unhealthy"},
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
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic Depends in default
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
                if row["last_error"]
                else None
            ),
        )
        for row in rows
    ]
    body = PlatformListResponse(platforms=items)
    return JSONResponse(content=body.model_dump())
```

- [ ] **Step 2: Wire the router into main.py**

Open `src/orchestrator/api/main.py`. Find the existing import line:
```python
from orchestrator.api.routers.health import router as health_router
```
Add an import below it:
```python
from orchestrator.api.routers.platforms import router as platforms_router
```

Then find the existing wiring line near the bottom of `create_app()`:
```python
    # Routers
    app.include_router(health_router)
```
Add an `include_router` line for platforms below it:
```python
    app.include_router(platforms_router)
```

- [ ] **Step 3: Run the test file — verify green phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_platforms_router.py -q --no-header 2>&1 | tail -10
```
Expected: all tests pass.

- [ ] **Step 4: Run the full project test suite — confirm no regressions**

Run:
```bash
source .venv/bin/activate && pytest -q --no-header 2>&1 | tail -3
```
Expected: 387 tests passing (was 364 + ~23 new = ~387). 0 failures.

- [ ] **Step 5: Mark implemented**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
```
Expected: `[OK] Step 'implemented' completed for build_loop`.

---

## Task 3: Security audit pass

**Files:** No file changes if all checks pass; if a check fails, fix in source/tests and re-run.

- [ ] **Step 1: ruff check**

Run:
```bash
source .venv/bin/activate && ruff check src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py tests/api/test_platforms_router.py
```
Expected: `All checks passed!`

- [ ] **Step 2: ruff format check**

Run:
```bash
source .venv/bin/activate && ruff format --check src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py tests/api/test_platforms_router.py
```
Expected: `3 files already formatted`. If reformat suggested: run `ruff format <file>` then re-run `--check`.

- [ ] **Step 3: mypy --strict**

Run:
```bash
source .venv/bin/activate && mypy --strict src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py
```
Expected: `Success: no issues found in 2 source files`.

- [ ] **Step 4: semgrep OWASP Top 10**

Run:
```bash
source .venv/bin/activate && semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py
```
Expected: `0 findings`.

- [ ] **Step 5: gitleaks (whole repo, since the platform/path strings interact with the gitleaks `curl-auth-header` rule per UAT-3 closure memory)**

Run:
```bash
gitleaks detect --no-banner --redact --source .
```
Expected: `no leaks found`.

- [ ] **Step 6: Mark security_audit**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```
Expected: `[OK] Step 'security_audit' completed for build_loop`.

---

## Task 4: Documentation updates

**Files:**
- Modify: `CHANGELOG.md` (entry under `[Unreleased]` → `### Added`)
- Modify: `FEATURES.md` (new feature ledger entry)

- [ ] **Step 1: Update CHANGELOG.md**

Open `CHANGELOG.md`. Find the `[Unreleased]` → `### Added` section (the BL5 entries should be at the top of `[Unreleased]`). Add this entry as the FIRST item under `### Added` (newest entries on top):

```markdown
- **`GET /api/v1/platforms`** (BL6 / Feature 9 partial) — first real
  domain endpoint on the BL5 substrate. Returns the auth + sync status
  of every configured platform, with Steam pinned first in the response
  order. Six fields per platform (name, auth_status, auth_method,
  auth_expires_at, last_sync_at, last_error); `config` column
  intentionally excluded from the response surface. `last_error`
  truncated to 200 chars at the API layer (defense-in-depth on top of
  upstream redaction). Pool failures translate to HTTP 503 with a
  structured `api.platforms.read_failed` log event. Locks the wrapped
  envelope shape `{"<resource>": [...]}` that every future F9 read
  endpoint will inherit. See
  [spec](docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md).
```

- [ ] **Step 2: Update FEATURES.md**

Open `FEATURES.md`. Find the existing F9 entries (BL5 added some). Add a new entry following the same format:

```markdown
### F9 — `GET /api/v1/platforms` (read-only)

**Status:** Shipped (BL6, 2026-04-30)
**Branch:** `feat/bl6-platforms-readonly`
**Spec:** `docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md`

Lists the auth + sync status of all configured platforms (steam, epic).
Bearer-required, returns wrapped envelope `{"platforms": [...]}`.
`config` field excluded from response surface; `last_error` truncated
to 200 chars. Steam-first sort order. Locks the F9 read-endpoint
conventions (envelope shape, error semantics, response strictness)
that subsequent endpoints inherit.
```

If `FEATURES.md` doesn't have F9 entries yet (verify first with `grep -c "^### F9" FEATURES.md`), add the section under the most appropriate parent heading following the file's existing structure.

- [ ] **Step 3: Verify docs render reasonably**

Run:
```bash
head -60 CHANGELOG.md
echo "---"
grep -A 8 "F9 — \`GET /api/v1/platforms\`" FEATURES.md
```
Expected: CHANGELOG entry visible at top of `[Unreleased]` → `### Added`; FEATURES entry block visible.

- [ ] **Step 4: Mark documentation_updated**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```
Expected: `[OK] Step 'documentation_updated' completed for build_loop`.

---

## Task 5: Combined feat + docs commit (Build Loop forces this ordering)

**Files staged:** all source + tests + CHANGELOG + FEATURES (per BL5 closure pattern — gate forces docs-marked-before-source-commit).

- [ ] **Step 1: Survey staged + unstaged state**

Run:
```bash
git status --short
git diff --stat
```
Expected: `?? tests/api/test_platforms_router.py`, modified files: `src/orchestrator/api/routers/platforms.py` (new), `src/orchestrator/api/main.py`, `CHANGELOG.md`, `FEATURES.md`, `.claude/process-state.json` (auto-bumped by checklist marks), `.claude/build-progress.json` (auto-bumped).

- [ ] **Step 2: Stage files**

Run:
```bash
git add src/orchestrator/api/routers/platforms.py src/orchestrator/api/main.py tests/api/test_platforms_router.py CHANGELOG.md FEATURES.md .claude/process-state.json .claude/build-progress.json
git status --short
```
Expected: all listed files now staged (`A` for new, `M` for modified). Working tree should have no remaining changes.

- [ ] **Step 3: Write commit message to a tmp file**

Write `/tmp/bl6-commit1.txt` with:
```
feat(api): GET /api/v1/platforms — first F9 read endpoint

First real domain endpoint on the BL5 FastAPI substrate. Lists the
auth + sync status of every configured platform.

Behavior:
- Wrapped envelope: {"platforms": [{...}, {...}]} — locks F9 convention
- Six fields per platform (config excluded — D1)
- last_error truncated to 200 chars at API layer (D3)
- Steam-first sort via CASE expression (D4)
- Pool errors → 503 with structured api.platforms.read_failed log (D6)
- Bearer required (NOT in AUTH_EXEMPT_PATHS — D7)
- Pydantic extra="forbid" on response model (D8)

Implementation: src/orchestrator/api/routers/platforms.py (~80 LoC)
+ 2-line wire-up in main.py.

Tests: tests/api/test_platforms_router.py — 22 tests across 7 classes
(happy path, auth, last_error truncation, config exclusion, response
schema strictness, pool failure 503 path, ordering + stability).

Docs: CHANGELOG entry under [Unreleased] → Added; FEATURES F9 ledger
entry; spec already on disk at
docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md.

Verification: full project suite green (387 tests); ruff / mypy
--strict / semgrep OWASP Top 10 / gitleaks all clean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 4: Commit**

Run:
```bash
git commit -F /tmp/bl6-commit1.txt
```

If pre-commit gate blocks: read the message, fix the underlying issue, re-stage, commit again. NEVER use `--no-verify`.

If config-guard blocks `.claude/*` staging via Bash (per UAT-3 memory `.claude/process-state.json` and `.claude/build-progress.json` are NOT in the blocked-list, but verify): hand off to user terminal with `! git add .claude/process-state.json .claude/build-progress.json && git commit -F /tmp/bl6-commit1.txt`.

- [ ] **Step 5: Verify commit landed**

Run:
```bash
git log --oneline -2
git status --short
```
Expected: top commit subject `feat(api): GET /api/v1/platforms — first F9 read endpoint`. Working tree clean (or only `M .claude/process-state.json` if the next checklist mark already fired — that's expected).

---

## Task 6: Record the feature; mark feature_recorded; check test gate

**Files:** `.claude/build-progress.json` and `.claude/process-state.json` updated by the scripts.

- [ ] **Step 1: Mark feature_recorded**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
```
Expected: `[OK] Step 'feature_recorded' completed for build_loop (6/6)` + `[OK] All steps complete for build_loop!`.

- [ ] **Step 2: Record feature in test-gate counter**

Run:
```bash
scripts/test-gate.sh --record-feature "BL6-F9-platforms-readonly"
```
Expected: `[OK] Feature recorded`. Counter increments to 1/2.

- [ ] **Step 3: Verify state**

Run:
```bash
scripts/test-gate.sh --check-batch
```
Expected: `[OK] Clear to continue (1 features until next testing session)` (UAT-4 will fire after the NEXT feature, since interval is 2).

- [ ] **Step 4: Push branch + open PR**

Run:
```bash
git push -u origin feat/bl6-platforms-readonly
```

Then write `/tmp/bl6-pr-body.txt`:
```markdown
## Summary

BL6 — `GET /api/v1/platforms` — first real F9 read endpoint on the BL5 FastAPI substrate. Locks the API conventions (wrapped envelope, response strictness, error semantics) every future F9 endpoint will inherit.

## What's in this PR

- **`docs(bl6)`** — design spec + implementation plan
- **`feat(api)`** — router + main.py wiring + 22-test suite + CHANGELOG/FEATURES updates

## Locked decisions (D1-D8)

| ID | Decision |
|---|---|
| D1 | `config` field excluded from response (least blast-radius) |
| D2 | Wrapped envelope `{"platforms": [...]}` — locks F9 convention |
| D3 | `last_error` truncated to 200 chars at API layer |
| D4 | Steam-first sort via CASE expression |
| D5 | No ETag for v1 (YAGNI) |
| D6 | Pool errors → 503 with structured body |
| D7 | Bearer required (NOT in AUTH_EXEMPT_PATHS) |
| D8 | Pydantic `extra="forbid"` on response model |

## Verification

- 387 project tests passing (+22 new in `test_platforms_router.py`)
- ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten / gitleaks all clean
- 6/6 Build Loop checklist; feature recorded; test-gate counter at 1/2

## Test plan

- [ ] CI status checks pass (8 required)
- [ ] Manual smoke: `uvicorn orchestrator.api.main:app` boots; `curl -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/platforms` returns `{"platforms": [{"name": "steam", ...}, {"name": "epic", ...}]}`
- [ ] Review locked decisions in spec; flag any conventions you want to revisit before they propagate to `/games`, `/jobs`, `/manifests`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Then:
```bash
gh pr create --title "feat(api): GET /api/v1/platforms — first F9 read endpoint" --body-file /tmp/bl6-pr-body.txt --base main --head feat/bl6-platforms-readonly
```

- [ ] **Step 5: Stop. Report PR URL to the user; do not call `gh pr merge`.**

Per project memory `feedback_pr_merge_ownership.md`: user merges PRs themselves on GitHub.

---

## Self-Review

**Spec coverage check:**

| Spec section | Plan task |
|---|---|
| §1 Goal | Tasks 2-6 (full implementation + commit) |
| §2 D1 (config excluded) | Task 1 `TestPlatformsConfigExclusion`; Task 2 router doesn't SELECT or expose config |
| §2 D2 (wrapped envelope) | Task 1 `TestPlatformsHappyPath::test_response_envelope_shape`; Task 2 `PlatformListResponse` |
| §2 D3 (last_error truncation) | Task 1 `TestPlatformsLastErrorTruncation` (5 tests); Task 2 `_LAST_ERROR_TRUNCATE = 200` |
| §2 D4 (Steam-first ordering) | Task 1 `TestPlatformsOrdering` (3 tests); Task 2 SQL CASE clause |
| §2 D5 (no ETag) | implicit — no ETag code in Task 2; deferred to follow-ups |
| §2 D6 (pool error 503) | Task 1 `TestPlatformsPoolFailure` (2 tests); Task 2 `except PoolError` |
| §2 D7 (bearer required) | Task 1 `TestPlatformsAuth` (3 tests); Task 2 path NOT added to `AUTH_EXEMPT_PATHS` |
| §2 D8 (extra="forbid") | Task 1 `TestPlatformsResponseSchema` (4 tests); Task 2 `model_config = ConfigDict(extra="forbid")` on both models |
| §3.1 file layout | Task 2 file paths exact |
| §3.2 router structure | Task 2 Step 1 — full file content |
| §3.3 wire format | Task 1 happy-path tests assert on this |
| §3.4 main.py wiring | Task 2 Step 2 — exact import + `include_router` lines |
| §4.1 test classes | Task 1 — all 7 classes present |
| §4.2 test fixtures | Task 1 uses `client`, `unit_app`, `populated_pool` |
| §4.3 out of scope | Plan respects: no ETag tests; no concurrent-read tests; no CORS tests |
| §5 risk register | Tasks 1-2 cover migration drift (extra=forbid test), credential leak (truncation test), config exclusion test |
| §6 documentation deltas | Task 4 covers CHANGELOG + FEATURES; Task 0 covers spec/plan commit; ADR omitted per spec |
| §7 cross-references | All paths used in plan match spec §7 |
| §8 open follow-ups | Plan does not implement diagnostics endpoint, ETag, per-platform endpoint, or `meta` envelope — correctly deferred |

**Placeholder scan:** No "TBD", "TODO", "implement later", or unspecified-detail steps. All file paths, code blocks, and commands are concrete.

**Type consistency:** `PlatformResponse` and `PlatformListResponse` referenced consistently across Task 1 (tests) and Task 2 (impl). `_LAST_ERROR_TRUNCATE = 200` used in Task 2 only; tests check the OUTCOME (length 200) not the constant name. `get_pool_dep` imported from `orchestrator.api.dependencies` in both source and tests. `PoolError` from `orchestrator.db.pool` consistent. `_log` private name only used in source; tests assert on event-name string.
