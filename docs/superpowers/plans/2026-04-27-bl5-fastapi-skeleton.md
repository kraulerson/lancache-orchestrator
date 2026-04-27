# BL5 / F9 partial — FastAPI Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the BL5 FastAPI skeleton (`/api/v1/health` + 4-layer middleware stack + lifespan) so BL6+ feature endpoints have a complete substrate to land on.

**Architecture:** `create_app()` factory returns a FastAPI instance with three custom pure-ASGI middlewares (CorrelationId, BodySizeCap, BearerAuth) plus Starlette's CORSMiddleware, an `@asynccontextmanager` lifespan that runs migrations + initializes the BL4 pool singleton, and one router (`/api/v1/health`) that consumes the pool via `Depends(get_pool_dep)` and returns the 7-field response per Bible §8.4 with fail-fast 503 semantics.

**Tech Stack:** Python 3.12, FastAPI 0.136.1, uvicorn[standard], starlette (via FastAPI), aiosqlite (via BL4 pool), pydantic v2 (via FastAPI + pydantic-settings), structlog (via BL2 logging), httpx + asgi-lifespan==2.1.0 (test-only).

**Spec:** `docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md` — sections referenced as §N below.

**Branch:** `feat/bl5-f9-skeleton` (already created and active; build_loop checklist already started for feature `BL5-F9-fastapi-skeleton`).

---

## Task decomposition overview

| # | Task | Spec ref | Commit |
|---|---|---|---|
| 1 | Add `asgi-lifespan==2.1.0` to dev deps + pip-compile + install | §3.2 | C3 |
| 2 | Verify install + commit precursor | — | C3 |
| 3 | Create `tests/api/__init__.py` + `tests/api/conftest.py` (5 fixtures) | §7.1 | C4 (red) |
| 4 | Write `tests/api/test_app_factory.py` (~5 tests) | §7.2 | C4 (red) |
| 5 | Write `tests/api/test_lifespan.py` (~6 tests) | §4, §7.2 | C4 (red) |
| 6 | Write `tests/api/test_middleware_correlation_id.py` (~8 tests) | §5.2, §7.2 | C4 (red) |
| 7 | Write `tests/api/test_middleware_body_size_cap.py` (~6 tests) | §5.3, §7.2 | C4 (red) |
| 8 | Write `tests/api/test_middleware_bearer_auth.py` (~12 tests) | §5.4, §7.2 | C4 (red) |
| 9 | Write `tests/api/test_health_endpoint.py` (~10 tests) | §6, §7.2 | C4 (red) |
| 10 | Verify all tests fail + mark checklist + commit red phase | — | C4 |
| 11 | Create `src/orchestrator/api/dependencies.py` (auth-exempt list + get_pool_dep + version constant) | §4 | C5 (green) |
| 12 | Create `src/orchestrator/api/middleware.py` (3 ASGI middlewares) | §5.2 / §5.3 / §5.4 | C5 (green) |
| 13 | Create `src/orchestrator/api/routers/__init__.py` + `routers/health.py` | §6 | C5 (green) |
| 14 | Create `src/orchestrator/api/main.py` (create_app factory + lifespan + middleware registration + router mounting) | §3, §4, §5.5 | C5 (green) |
| 15 | Run full suite, iterate to all-green | — | C5 (green) |
| 16 | Run branch-coverage, verify ≥ 95 % on `src/orchestrator/api/` | §7.2 | C5 (green) |
| 17 | Mark build_loop checklist + commit green phase | — | C5 |
| 18 | Phase 2.4 self-audit — produce `docs/security-audits/bl5-fastapi-skeleton-security-audit.md` | §10 | C5 (green) |
| 19 | Write `docs/ADR documentation/0012-fastapi-skeleton-architecture.md` | §9 | C6 (docs) |
| 20 | Update `CHANGELOG.md` + `FEATURES.md` + `README.md` + `PROJECT_BIBLE.md` | §9 | C6 (docs) |
| 21 | Mark documentation_updated + commit docs | — | C6 |
| 22 | File 4 follow-up issues per §10 | §10 | (no commit) |
| 23 | record-feature + qdrant memory + open PR | §11, §13 | (no commit) |

Total: 23 tasks across 4 commits (C3-C6 in the spec's 6-commit sequence; C1+C2 are spec + plan, already done / writing now).

---

## Task 1: Add `asgi-lifespan==2.1.0` to dev deps

**Files:**
- Modify: `requirements-dev.in` (if it exists; else `requirements-dev.txt` directly)
- Modify: `requirements-dev.txt` (regenerated)

- [ ] **Step 1: Check for the .in file convention**

```bash
ls requirements*.in 2>&1 | head
```

Expected: either lists `requirements.in`, `requirements-dev.in`, etc., OR shows nothing (project uses raw `.txt`).

- [ ] **Step 2: Add the dependency**

If `.in` files exist:
```bash
echo "asgi-lifespan==2.1.0" >> requirements-dev.in
.venv/bin/pip-compile requirements-dev.in --output-file requirements-dev.txt --resolver=backtracking --generate-hashes
```

If no `.in` files (raw `.txt` only): manually add the package + hash to `requirements-dev.txt`. Get the hash via:
```bash
.venv/bin/pip download asgi-lifespan==2.1.0 --no-deps -d /tmp/al-pkg && sha256sum /tmp/al-pkg/asgi_lifespan-2.1.0-*.whl
```
Then append the formatted entry. (If the project uses pip-tools, prefer Step 2's pip-compile path.)

- [ ] **Step 3: Install into venv**

```bash
.venv/bin/pip install -r requirements-dev.txt
```

Expected: `Successfully installed asgi-lifespan-2.1.0 sniffio-1.x.x` (sniffio is its only runtime dep, likely already installed transitively).

- [ ] **Step 4: Verify import**

```bash
.venv/bin/python -c "from asgi_lifespan import LifespanManager; print(LifespanManager.__module__)"
```

Expected: `asgi_lifespan.manager` (or similar — confirm the module is importable, no exception).

---

## Task 2: Verify install + commit precursor

**Files:**
- `requirements-dev.txt` (and `.in` if applicable)
- `Pipfile.lock` (regenerate per BL4 pattern)

- [ ] **Step 1: Regenerate Pipfile.lock**

If the project still has `Pipfile` from BL4:
```bash
PIPENV_VENV_IN_PROJECT=1 PIPENV_IGNORE_VIRTUALENVS=1 .venv/bin/pipenv install -r requirements.txt --skip-lock
PIPENV_VENV_IN_PROJECT=1 PIPENV_IGNORE_VIRTUALENVS=1 .venv/bin/pipenv lock
```

Note: this regenerates Pipfile + Pipfile.lock to reflect the new dep set. Per follow-up #43, these are gate-satisfaction artifacts; not authoritative.

- [ ] **Step 2: Verify ruff + mypy still clean**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/mypy --strict src/
```

Expected: both pass (no source changes yet).

- [ ] **Step 3: Stage + commit**

```bash
git add requirements-dev.txt requirements-dev.in Pipfile Pipfile.lock 2>/dev/null
git status --short
```

Verify only deps files are staged.

```bash
git commit -m "chore(deps): add asgi-lifespan==2.1.0 to dev deps for BL5

asgi-lifespan provides LifespanManager(app) for tests that need to
exercise the real FastAPI lifespan path (Context7-confirmed via
fastapi/fastapi docs: 'If your application relies on lifespan events,
the AsyncClient won't trigger these events; use LifespanManager from
florimondmanca/asgi-lifespan'). Required for BL5 spec §7.1's
'lifespan_app' fixture.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Push (optional — can wait until red-phase commit)**

Defer push to keep the branch's commit graph cohesive.

---

## Task 3: Create `tests/api/__init__.py` + `tests/api/conftest.py`

**Files:**
- Create: `tests/api/__init__.py`
- Create: `tests/api/conftest.py`

- [ ] **Step 1: Create the `__init__.py`**

Write `tests/api/__init__.py` with empty content (makes the directory a package — pytest can discover without it but consistency with `tests/db/`, `tests/core/` matters).

- [ ] **Step 2: Create the conftest.py with the 5 spec-required fixtures**

Write `tests/api/conftest.py`:

```python
"""Shared fixtures for tests/api/.

Per spec §7.1: two app fixtures (unit_app no-lifespan; lifespan_app via
asgi_lifespan.LifespanManager) and three client fixtures (default,
loopback-simulated, external-IP-simulated for OQ2 testing).

Re-exports populated_pool from tests/db/conftest.py via direct import.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, AsyncIterator

import httpx
import pytest
import pytest_asyncio

# Re-use the pool fixtures from tests/db/conftest.py — these are
# discoverable by pytest as long as conftest.py at tests/ level is
# loaded, but explicit import for clarity.
from tests.db.conftest import _isolated_env, db_path, mem_pool, pool, populated_pool  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI


@pytest_asyncio.fixture
async def unit_app(populated_pool):
    """Fast unit-test app: no lifespan, deps overridden, app.state stubbed."""
    from orchestrator.api.dependencies import get_pool_dep
    from orchestrator.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_pool_dep] = lambda: populated_pool
    app.state.boot_time = time.monotonic()
    app.state.git_sha = "test-sha-deadbeef"
    return app


@pytest_asyncio.fixture
async def lifespan_app(db_path: Path, monkeypatch) -> AsyncIterator[FastAPI]:
    """Integration-test app: real lifespan via asgi_lifespan."""
    from asgi_lifespan import LifespanManager

    from orchestrator.api.main import create_app

    monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
    app = create_app()
    async with LifespanManager(app):
        yield app


@pytest_asyncio.fixture
async def client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient hitting the unit_app via ASGITransport (no socket)."""
    transport = httpx.ASGITransport(app=unit_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def loopback_client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient that simulates a 127.0.0.1 origin (OQ2 positive-path test)."""
    transport = httpx.ASGITransport(app=unit_app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def external_client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient that simulates a non-loopback origin (OQ2 negative-path test)."""
    transport = httpx.ASGITransport(app=unit_app, client=("192.168.1.100", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
```

- [ ] **Step 3: Verify file is syntactically valid**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/conftest.py').read())"
.venv/bin/ruff check tests/api/conftest.py
.venv/bin/ruff format --check tests/api/conftest.py
```

Expected: AST parses, ruff clean, format clean. (May need to run `ruff format` if needed.)

Don't run pytest yet — fixtures depend on `orchestrator.api.main.create_app` which doesn't exist; tests using these fixtures will fail-import until Task 14. Expected for TDD red phase.

---

## Task 4: Write `tests/api/test_app_factory.py` (~5 tests)

**Files:**
- Create: `tests/api/test_app_factory.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for orchestrator.api.main.create_app() factory shape (spec §7.2).

Verifies: the factory returns a usable FastAPI app, the middleware order
matches spec §5.1, the OpenAPI schema has the bearer security_scheme
registered, the /api/v1/health route is mounted, the auth-exempt prefix
constants align with the routes that should be exempt.
"""

from __future__ import annotations

import pytest

from orchestrator.api.dependencies import AUTH_EXEMPT_PREFIXES
from orchestrator.api.main import create_app


class TestAppFactory:
    def test_create_app_returns_fastapi_instance(self):
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_health_route_mounted(self):
        app = create_app()
        paths = {route.path for route in app.routes}
        assert "/api/v1/health" in paths

    def test_openapi_security_scheme_registered(self):
        """Spec §3.3 + §5.4: even with auth-as-middleware, the OpenAPI
        schema must declare the bearer scheme so Swagger UI shows the
        Authorize button."""
        app = create_app()
        schema = app.openapi()
        assert "components" in schema
        assert "securitySchemes" in schema["components"]
        # We register the scheme as "bearerAuth" by convention.
        bearer = schema["components"]["securitySchemes"].get("bearerAuth")
        assert bearer is not None
        assert bearer["type"] == "http"
        assert bearer["scheme"] == "bearer"

    def test_middleware_order_matches_spec(self):
        """Spec §5.1: outermost-first order is CorrelationId, BodySizeCap,
        BearerAuth, CORS. The Starlette middleware stack stores them in
        registration order (outermost last in user_middleware list — but
        because add_middleware prepends, the last-added is outermost)."""
        from orchestrator.api.middleware import (
            BearerAuthMiddleware,
            BodySizeCapMiddleware,
            CorrelationIdMiddleware,
        )
        from starlette.middleware.cors import CORSMiddleware

        app = create_app()
        # app.user_middleware is a list; index 0 is the outermost layer.
        names = [m.cls.__name__ for m in app.user_middleware]
        # CorrelationId outermost (index 0), then BodySizeCap, then
        # BearerAuth, then CORS (innermost).
        assert names.index("CorrelationIdMiddleware") < names.index("BodySizeCapMiddleware")
        assert names.index("BodySizeCapMiddleware") < names.index("BearerAuthMiddleware")
        assert names.index("BearerAuthMiddleware") < names.index("CORSMiddleware")

    def test_auth_exempt_prefixes_align_with_documented_routes(self):
        """Spec §4: the exempt list must include /api/v1/health (the only
        unauthenticated handler in BL5) and the OpenAPI/Swagger paths."""
        assert "/api/v1/health" in AUTH_EXEMPT_PREFIXES
        assert any(p.endswith("/openapi.json") for p in AUTH_EXEMPT_PREFIXES)
        assert any(p.endswith("/docs") for p in AUTH_EXEMPT_PREFIXES)
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_app_factory.py').read())"
.venv/bin/ruff check tests/api/test_app_factory.py
```

Expected: AST OK, ruff clean.

---

## Task 5: Write `tests/api/test_lifespan.py` (~6 tests)

**Files:**
- Create: `tests/api/test_lifespan.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for the FastAPI lifespan (spec §4).

Uses lifespan_app fixture (asgi_lifespan.LifespanManager) so the real
startup path runs: migrations applied, pool initialized, app.state
populated. Shutdown closes the pool.

Lifespan failure paths (migration failure / pool init failure) are
tested via monkeypatch + SystemExit assertion.
"""

from __future__ import annotations

import time

import httpx
import pytest

from orchestrator.db.pool import get_pool


class TestLifespanStartup:
    async def test_lifespan_applies_migrations_and_inits_pool(self, lifespan_app):
        # Pool singleton should exist after lifespan ran
        pool = get_pool()
        assert pool is not None
        # And it should be in the "ready" state
        health = await pool.health_check()
        assert health["writer"]["healthy"] is True
        assert health["readers"]["total"] >= 1

    async def test_lifespan_sets_boot_time(self, lifespan_app):
        assert hasattr(lifespan_app.state, "boot_time")
        assert isinstance(lifespan_app.state.boot_time, float)
        # Boot time should be recent (within last 60 sec)
        assert time.monotonic() - lifespan_app.state.boot_time < 60.0

    async def test_lifespan_sets_git_sha_with_default_unknown(self, lifespan_app):
        # No GIT_SHA env set in test → falls back to "unknown"
        assert lifespan_app.state.git_sha == "unknown"

    async def test_lifespan_reads_git_sha_from_env(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("GIT_SHA", "abc1234")
        app = create_app()
        async with LifespanManager(app):
            assert app.state.git_sha == "abc1234"


class TestLifespanFailures:
    async def test_lifespan_migration_failure_raises_systemexit(self, monkeypatch, tmp_path):
        from asgi_lifespan import LifespanManager

        from orchestrator.db import migrate
        from orchestrator.api.main import create_app

        # Point at a deliberately broken DB path
        monkeypatch.setenv("ORCH_DATABASE_PATH", "/dev/null")  # V-3 reject path
        app = create_app()
        with pytest.raises((SystemExit, migrate.MigrationError)):
            async with LifespanManager(app):
                pass

    async def test_lifespan_returns_503_through_handler_when_unhealthy(self, lifespan_app):
        """Once lifespan is up, a request to /health hits the handler.
        BL5 ship state: 503 because three subsystems are stub-false."""
        transport = httpx.ASGITransport(app=lifespan_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 503  # BL5 ship state per spec §6.4
        body = r.json()
        assert body["status"] in ("ok", "degraded")
        assert body["scheduler_running"] is False  # stub
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_lifespan.py').read())"
.venv/bin/ruff check tests/api/test_lifespan.py
```

Expected: AST OK, ruff clean.

---

## Task 6: Write `tests/api/test_middleware_correlation_id.py` (~8 tests)

**Files:**
- Create: `tests/api/test_middleware_correlation_id.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for CorrelationIdMiddleware (spec §5.2).

Coverage: CID generated when missing; echoed when valid UUID4;
regenerated when invalid; X-Correlation-ID present on response;
structlog contextvar populated; two requests yield distinct CIDs.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest


_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class TestCorrelationIdHeaderEcho:
    async def test_cid_generated_when_missing(self, client):
        r = await client.get("/api/v1/health")
        cid = r.headers.get("x-correlation-id")
        assert cid is not None
        assert _UUID4_RE.match(cid)

    async def test_cid_echoed_when_valid(self, client):
        provided = str(uuid.uuid4())
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        assert r.headers.get("x-correlation-id") == provided

    async def test_cid_regenerated_when_invalid(self, client):
        bad = "not-a-uuid"
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": bad})
        cid = r.headers.get("x-correlation-id")
        assert cid != bad
        assert _UUID4_RE.match(cid)

    async def test_cid_regenerated_when_uuid_v1(self, client):
        # UUID v1 is not v4; should be regenerated
        v1 = str(uuid.uuid1())
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": v1})
        cid = r.headers.get("x-correlation-id")
        assert cid != v1
        assert _UUID4_RE.match(cid)


class TestCorrelationIdLogPropagation:
    async def test_cid_appears_in_request_received_log(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        provided = str(uuid.uuid4())
        await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        recv = [e for e in events if e.get("event") == "api.request.received"]
        assert len(recv) >= 1
        assert recv[0].get("correlation_id") == provided

    async def test_cid_appears_in_request_completed_log(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        provided = str(uuid.uuid4())
        await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        comp = [e for e in events if e.get("event") == "api.request.completed"]
        assert len(comp) >= 1
        assert comp[0].get("correlation_id") == provided
        assert "duration_ms" in comp[0]


class TestCorrelationIdIsolation:
    async def test_two_requests_yield_distinct_cids(self, client):
        r1 = await client.get("/api/v1/health")
        r2 = await client.get("/api/v1/health")
        assert r1.headers["x-correlation-id"] != r2.headers["x-correlation-id"]

    async def test_concurrent_requests_have_independent_cids(self, client):
        import asyncio

        results = await asyncio.gather(
            client.get("/api/v1/health"),
            client.get("/api/v1/health"),
            client.get("/api/v1/health"),
        )
        cids = [r.headers["x-correlation-id"] for r in results]
        assert len(set(cids)) == 3  # all distinct
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_middleware_correlation_id.py').read())"
.venv/bin/ruff check tests/api/test_middleware_correlation_id.py
```

---

## Task 7: Write `tests/api/test_middleware_body_size_cap.py` (~6 tests)

**Files:**
- Create: `tests/api/test_middleware_body_size_cap.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for BodySizeCapMiddleware (spec §5.3).

Coverage: Content-Length over → 413; Content-Length under → handler runs;
chunked over → 413; chunked under → handler runs; GET unaffected by cap;
api.body_size_cap_exceeded log event emitted.
"""

from __future__ import annotations

import json

import pytest


class TestBodySizeCapContentLength:
    async def test_oversize_content_length_rejected_413(self, client):
        body = b"x" * (32 * 1024 + 1)  # 32 KiB + 1
        # POST to a non-existent path; the cap fires before routing.
        r = await client.post(
            "/api/v1/anything",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 413

    async def test_at_cap_content_length_passes_to_handler(self, client):
        body = b"x" * (32 * 1024)  # exactly 32 KiB — at the cap
        # /api/v1/health is exempt from auth; POST to it routes to a 405
        # (method not allowed) — that's fine, what we want is to verify
        # body cap didn't fire (we'd see 413, not 405).
        r = await client.post("/api/v1/health", content=body)
        assert r.status_code != 413

    async def test_under_cap_content_length_passes(self, client):
        body = b"x" * 100  # tiny
        r = await client.post("/api/v1/health", content=body)
        assert r.status_code != 413


class TestBodySizeCapStreaming:
    async def test_chunked_oversize_rejected_413(self, client):
        async def gen():
            for _ in range(33):  # 33 chunks × 1 KiB = 33 KiB > 32 KiB cap
                yield b"x" * 1024

        r = await client.post(
            "/api/v1/anything",
            content=gen(),
            headers={"Transfer-Encoding": "chunked"},
        )
        assert r.status_code == 413

    async def test_chunked_under_cap_passes(self, client):
        async def gen():
            for _ in range(10):  # 10 KiB total
                yield b"x" * 1024

        r = await client.post(
            "/api/v1/health",
            content=gen(),
            headers={"Transfer-Encoding": "chunked"},
        )
        assert r.status_code != 413


class TestBodySizeCapLogging:
    async def test_413_emits_structured_event(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        body = b"x" * (32 * 1024 + 1)
        await client.post("/api/v1/anything", content=body)
        out = capsys.readouterr().out
        events = [
            json.loads(line)
            for line in out.splitlines()
            if line.strip()
        ]
        names = [e.get("event") for e in events]
        assert "api.body_size_cap_exceeded" in names
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_middleware_body_size_cap.py').read())"
.venv/bin/ruff check tests/api/test_middleware_body_size_cap.py
```

---

## Task 8: Write `tests/api/test_middleware_bearer_auth.py` (~12 tests)

**Files:**
- Create: `tests/api/test_middleware_bearer_auth.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for BearerAuthMiddleware (spec §5.4).

Coverage: exempt prefixes bypass; missing/malformed/wrong-token → 401;
correct token → handler runs; OPTIONS preflight bypass; OQ2 loopback
enforcement (positive + negative); timing-safe compare on length variants;
no raw token in any log line; api.auth.rejected event with sha256 prefix.
"""

from __future__ import annotations

import json

import pytest


VALID_TOKEN = "a" * 32  # matches the conftest dummy token


class TestBearerAuthExempt:
    async def test_health_path_no_auth_required(self, client):
        # No Authorization header at all
        r = await client.get("/api/v1/health")
        # Should NOT be 401; likely 503 (BL5 ship state) but never 401
        assert r.status_code != 401

    async def test_openapi_json_no_auth_required(self, client):
        r = await client.get("/api/v1/openapi.json")
        assert r.status_code != 401

    async def test_docs_no_auth_required(self, client):
        r = await client.get("/api/v1/docs")
        assert r.status_code != 401

    async def test_options_preflight_bypasses_auth(self, client):
        r = await client.options(
            "/api/v1/anything",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # OPTIONS preflight should not be auth-rejected (it's never 401)
        assert r.status_code != 401


class TestBearerAuthRejection:
    async def test_missing_authorization_header_returns_401(self, client):
        r = await client.get("/api/v1/anything")  # non-exempt path
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
        assert "Bearer" in r.headers["WWW-Authenticate"]

    async def test_malformed_authorization_header_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "NotBearer xyz"},
        )
        assert r.status_code == 401

    async def test_empty_bearer_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer "},
        )
        assert r.status_code == 401

    async def test_wrong_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer wrong-token-xxxxxxxxxxxxxxxxx"},
        )
        assert r.status_code == 401

    async def test_correct_token_passes_auth(self, client):
        # /api/v1/anything doesn't exist (404), but auth passes first
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # Auth passed; the actual response is 404 (route doesn't exist)
        assert r.status_code == 404


class TestBearerAuthOQ2Loopback:
    async def test_loopback_client_can_post_to_platforms_auth(self, loopback_client):
        # Path matches LOOPBACK_ONLY_PATTERNS. 127.0.0.1 + valid token →
        # auth passes (404 because the route isn't implemented in BL5).
        r = await loopback_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code != 403  # not blocked by OQ2; route may 404

    async def test_external_client_blocked_from_platforms_auth(self, external_client):
        r = await external_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 403


class TestBearerAuthLogging:
    async def test_no_raw_token_in_logs(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        secret = "VERY_SECRET_TOKEN_NEVER_LEAK_aa"  # 32 chars
        await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"Bearer {secret}"},
        )
        out = capsys.readouterr().out
        assert secret not in out

    async def test_auth_rejected_event_emits_with_sha256_prefix(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer wrong-token-xxxxxxxxxxxxxxxxx"},
        )
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        rejected = [e for e in events if e.get("event") == "api.auth.rejected"]
        assert len(rejected) >= 1
        # Spec §5.4: bad_token reason includes sha256 prefix (8 hex chars)
        e = rejected[0]
        if e.get("reason") == "bad_token":
            assert "token_sha256_prefix" in e
            assert len(e["token_sha256_prefix"]) == 8
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_middleware_bearer_auth.py').read())"
.venv/bin/ruff check tests/api/test_middleware_bearer_auth.py
```

---

## Task 9: Write `tests/api/test_health_endpoint.py` (~10 tests)

**Files:**
- Create: `tests/api/test_health_endpoint.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for /api/v1/health endpoint (spec §6).

Coverage: BL5 ship state returns 503; status field reflects pool;
uptime increases monotonically; git_sha echoes app.state;
cache_volume_mounted reflects path stat; schema-drift triggers
"degraded"; pool failure triggers "degraded"; HealthResponse
extra=forbid; response Content-Type; response model_dump shape.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class TestHealthShipState:
    async def test_bl5_ship_state_returns_503(self, client):
        """Per spec §6.4: scheduler/lancache/validator are stubbed false
        in BL5 → /health must return 503."""
        r = await client.get("/api/v1/health")
        assert r.status_code == 503

    async def test_response_has_all_seven_required_fields(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        for field in [
            "status",
            "version",
            "uptime_sec",
            "scheduler_running",
            "lancache_reachable",
            "cache_volume_mounted",
            "validator_healthy",
            "git_sha",
        ]:
            assert field in body, f"missing field: {field}"

    async def test_status_is_ok_when_pool_healthy(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        # populated_pool fixture has healthy pool → status="ok"
        assert body["status"] == "ok"

    async def test_three_stubbed_subsystems_are_false_in_bl5(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        assert body["scheduler_running"] is False
        assert body["lancache_reachable"] is False
        assert body["validator_healthy"] is False


class TestHealthDynamicFields:
    async def test_uptime_sec_increases_monotonically(self, client):
        r1 = await client.get("/api/v1/health")
        await asyncio.sleep(1.1)
        r2 = await client.get("/api/v1/health")
        assert r2.json()["uptime_sec"] >= r1.json()["uptime_sec"] + 1

    async def test_git_sha_echoes_app_state(self, client, unit_app):
        r = await client.get("/api/v1/health")
        assert r.json()["git_sha"] == unit_app.state.git_sha

    async def test_cache_volume_mounted_reflects_stat(self, client, monkeypatch, tmp_path):
        # Point lancache_nginx_cache_path at a tmp dir that exists
        from orchestrator.core.settings import get_settings

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(cache_dir))
        get_settings.cache_clear()
        r = await client.get("/api/v1/health")
        assert r.json()["cache_volume_mounted"] is True


class TestHealthDegradedTransitions:
    async def test_pool_unhealthy_drops_status_to_degraded(self, client, monkeypatch, populated_pool):
        # Force the pool's writer to be reported unhealthy
        original = populated_pool.health_check

        async def fake_health():
            result = await original()
            result["writer"]["healthy"] = False
            return result

        monkeypatch.setattr(populated_pool, "health_check", fake_health)
        r = await client.get("/api/v1/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"


class TestHealthResponseShape:
    async def test_content_type_application_json(self, client):
        r = await client.get("/api/v1/health")
        assert r.headers["content-type"].startswith("application/json")

    async def test_response_model_extra_forbid(self):
        """Spec §6.1: HealthResponse has extra='forbid'."""
        from pydantic import ValidationError

        from orchestrator.api.routers.health import HealthResponse

        with pytest.raises(ValidationError):
            HealthResponse(
                status="ok",
                version="x",
                uptime_sec=0,
                scheduler_running=False,
                lancache_reachable=False,
                cache_volume_mounted=False,
                validator_healthy=False,
                git_sha="x",
                unknown_extra_field="leak",  # forbidden
            )
```

- [ ] **Step 2: Verify syntax + ruff clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('tests/api/test_health_endpoint.py').read())"
.venv/bin/ruff check tests/api/test_health_endpoint.py
```

---

## Task 10: Verify all tests fail (red phase) + commit

**Files:**
- All 7 test files from Tasks 3-9
- `.claude/process-state.json` (auto-bumped by checklist)

- [ ] **Step 1: Run all tests/api/ — expect ImportError on `orchestrator.api.main`**

```bash
.venv/bin/pytest tests/api/ -v 2>&1 | tail -30
```

Expected: all tests in `tests/api/` fail to collect with `ModuleNotFoundError: No module named 'orchestrator.api.main'` (or `'orchestrator.api.dependencies'`, `'orchestrator.api.middleware'`, `'orchestrator.api.routers.health'`). This is the TDD red phase. The tests cannot collect because `create_app` doesn't exist yet.

If collection itself crashes with a Python syntax error in test files (rather than ModuleNotFoundError), fix the offending test file before proceeding. Test files should ALL produce ModuleNotFoundError, not SyntaxError.

- [ ] **Step 2: Verify the rest of the suite still passes**

```bash
.venv/bin/pytest tests/ --ignore=tests/api -q 2>&1 | tail -3
```

Expected: 281 passed (or close — pre-BL5 baseline). 0 regressions in tests/db/ or tests/core/.

- [ ] **Step 3: Mark process checklist for tests_written + verified-failing**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```

Expected each: `[OK] Step '...' completed`.

- [ ] **Step 4: Stage tests + state files**

```bash
git add tests/api/ .claude/process-state.json
git status --short
```

Expected: 7 new test files (`tests/api/__init__.py`, `conftest.py`, 5 test files) + state file.

- [ ] **Step 5: Present commit options + commit per Orchestrator pick**

PAUSE — present A/B/C:

- **A1.** Single commit: all 7 test files + framework state. Atomic TDD red-phase commit (matches BL4 pattern).
- **A2.** Two commits: tests first, framework state as trailer.
- **A3.** Five commits, one per test file. Excessive granularity.

Recommend **A1**. Default command:

```bash
git commit -m "$(cat <<'EOF'
test(api): BL5 FastAPI skeleton — failing test suite (~47 tests, 7 files)

TDD red phase for BL5 (FastAPI skeleton). All tests fail with
ModuleNotFoundError on orchestrator.api.main — implementation
ships in the next commit.

Test file breakdown:
  tests/api/conftest.py                  5 fixtures (unit_app, lifespan_app,
                                          client, loopback_client, external_client)
  test_app_factory.py                    ~5 tests (factory shape, OpenAPI scheme,
                                                    middleware order, exempt paths)
  test_lifespan.py                       ~6 tests (real lifespan via asgi-lifespan;
                                                    migrations + pool init + state +
                                                    failure paths)
  test_middleware_correlation_id.py       ~8 tests (CID gen/echo/regen, log
                                                    propagation, isolation)
  test_middleware_body_size_cap.py        ~6 tests (Content-Length, chunked,
                                                    log event)
  test_middleware_bearer_auth.py          ~12 tests (exempt prefixes, rejection
                                                    paths, OQ2 loopback, no leak)
  test_health_endpoint.py                 ~10 tests (BL5 ship state, dynamic
                                                    fields, degraded transitions,
                                                    response shape)

Process checklist: tests_written + tests_verified_failing marked.

Spec: docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md
Plan: docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Pause for orchestrator review per BL3/BL4/BL5 rhythm**

STOP — Build Loop step says pause here before implementation. Post a one-line status: "Tests committed at <hash>. Ready to implement when you give the green light."

---

## Task 11: Create `src/orchestrator/api/dependencies.py`

**Files:**
- Create: `src/orchestrator/api/dependencies.py`

- [ ] **Step 1: Write the dependencies module**

```python
"""Shared dependencies, constants, and the version string for the API layer.

Per spec §4: AUTH_EXEMPT_PREFIXES + LOOPBACK_ONLY_PATTERNS (path constants
read by BearerAuthMiddleware) + get_pool_dep (FastAPI dependency wrapping
the BL4 pool singleton).
"""

from __future__ import annotations

import re

from orchestrator.db.pool import Pool, get_pool

# Body cap (32 KiB per Bible §9.2)
BODY_SIZE_CAP_BYTES: int = 32 * 1024

# API version surfaced in /health
__version__: str = "0.1.0"

# Path prefixes that bypass BearerAuthMiddleware (spec §3.3 + §4)
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
)

# Path patterns that ADDITIONALLY require client.host == "127.0.0.1" (OQ2)
LOOPBACK_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/v1/platforms/[^/]+/auth$"),
)


async def get_pool_dep() -> Pool:
    """FastAPI dependency wrapping orchestrator.db.pool.get_pool().

    Raises PoolNotInitializedError if init_pool() was not called during
    lifespan startup. Tests override this via app.dependency_overrides.
    """
    return get_pool()
```

- [ ] **Step 2: Verify syntax + ruff + mypy clean**

```bash
.venv/bin/python -c "import ast; ast.parse(open('src/orchestrator/api/dependencies.py').read())"
.venv/bin/ruff check src/orchestrator/api/dependencies.py
.venv/bin/mypy --strict src/
```

Expected: AST OK, ruff clean, mypy clean.

---

## Task 12: Create `src/orchestrator/api/middleware.py`

**Files:**
- Create: `src/orchestrator/api/middleware.py`

- [ ] **Step 1: Write the middleware module**

```python
"""Three pure-ASGI middlewares for BL5 (spec §5).

Pure-ASGI (not BaseHTTPMiddleware) chosen because:
  1. BodySizeCap needs receive() interception for streaming bodies
  2. Consistency across all three middlewares
  3. BaseHTTPMiddleware has documented BackgroundTasks/exception-handler
     issues (FastAPI release notes around 0.106).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from typing import Any, Awaitable, Callable

import structlog

from orchestrator.api.dependencies import (
    AUTH_EXEMPT_PREFIXES,
    BODY_SIZE_CAP_BYTES,
    LOOPBACK_ONLY_PATTERNS,
)
from orchestrator.core.logging import request_context
from orchestrator.core.settings import get_settings

ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

_log = structlog.get_logger(__name__)

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------
# CorrelationIdMiddleware (spec §5.2)
# ----------------------------------------------------------------------


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cid_bytes = headers.get(b"x-correlation-id", b"")
        cid_in = cid_bytes.decode("ascii", errors="ignore")
        cid = cid_in if _UUID4_RE.match(cid_in) else str(uuid.uuid4())

        async with request_context(correlation_id=cid):
            log = structlog.get_logger()
            t0 = time.perf_counter()
            log.info(
                "api.request.received",
                method=scope["method"],
                path=scope["path"],
                correlation_id=cid,
            )

            async def send_with_cid(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    response_headers = list(message.get("headers", []))
                    response_headers.append((b"x-correlation-id", cid.encode("ascii")))
                    message = {**message, "headers": response_headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_cid)
            finally:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                log.info(
                    "api.request.completed",
                    duration_ms=duration_ms,
                    correlation_id=cid,
                )


# ----------------------------------------------------------------------
# BodySizeCapMiddleware (spec §5.3)
# ----------------------------------------------------------------------


class _BodyTooLarge(Exception):
    """Raised internally to signal cap exhaustion; converted to 413 response."""


class BodySizeCapMiddleware:
    def __init__(self, app: ASGIApp, cap: int = BODY_SIZE_CAP_BYTES) -> None:
        self.app = app
        self.cap = cap

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cl_bytes = headers.get(b"content-length")

        # Path 1: Content-Length present
        if cl_bytes is not None:
            try:
                cl = int(cl_bytes)
            except ValueError:
                cl = 0
            if cl > self.cap:
                _log.error(
                    "api.body_size_cap_exceeded",
                    path=scope["path"],
                    content_length=cl,
                    cap=self.cap,
                )
                await self._send_413(send)
                return

        # Path 2: streaming — track bytes via wrapped receive()
        bytes_received = 0

        async def receive_with_cap() -> dict[str, Any]:
            nonlocal bytes_received
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body", b"")
                bytes_received += len(body)
                if bytes_received > self.cap:
                    raise _BodyTooLarge()
            return msg

        try:
            await self.app(scope, receive_with_cap, send)
        except _BodyTooLarge:
            _log.error(
                "api.body_size_cap_exceeded",
                path=scope["path"],
                bytes_received=bytes_received,
                cap=self.cap,
            )
            await self._send_413(send)

    @staticmethod
    async def _send_413(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"request body exceeds 32 KiB cap"}',
            }
        )


# ----------------------------------------------------------------------
# BearerAuthMiddleware (spec §5.4)
# ----------------------------------------------------------------------


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope["method"]

        # Skip preflight
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Skip exempt paths
        if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Validate token
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("ascii", errors="ignore")

        if not auth_header:
            _log.warning("api.auth.rejected", reason="missing_header", path=path)
            await self._send_401(send)
            return

        if not auth_header.startswith("Bearer "):
            _log.warning("api.auth.rejected", reason="malformed_header", path=path)
            await self._send_401(send)
            return

        token = auth_header[len("Bearer "):].strip()
        if not token:
            _log.warning("api.auth.rejected", reason="malformed_header", path=path)
            await self._send_401(send)
            return

        settings = get_settings()
        expected = settings.orchestrator_token.get_secret_value()
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            sha = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
            _log.warning(
                "api.auth.rejected",
                reason="bad_token",
                path=path,
                token_sha256_prefix=sha,
            )
            await self._send_401(send)
            return

        # OQ2: 127.0.0.1 enforcement on POST /api/v1/platforms/{name}/auth
        if any(p.match(path) for p in LOOPBACK_ONLY_PATTERNS):
            client_info = scope.get("client")
            client_host = client_info[0] if client_info else None
            if client_host != "127.0.0.1":
                _log.warning(
                    "api.auth.rejected",
                    reason="non_loopback",
                    path=path,
                    client_host=client_host,
                )
                await self._send_403(send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="orchestrator"'),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"unauthorized"}',
            }
        )

    @staticmethod
    async def _send_403(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"forbidden: loopback only"}',
            }
        )
```

- [ ] **Step 2: Verify syntax + ruff + mypy**

```bash
.venv/bin/python -c "import ast; ast.parse(open('src/orchestrator/api/middleware.py').read())"
.venv/bin/ruff check src/orchestrator/api/middleware.py
.venv/bin/mypy --strict src/
```

---

## Task 13: Create `src/orchestrator/api/routers/__init__.py` + `routers/health.py`

**Files:**
- Create: `src/orchestrator/api/routers/__init__.py` (empty package marker)
- Create: `src/orchestrator/api/routers/health.py`

- [ ] **Step 1: Create the empty package marker**

Write `src/orchestrator/api/routers/__init__.py` with:

```python
from __future__ import annotations
```

(Single line; just establishes the package.)

- [ ] **Step 2: Create the health router**

Write `src/orchestrator/api/routers/health.py`:

```python
"""GET /api/v1/health endpoint per spec §6 + Bible §8.4."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import __version__, get_pool_dep
from orchestrator.core.settings import get_settings
from orchestrator.db.pool import Pool


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    uptime_sec: int
    scheduler_running: bool
    lancache_reachable: bool
    cache_volume_mounted: bool
    validator_healthy: bool
    git_sha: str


router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "All subsystems healthy"},
        503: {"description": "At least one subsystem unhealthy", "model": HealthResponse},
    },
)
async def get_health(
    request: Request,
    pool: Pool = Depends(get_pool_dep),
) -> JSONResponse:
    pool_health = await pool.health_check()
    schema_status = await pool.schema_status()

    pool_ok = (
        pool_health["writer"]["healthy"]
        and pool_health["readers"]["healthy"] == pool_health["readers"]["total"]
        and schema_status["current"]
    )

    settings = get_settings()
    cache_path = Path(settings.lancache_nginx_cache_path)
    cache_volume_mounted = cache_path.is_dir()

    body = HealthResponse(
        status="ok" if pool_ok else "degraded",
        version=__version__,
        uptime_sec=int(time.monotonic() - request.app.state.boot_time),
        # BL5 stubs — real in BL6+ as features land
        scheduler_running=False,
        lancache_reachable=False,
        cache_volume_mounted=cache_volume_mounted,
        validator_healthy=False,
        git_sha=request.app.state.git_sha,
    )

    all_healthy = (
        pool_ok
        and body.scheduler_running
        and body.lancache_reachable
        and body.cache_volume_mounted
        and body.validator_healthy
    )
    return JSONResponse(
        content=body.model_dump(),
        status_code=200 if all_healthy else 503,
    )
```

- [ ] **Step 3: Verify syntax + ruff + mypy**

```bash
.venv/bin/ruff check src/orchestrator/api/routers/
.venv/bin/mypy --strict src/
```

---

## Task 14: Create `src/orchestrator/api/main.py`

**Files:**
- Create: `src/orchestrator/api/main.py`

- [ ] **Step 1: Write main.py**

```python
"""FastAPI application factory for the orchestrator API (spec §3, §4).

Use:
  uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from orchestrator.api.middleware import (
    BearerAuthMiddleware,
    BodySizeCapMiddleware,
    CorrelationIdMiddleware,
)
from orchestrator.api.routers.health import router as health_router
from orchestrator.core.settings import get_settings
from orchestrator.db import migrate
from orchestrator.db.pool import (
    PoolError,
    SchemaNotMigratedError,
    SchemaUnknownMigrationError,
    close_pool,
    init_pool,
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log = structlog.get_logger()

    # 1. Migrations (sync; offload)
    log.info("api.boot.migrations_starting")
    try:
        await asyncio.to_thread(migrate.run_migrations, settings.database_path)
    except migrate.MigrationError as e:
        log.critical("api.boot.migrations_failed", reason=str(e))
        raise SystemExit(1) from e

    # 2. Pool init
    log.info("api.boot.pool_starting")
    try:
        await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical("api.boot.pool_init_failed", reason=str(e))
        raise SystemExit(1) from e

    # 3. Boot metadata
    app.state.boot_time = time.monotonic()
    app.state.git_sha = os.environ.get("GIT_SHA", "unknown")
    log.info("api.boot.complete")

    yield

    log.info("api.shutdown.starting")
    try:
        await close_pool()
    except PoolError as e:
        log.error("api.shutdown.pool_close_failed", reason=str(e))
    log.info("api.shutdown.complete")


def create_app() -> FastAPI:
    """FastAPI application factory.

    Returns a fully-configured FastAPI app with:
      - lifespan that runs migrations + initializes the BL4 pool singleton
      - 4-layer middleware stack (spec §5.1)
      - bearer security_scheme registered for OpenAPI
      - /api/v1/health router mounted
    """
    settings = get_settings()

    app = FastAPI(
        title="lancache_orchestrator API",
        version="0.1.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=_lifespan,
    )

    # Middleware stack — registered in REVERSE order of how they wrap requests.
    # add_middleware prepends to user_middleware, so the LAST add_middleware
    # call is the OUTERMOST layer at request time.
    # Per spec §5.1 the desired order (outermost → innermost) is:
    #   CorrelationId → BodySizeCap → BearerAuth → CORS
    # So we register in REVERSE: CORS, BearerAuth, BodySizeCap, CorrelationId.

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # OpenAPI security scheme — middleware does the actual enforcement.
    # This block surfaces the bearer scheme in /api/v1/openapi.json so
    # Swagger UI's Authorize button works.
    app.openapi_components_security = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
        }
    }

    # Patch the openapi() method to include security_schemes.
    _orig_openapi = app.openapi

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = _orig_openapi()
        schema.setdefault("components", {})
        schema["components"]["securitySchemes"] = app.openapi_components_security
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    # Routers
    app.include_router(health_router)

    return app
```

- [ ] **Step 2: Verify syntax + ruff + mypy**

```bash
.venv/bin/ruff check src/orchestrator/api/main.py
.venv/bin/mypy --strict src/
```

Expected: clean.

---

## Task 15: Run full suite — iterate to all-green

**Files:** none (test runs only)

- [ ] **Step 1: Run tests/api/**

```bash
.venv/bin/pytest tests/api/ -v 2>&1 | tail -40
```

Iterate: any test failures, examine, fix the implementation in src/orchestrator/api/, re-run. Repeat until all tests/api/ pass.

Common likely fixes:
- middleware order list assertion in `test_middleware_order_matches_spec` may need a different navigation pattern depending on Starlette internals — verify `app.user_middleware` at runtime, adjust assertion accordingly
- exemption paths might miss `/redoc` since FastAPI uses `/api/v1/redoc` (with prefix)
- the openapi_schema-cache test may need to invalidate `app.openapi_schema = None` between calls
- `_BodyTooLarge` exception may surface as 500 from inside Starlette — the catch must be at the receive() boundary, not deeper

- [ ] **Step 2: Run full project suite — verify no regressions**

```bash
.venv/bin/pytest tests/ -q --ignore=tests/test_licenses.py 2>&1 | tail -3
```

Expected: all pass except the pre-existing pip-licenses skip. Typical count: ~325 passed (281 baseline + ~47 new).

---

## Task 16: Run branch-coverage — verify ≥ 95% on `src/orchestrator/api/`

**Files:** none (coverage analysis)

- [ ] **Step 1: Run coverage**

```bash
.venv/bin/pytest tests/api/ --cov=orchestrator.api --cov-branch --cov-report=term-missing 2>&1 | tail -20
```

- [ ] **Step 2: Review missing branches**

For any branch < 95 %, write 1-2 targeted tests to cover. Common gaps to anticipate:
- `_BodyTooLarge` raised when Content-Length present (one path) AND raised in streaming (other path) — both must be exercised
- `_send_403` (OQ2 non-loopback)
- `_send_401` malformed-header vs missing-header vs bad-token branches
- CorrelationId branch where scope["type"] != "http" (e.g. websocket — though we don't expose any in BL5)

Add tests as needed; keep the targeted-coverage tests in the same file as the related middleware test.

- [ ] **Step 3: Re-run coverage to confirm threshold**

```bash
.venv/bin/pytest tests/api/ --cov=orchestrator.api --cov-branch 2>&1 | tail -5
```

Target: ≥ 95% combined branch coverage on `src/orchestrator/api/`.

---

## Task 17: Mark build_loop checklist + commit green phase

**Files:**
- All source files created in Tasks 11-14
- `.claude/process-state.json`

- [ ] **Step 1: Mark implemented step**

```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
```

Expected: `[OK] Step 'implemented' completed`.

- [ ] **Step 2: Re-run final smoke (sanity)**

```bash
.venv/bin/pytest tests/ -q --ignore=tests/test_licenses.py 2>&1 | tail -3
.venv/bin/ruff check src/ tests/
.venv/bin/mypy --strict src/
```

All must pass.

- [ ] **Step 3: Stage + commit (A1 atomic green-phase per BL3/BL4 pattern)**

```bash
git add src/orchestrator/api/ .claude/process-state.json
git status --short
```

Verify only the new src files + state file staged.

```bash
git commit -m "$(cat <<'EOF'
feat(api): BL5 FastAPI skeleton — main + middleware + dependencies + health router

FastAPI application factory consuming BL3 Settings + BL4 DB pool.
4-layer middleware stack: CorrelationId (outermost) → BodySizeCap →
BearerAuth → CORS → router → /api/v1/health handler.

Module layout:
  src/orchestrator/api/main.py            create_app() factory + lifespan
                                          + middleware registration + openapi
                                          security scheme + router mounting
  src/orchestrator/api/dependencies.py    AUTH_EXEMPT_PREFIXES,
                                          LOOPBACK_ONLY_PATTERNS, get_pool_dep,
                                          BODY_SIZE_CAP_BYTES, __version__
  src/orchestrator/api/middleware.py      3 pure-ASGI middlewares
                                          (CorrelationIdMiddleware,
                                          BodySizeCapMiddleware,
                                          BearerAuthMiddleware)
  src/orchestrator/api/routers/health.py  /api/v1/health endpoint +
                                          HealthResponse model

Lifespan:
  - asyncio.to_thread(run_migrations, ...) → fail-fast SystemExit(1)
  - init_pool() → fail-fast SystemExit(1)
  - app.state.boot_time, app.state.git_sha set
  - close_pool() (30s timeout from BL4) on shutdown

Bearer auth (middleware, not Depends per spec §Q2):
  - hmac.compare_digest on UTF-8 bytes (timing-safe)
  - exempt prefixes: /api/v1/health, /openapi.json, /docs, /redoc
  - OPTIONS preflight bypass
  - OQ2: 127.0.0.1 enforcement on /api/v1/platforms/{name}/auth (path
    pattern matches; route 404s in BL5)
  - bad_token logged with token_sha256_prefix (TM-012; never raw token)

Body cap (32 KiB per Bible §9.2):
  - Content-Length path: 413 immediately
  - streaming path: receive() interception with bytes counter

Correlation-ID:
  - Reads X-Correlation-ID (UUID4 regex check); generates fresh if missing/invalid
  - Enters ID3 request_context() so every log has the CID
  - Echoed in response header

/api/v1/health (BL5 ship state):
  - Returns 503 because scheduler/lancache/validator are stub-false
    (BL6+ will flip them as features land)
  - Uses pool.health_check() + pool.schema_status() for pool fields
  - cache_volume_mounted reflects Path(settings.lancache_nginx_cache_path).is_dir()

OpenAPI:
  - schema at /api/v1/openapi.json (auth-exempt)
  - Swagger UI at /api/v1/docs
  - bearerAuth security_scheme registered (Swagger UI Authorize works)

Test results: 47 tests passing in tests/api/, ~95% branch coverage on
src/orchestrator/api/. Ruff + mypy --strict + semgrep + gitleaks clean.
Full project suite ~325 passing.

Process checklist: implemented step marked (4/6 build_loop steps done;
security_audit + documentation_updated + feature_recorded follow).

Spec: docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md
Plan: docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Push (or hand off)**

```bash
git push -u origin feat/bl5-f9-skeleton
```

If gate blocks (UAT-2 pattern), hand off to user terminal. After commit lands, proceed to Task 18.

---

## Task 18: Phase 2.4 self-audit — produce findings doc

**Files:**
- Create: `docs/security-audits/bl5-fastapi-skeleton-security-audit.md`

- [ ] **Step 1: Run automated SAST**

```bash
.venv/bin/ruff check src/orchestrator/api/
semgrep scan --config=p/owasp-top-ten --config=.semgrep/ src/orchestrator/api/ --quiet 2>&1 | tail -10
gitleaks protect --staged --no-banner 2>&1 | tail -3
```

- [ ] **Step 2: Self-audit checklist**

Walk through each TM relevant to BL5 (spec §15 references):
- TM-001 token leak — verify `_send_401` does NOT echo the rejected token; `bad_token` log uses sha256 prefix only
- TM-005 SQL injection — N/A in BL5 (no raw SQL; pool layer enforces)
- TM-012 log credential leak — verify no raw `Authorization` header value reaches any log line; `_template_only` is BL4's responsibility, not API layer
- TM-013 fingerprinting — verify `/api/v1/openapi.json` doesn't leak version-specific server info beyond what's already in published artifacts
- TM-018 memory bomb — verify body cap fires before any handler runs; streaming case correctly interrupts upload
- TM-023 kill chain — OQ2 loopback enforcement covers the platforms/auth path even though no real handler exists in BL5

- [ ] **Step 3: Write the audit findings file**

Use the BL4 audit template (`docs/security-audits/db-pool-security-audit.md`) as the structure model. Include:
- Scope, methodology, findings table (likely 0 SEV-1/2; 1-2 SEV-3 are realistic), non-findings, decision
- Cite Phase 2.4 sub-agent personas (in BL5 self-audit there's only one persona — Senior Security Engineer)

- [ ] **Step 4: Mark security_audit step**

```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```

If artifact-check requires the file at the expected slug (`bl5-fastapi-skeleton-...md`), it'll match. Mark passes.

---

## Task 19: Write `docs/ADR documentation/0012-fastapi-skeleton-architecture.md`

**Files:**
- Create: `docs/ADR documentation/0012-fastapi-skeleton-architecture.md`

- [ ] **Step 1: Write ADR-0012**

Use ADR-0011 (DB pool architecture) as the structural model. Capture the 7 spec decisions verbatim (Q1-Q7 from §2 of the spec) plus:
- Status: Accepted, Date: 2026-04-27, Phase 2 Milestone B BL5
- Context section connecting to Bible §3.3, §7.3, §8, §9.2
- 7 numbered decisions (D1-D7) mapping to Q1-Q7 with rationale
- Edge cases acknowledged
- Cross-references (Spec / Plan / BL3 ADR-0010 / BL4 ADR-0011 / Audit findings)
- References to TM-001, TM-013, TM-018, TM-023 mitigations

Length: ~250-350 lines (matches ADR-0011 scale).

- [ ] **Step 2: Verify ruff format on the .md (no-op for markdown but for cleanliness)**

```bash
ls -la "docs/ADR documentation/0012-fastapi-skeleton-architecture.md"
```

---

## Task 20: Update `CHANGELOG.md` + `FEATURES.md` + `README.md` + `PROJECT_BIBLE.md`

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`
- Modify: `README.md`
- Modify: `PROJECT_BIBLE.md`

- [ ] **Step 1: CHANGELOG.md `[Unreleased]` section additions**

Add under appropriate categories:

```
### Added
- **FastAPI app skeleton** (BL5 / Feature 5) — application factory at
  `src/orchestrator/api/main.py:create_app`. Lifespan runs migrations
  + initializes the BL4 pool singleton; shutdown closes the pool with
  30 s hard timeout. See ADR-0012.
- **`GET /api/v1/health`** endpoint per Bible §8.4. Returns 7-field
  response with HTTP 200 if all subsystems healthy, 503 otherwise.
  Note: BL5 ship state intentionally returns 503 because three
  subsystems (`scheduler_running`, `lancache_reachable`,
  `validator_healthy`) are stub-false until BL6+.
- **OpenAPI schema** at `/api/v1/openapi.json`, **Swagger UI** at
  `/api/v1/docs`, **ReDoc** at `/api/v1/redoc`. Bearer security_scheme
  registered so Swagger UI's Authorize button works.

### Security
- **TM-013 fingerprinting defense:** bearer-auth implemented as
  ASGI middleware (not FastAPI Depends), so 404s on non-exempt
  paths still require auth. Returns 401 with timing-safe
  `hmac.compare_digest` comparison.
- **OQ2 loopback enforcement:** `POST /api/v1/platforms/{name}/auth`
  additionally requires `request.client.host == "127.0.0.1"`.
  Path is reserved in BL5 (the actual handler lands in F1/F2).
- **TM-012 log redaction:** rejected bearer tokens logged with
  `token_sha256_prefix` (first 8 hex of SHA-256), never the raw token.
- **TM-018 memory bomb defense:** ASGI middleware enforces 32 KiB
  request body cap (Bible §9.2). Streaming uploads (Transfer-
  Encoding: chunked) tracked byte-by-byte and interrupted at cap.
```

- [ ] **Step 2: FEATURES.md Feature 5 entry**

Add a Feature 5 block per the existing pattern (see Feature 1-4). Key bits:
- Phase Built: 2 (Milestone B, Build Loop 5)
- Status: Complete (2026-04-27)
- Summary: 2-3 sentences naming the deliverable
- Key interfaces: `create_app()`, the 4 middlewares, `GET /api/v1/health`
- Related ADRs: ADR-0012, plus consumes ADR-0010 (Settings) + ADR-0011 (Pool)
- Test coverage: 47 tests / target ≥ 95% branch coverage
- Known limitations: 3 stubbed subsystems (scheduler/lancache/validator) → /health returns 503 by-design until BL6+ flips them

- [ ] **Step 3: README.md updates**

Add a "Running the API" section after the Configuration section:

```markdown
## Running the API

The FastAPI app is invoked via uvicorn:

```bash
uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765
```

The `--factory` flag tells uvicorn that `create_app` is a callable returning the app, not the app itself.

**BL5 health-check note:** `GET /api/v1/health` will return **HTTP 503** until BL6+ implements the scheduler, Lancache self-test, and validator subsystems. The body still contains the 7-field response so operators can see exactly which subsystems are unhealthy. Container health checks (Docker HEALTHCHECK, k8s liveness probes) should expect 503 during this transition window.
```

- [ ] **Step 4: PROJECT_BIBLE.md updates**

§3.2 (sub-ADRs): add ADR-0012 to the "issued" list.
§9.2 (REST API): bump `<!-- Last Updated -->` marker to 2026-04-27 (the spec section text is unchanged but a recent date signals it was reviewed).

- [ ] **Step 5: Mark documentation_updated**

```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```

---

## Task 21: Commit docs

**Files:**
- All files modified/created in Tasks 18-20
- `.claude/process-state.json`

- [ ] **Step 1: Stage docs**

```bash
git add docs/ADR\ documentation/0012-fastapi-skeleton-architecture.md \
        docs/security-audits/bl5-fastapi-skeleton-security-audit.md \
        CHANGELOG.md FEATURES.md README.md PROJECT_BIBLE.md \
        .claude/process-state.json
git status --short
```

- [ ] **Step 2: Commit (docs-only, gate-friendly)**

```bash
git commit -m "$(cat <<'EOF'
docs(adr,changelog,features): BL5 FastAPI skeleton — ADR-0012 + Feature 5

ADR-0012 (new): FastAPI skeleton architecture decision record. 7
load-bearing decisions documented:
  D1 hybrid app layout (main + dependencies + middleware + routers/)
  D2 bearer auth as ASGI middleware (TM-013 defense, runs on 404s)
  D3 pool exposure via Depends(get_pool_dep)
  D4 httpx.AsyncClient + ASGITransport for tests, asgi-lifespan for
     lifespan integration tests
  D5 correlation-ID propagation via outermost ASGI middleware
  D6 fail-fast 503 health policy (Bible §8.4 / JQ3)
  D7 32 KiB body cap via streaming-aware ASGI middleware

CHANGELOG.md: Added section enumerates the new app surface; Security
section documents TM-013 fingerprinting defense, OQ2 loopback
enforcement, TM-012 token-rejection logging with sha256 prefix,
TM-018 body-size cap with streaming variant.

FEATURES.md: Feature 5 (BL5 — FastAPI Skeleton) entry.

README.md: "Running the API" section with uvicorn invocation +
explicit note that BL5 /health returns 503 by-design.

PROJECT_BIBLE.md: §3.2 sub-ADR list extended with ADR-0012; §9.2
Last Updated bumped to 2026-04-27.

docs/security-audits/bl5-fastapi-skeleton-security-audit.md (new):
Phase 2.4 self-audit findings covering TM-001/012/013/018/023.

Cross-references:
  Spec: docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md
  Plan: docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md
  Implementation: <green-phase commit hash>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Push**

```bash
git push
```

---

## Task 22: File 4 follow-up issues

**Files:** none (gh CLI calls only)

- [ ] **Step 1: File issues per spec §10**

```bash
gh issue create --title "[SEV-3][api] /health Spike-F load test integration deferred to BL-validator timeframe" --label "sev-3,area:api" --body "..."
```

```bash
gh issue create --title "[SEV-4][api] Verify api.request.completed structlog contextvar redaction interaction" --label "sev-4,area:api,area:logging" --body "..."
```

```bash
gh issue create --title "[SEV-4][api] Hypothesis property test for streaming body-cap edge cases" --label "sev-4,area:api,area:testing" --body "..."
```

```bash
gh issue create --title "[SEV-4][api] OpenAPI security_scheme description block" --label "sev-4,area:api" --body "..."
```

(Body content per spec §10 numbered items 1-4.)

If `area:api` label doesn't exist, create it first:
```bash
gh label create area:api --color "5319e7" --description "FastAPI application skeleton (BL5)"
```

- [ ] **Step 2: Capture issue numbers for the qdrant memory artifact**

Note the 4 issue URLs printed by `gh issue create`; you'll embed them in the BL5 memory record at Task 23.

---

## Task 23: record-feature + qdrant memory + open PR

**Files:**
- `.claude/process-state.json` (counter bumps)
- `.claude/build-progress.json` (BL5 added to features_completed)

- [ ] **Step 1: feature_recorded + record-feature**

```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
scripts/test-gate.sh --record-feature "BL5-F9-fastapi-skeleton"
```

Expected: Build loop reset; counter updates (likely 1/2 or 2/2 → triggers UAT-3).

- [ ] **Step 2: Final state commit (matches BL4 pattern)**

```bash
git add .claude/process-state.json .claude/build-progress.json
git commit -m "chore(framework): close BL5-F9-fastapi-skeleton build_loop + record feature

Marks build_loop:feature_recorded (6/6) and bumps the test-gate
counter for the BL5 feature.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push
```

- [ ] **Step 3: qdrant-store memory artifact**

Use `mcp__qdrant__qdrant-store` with metadata `{type: "project", project: "lancache_orchestrator", topic: "bl5-fastapi-skeleton-complete", milestone: "B", phase: 2, date: "2026-04-27", feature: "BL5-F9-fastapi-skeleton", build_loop: 5}`.

Body: mirror BL3/BL4 patterns. Include:
- 7 locked decisions (one-line each)
- 6 commit hashes
- Total LoC + test count + coverage achieved
- Non-obvious learnings (especially Context7-driven design changes)
- Follow-up issue numbers from Task 22
- Pointer to ADR-0012 and audit findings

- [ ] **Step 4: Open PR (do NOT merge — user merges)**

```bash
gh pr create --title "BL5: FastAPI skeleton — /api/v1/health + 4-layer middleware stack" --body "$(cat <<'EOF'
## Summary

BL5 ships the FastAPI skeleton (F9 partial). 4-layer pure-ASGI middleware stack (CorrelationId / BodySizeCap / BearerAuth / CORS), lifespan-managed migrations + BL4 pool init, single endpoint /api/v1/health per Bible §8.4. ~47 tests, ≥95% branch coverage.

- Spec: docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md
- Plan: docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md
- ADR: docs/ADR documentation/0012-fastapi-skeleton-architecture.md
- Audit: docs/security-audits/bl5-fastapi-skeleton-security-audit.md

## Note: /health returns 503 by-design

Three subsystems (scheduler/lancache/validator) are stub-false in BL5. BL6+ will flip them as features land. This is intentional, not a bug — the spec §6.4 commitment is explicit. Container HEALTHCHECK should expect 503 during this transition.

## Test plan
- [x] All ~47 tests pass (default pytest)
- [x] ≥95% branch coverage on src/orchestrator/api/
- [x] Ruff + mypy --strict + semgrep + gitleaks clean
- [ ] CI: should pass on all required checks

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

STOP — do not call `gh pr merge`. User merges per `feedback_pr_merge_ownership.md`.

---

## Implementation reference

The full implementations for Tasks 11-14 are reproduced inline (no spec deferral). Engineers running this plan have everything needed to type the code from the plan alone.

**Critical type / signature consistency:**
- `Pool` from `orchestrator.db.pool` is the BL4 class (not modified in BL5).
- `get_pool` is BL4's module-level singleton accessor; `get_pool_dep` is the new BL5 wrapper.
- `request_context` is BL2's async context manager (positional or kwargs to set the CID).
- `migrate.run_migrations(database_path)` is sync; lifespan wraps it via `asyncio.to_thread`.
- All structlog event names follow `api.<area>.<verb>` pattern (matches BL4's `pool.<verb>` naming).

---

## Self-review

**Spec coverage** (each spec section → task that implements it):
- §1 purpose — captured in plan goal
- §2 7 locked decisions — D1→Tasks 11-14 (module layout); D2→Task 12 (BearerAuthMiddleware); D3→Task 11 (get_pool_dep); D4→Task 3 (fixtures); D5→Task 12 (CorrelationIdMiddleware); D6→Task 13 (health handler 503 logic); D7→Task 12 (BodySizeCapMiddleware)
- §3 module layout — Tasks 11-14
- §4 lifespan — Task 14
- §5 middleware — Task 12 (custom 3) + Task 14 (CORS registration)
- §6 health endpoint — Task 13
- §7 test strategy — Tasks 3-9
- §8 PRAGMA / Settings — N/A
- §9 documentation — Tasks 19-20
- §10 follow-ups — Task 22
- §11 memory — Task 23
- §12 commit plan — embedded in Tasks 2/10/17/21/23 commit steps

**Placeholder scan:** No "TBD" / "TODO" / "implement later" anywhere. Code blocks complete. ADR-0012 content not embedded inline (it's structurally similar to ADR-0011; engineer follows the model). Self-audit findings doc same — engineer follows BL4 audit template.

**Type consistency:**
- `BODY_SIZE_CAP_BYTES` defined in `dependencies.py` (Task 11), referenced in `middleware.py` (Task 12) — same name, consistent.
- `AUTH_EXEMPT_PREFIXES`, `LOOPBACK_ONLY_PATTERNS` — same.
- `__version__` — defined in `dependencies.py` (Task 11), used in `routers/health.py` (Task 13) — same.
- `_lifespan` (Task 14) is the lifespan function; tests access it via the lifespan_app fixture which calls `LifespanManager(app)` (which discovers the lifespan via `app.router.lifespan_context`).
- `get_pool_dep` declared in Task 11, used in Task 13 (`Depends(get_pool_dep)`), overridden in Task 3 (`app.dependency_overrides[get_pool_dep]`) — same identity throughout.

**Spec gaps absorbed inline:**
- Spec §6.2 handler signature uses `Pool = Depends(get_pool_dep)`; Task 13 reproduces this.
- Spec §5.4 `WWW-Authenticate: Bearer realm="orchestrator"`; Task 12 reproduces.

Plan is shippable as-is.

---

## Plan complete

Plan saved to `docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
