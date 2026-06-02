# F5 Steam Prefill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Download a Steam game's depot chunks through the lancache (stream-and-discard) so they get cached, then auto-trigger F7 validation.

**Architecture:** A `prefill` job handler builds a deduped `(depot_id, sha)` chunk list (latest manifest per depot → worker `manifest.expand`, reusing F7's path), then an orchestrator-side async httpx downloader GETs each `/depot/{id}/chunk/{sha}` through lancache with Steam UA + Host override, bounded by a semaphore, streaming and discarding bodies. On success it enqueues a `validate` job (ID5). Bundled: F7's `disk_stat` now requires the owner-read bit so mode-000 unreadable cache files aren't counted.

**Tech Stack:** httpx (already an orchestrator dep), asyncio, aiosqlite, structlog, Pydantic v2.

**Process note:** Commits are gated behind `scripts/process-checklist.sh`; this project uses ONE combined `feat` commit per feature (not per-task). Spec + spike already committed (02c2d33). Implement all tasks, then Task 7 does the gate sweep + docs + combined commit (commit structure confirmed with the Orchestrator first) + PR. Per-task "Commit" steps are intentionally omitted.

---

## Task 1: Settings — prefill config

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write failing tests** (append near the steam-worker settings tests):

```python
    def test_prefill_defaults(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        from orchestrator.core.settings import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        assert s.lancache_base_url == "http://127.0.0.1"
        assert s.steam_cdn_host == "lancache.steamcontent.com"
        assert s.prefill_user_agent == "Valve/Steam HTTP Client 1.0"
        assert s.prefill_concurrency == 32
        assert s.prefill_chunk_timeout_sec == 10.0
        assert s.prefill_chunk_max_attempts == 3

    def test_prefill_concurrency_bounds(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        from orchestrator.core.settings import Settings, get_settings

        get_settings.cache_clear()
        with pytest.raises(ValueError, match="prefill_concurrency"):
            Settings(prefill_concurrency=0)
        with pytest.raises(ValueError, match="prefill_concurrency"):
            Settings(prefill_concurrency=999)

    def test_prefill_chunk_max_attempts_bounds(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        from orchestrator.core.settings import Settings, get_settings

        get_settings.cache_clear()
        with pytest.raises(ValueError, match="prefill_chunk_max_attempts"):
            Settings(prefill_chunk_max_attempts=0)
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError`)

Run: `.venv/bin/pytest tests/core/test_settings.py -q -k prefill`

- [ ] **Step 3: Add fields** to `Settings` (near the lancache cache topology / steam-worker fields):

```python
    # --- F5 prefill ---------------------------------------------------
    lancache_base_url: str = "http://127.0.0.1"
    steam_cdn_host: str = "lancache.steamcontent.com"
    prefill_user_agent: str = "Valve/Steam HTTP Client 1.0"
    prefill_concurrency: int = Field(default=32, ge=1, le=256)
    prefill_chunk_timeout_sec: float = Field(default=10.0, gt=0.0, le=120.0)
    prefill_chunk_max_attempts: int = Field(default=3, ge=1, le=10)
```

- [ ] **Step 4: Run — expect PASS**

---

## Task 2: F7 readability enhancement (mode-000 exclusion)

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py` (`_stat_batch`)
- Test: `tests/validator/test_disk_stat.py`

- [ ] **Step 1: Write failing tests:**

```python
async def test_unreadable_mode000_not_counted(tmp_path):
    """F5: a mode-000 cache file is unreadable by lancache; it must NOT count
    as cached even though it exists with size>0."""
    import os

    f = tmp_path / "unreadable"
    f.write_bytes(b"data")
    os.chmod(f, 0o000)
    try:
        cached, missing = await validate_chunks([f])
    finally:
        os.chmod(f, 0o644)  # restore so tmp cleanup can remove it
    assert (cached, missing) == (0, 1)


async def test_readable_mode644_counted(tmp_path):
    import os

    f = tmp_path / "readable"
    f.write_bytes(b"data")
    os.chmod(f, 0o644)
    cached, missing = await validate_chunks([f])
    assert (cached, missing) == (1, 0)
```

- [ ] **Step 2: Run — expect FAIL** (mode-000 currently counted as cached → `(1, 0)`)

Run: `.venv/bin/pytest tests/validator/test_disk_stat.py -q -k "mode000 or mode644"`

- [ ] **Step 3: Update `_stat_batch`** — add the owner-read bit check:

```python
    cached = 0
    errors = 0
    for p in paths:
        try:
            # A symlink is never a genuine cache file — don't follow it.
            if p.is_symlink():
                continue
            st = p.stat()
            # F5: lancache (www-data, the file owner) must be able to READ the
            # file to serve it. Mode-000 cache files exist but are unreadable
            # (~1.7% on the host, issue #128) — exclude them. stat() returns
            # st_mode without needing read access to the file content, so this
            # works even though the orchestrator (uid 1000) can't open
            # www-data:600 files itself.
            if st.st_size > 0 and (st.st_mode & 0o400):
                cached += 1
        except FileNotFoundError:
            pass
        except OSError:
            errors += 1
    return cached, errors
```

- [ ] **Step 4: Run — expect PASS** (and full `tests/validator/` still green)

Run: `.venv/bin/pytest tests/validator/ -q`

---

## Task 3: Prefill downloader (async httpx engine)

**Files:**
- Create: `src/orchestrator/prefill/__init__.py` (empty)
- Create: `src/orchestrator/prefill/downloader.py`
- Test: `tests/prefill/__init__.py`, `tests/prefill/test_downloader.py`

- [ ] **Step 1: Write failing tests** (httpx `MockTransport`, no real network/sleep):

```python
"""Tests for orchestrator.prefill.downloader (F5)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.prefill.downloader import prefill_chunks, steam_chunk_download_uri

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32
SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN, **kw)


def test_chunk_uri():
    assert steam_chunk_download_uri(529345, SHA) == f"/depot/529345/chunk/{SHA}"


async def test_all_ok(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x" * 10)

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    uris = [f"/depot/1/chunk/{SHA}", f"/depot/1/chunk/{'b' * 40}"]
    result = await prefill_chunks(uris, _settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (2, 2, 0)
    # headers + absolute URL assembled from lancache_base_url
    r0 = seen[0]
    assert r0.headers["User-Agent"] == "Valve/Steam HTTP Client 1.0"
    assert r0.headers["Host"] == "lancache.steamcontent.com"
    assert str(r0.url) == f"http://127.0.0.1/depot/1/chunk/{SHA}"


async def test_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    result = await prefill_chunks([f"/depot/1/chunk/{SHA}"], _settings())
    assert (result.chunks_ok, result.chunks_failed) == (1, 0)
    assert calls["n"] == 2  # one retry


async def test_persistent_failure_recorded(monkeypatch):
    def handler(request):
        return httpx.Response(500)

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    result = await prefill_chunks(
        [f"/depot/1/chunk/{SHA}"], _settings(prefill_chunk_max_attempts=2)
    )
    assert (result.chunks_ok, result.chunks_failed) == (0, 1)
    assert result.failures and result.failures[0][0] == f"/depot/1/chunk/{SHA}"


async def test_empty_list(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    result = await prefill_chunks([], _settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (0, 0, 0)


async def _noop_sleep(_seconds):
    return None
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

Run: `.venv/bin/pytest tests/prefill/test_downloader.py -q`

- [ ] **Step 3: Implement `downloader.py`:**

```python
"""F5 Steam prefill — async chunk downloader.

Downloads depot chunks THROUGH the lancache (stream-and-discard) so lancache
caches them under the key F7 validates. See spikes/spike_a5_prefill.md.

Runs in the orchestrator process (httpx async); no steam-next/worker needed
for the download itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import structlog

from orchestrator.validator.cache_key import steam_chunk_uri

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_BACKOFFS_SEC = (1.0, 4.0, 16.0)
_FAILURE_CAP = 50  # cap retained failure detail to keep payloads/logs bounded


def steam_chunk_download_uri(depot_id: int, sha_hex: str) -> str:
    """`/depot/{depot_id}/chunk/{sha}` — reuses the validator's shape checks."""
    return steam_chunk_uri(depot_id, sha_hex)


@dataclass
class PrefillResult:
    chunks_total: int
    chunks_ok: int
    chunks_failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an httpx.MockTransport. None → real network."""
    return None


def _backoff(attempt: int) -> float:
    return _BACKOFFS_SEC[min(attempt, len(_BACKOFFS_SEC) - 1)]


async def prefill_chunks(
    chunk_uris: list[str],
    settings: Settings,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> PrefillResult:
    """GET each chunk URI through lancache, streaming + discarding the body.

    Bounded by ``Semaphore(prefill_concurrency)``. Each chunk is retried up to
    ``prefill_chunk_max_attempts`` with [1,4,16]s backoff on timeout /
    transport error / 5xx. "ok" = 2xx.
    """
    total = len(chunk_uris)
    if total == 0:
        return PrefillResult(0, 0, 0)

    sem = asyncio.Semaphore(settings.prefill_concurrency)
    headers = {
        "User-Agent": settings.prefill_user_agent,
        "Host": settings.steam_cdn_host,
    }
    timeout = httpx.Timeout(settings.prefill_chunk_timeout_sec, connect=10.0)
    done = 0
    ok = 0
    failures: list[tuple[str, str]] = []
    lock = asyncio.Lock()

    transport = _build_transport()
    client_kwargs = {"base_url": settings.lancache_base_url, "timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def fetch(uri: str) -> None:
            nonlocal done, ok
            reason = "unknown"
            for attempt in range(settings.prefill_chunk_max_attempts):
                try:
                    async with client.stream("GET", uri, headers=headers) as resp:
                        if 200 <= resp.status_code < 300:
                            async for _ in resp.aiter_bytes():
                                pass  # stream + discard
                            async with lock:
                                ok += 1
                                done += 1
                                if on_progress is not None:
                                    on_progress(done, total)
                            return
                        reason = f"http {resp.status_code}"
                        if resp.status_code < 500:
                            break  # 4xx won't be fixed by retry
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    reason = f"{type(e).__name__}"
                if attempt < settings.prefill_chunk_max_attempts - 1:
                    await asyncio.sleep(_backoff(attempt))
            async with lock:
                failures.append((uri, reason))
                done += 1
                if on_progress is not None:
                    on_progress(done, total)

        async def guarded(uri: str) -> None:
            async with sem:
                await fetch(uri)

        await asyncio.gather(*(guarded(u) for u in chunk_uris))

    return PrefillResult(
        chunks_total=total,
        chunks_ok=ok,
        chunks_failed=total - ok,
        failures=failures[:_FAILURE_CAP],
    )
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/prefill/test_downloader.py -q`

---

## Task 4: Prefill job handler

**Files:**
- Create: `src/orchestrator/jobs/handlers/prefill.py`
- Modify: `src/orchestrator/jobs/handlers/__init__.py` (register)
- Test: `tests/jobs/test_prefill_handler.py`

- [ ] **Step 1: Write failing tests:**

```python
"""Tests for orchestrator.jobs.handlers.prefill (F5)."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.prefill.downloader import PrefillResult

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


class _StubSteam:
    def __init__(self, expand=None, fetch=None):
        self._expand = expand or {"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}
        self._fetch = fetch
        self.fetch_calls = 0

    async def manifest_expand(self, raw):
        return self._expand

    async def manifest_fetch(self, app_id):
        self.fetch_calls += 1
        return self._fetch or {"manifests": []}


def _job(game_id, platform="steam"):
    return {"id": 1, "kind": "prefill", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, platform="steam", app_id="730"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def _seed_manifest(pool, game_id, *, depot_id=731, version="100"):
    await pool.execute_write(
        "INSERT INTO manifests (game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 100, ?)",
        (game_id, depot_id, version, b"BLOB"),
    )


async def test_downloading_set_and_validate_enqueued_on_success(pool, monkeypatch):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), len(uris), 0)

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=_StubSteam()))

    # a validate job was enqueued (ID5)
    vj = await pool.read_one(
        "SELECT kind, state FROM jobs WHERE kind='validate' AND game_id=?", (game_id,)
    )
    assert vj == {"kind": "validate", "state": "queued"}


async def test_chunk_failure_marks_game_failed(pool, monkeypatch):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), 0, len(uris), [("/depot/731/chunk/x", "http 500")])

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=_StubSteam()))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"
    # no validate enqueued on failure
    vj = await pool.read_one("SELECT id FROM jobs WHERE kind='validate' AND game_id=?", (game_id,))
    assert vj is None


async def test_no_manifests_triggers_fetch(pool, monkeypatch):
    game_id = await _seed_game(pool)  # no manifest rows
    stub = _StubSteam()

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), len(uris), 0)

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)

    async def fake_fetch(self, app_id):
        self.fetch_calls += 1
        await _seed_manifest(pool, game_id)  # fetch populates manifests
        return {"manifests": [{}]}

    monkeypatch.setattr(_StubSteam, "manifest_fetch", fake_fetch)
    await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
    assert stub.fetch_calls == 1


async def test_non_steam_raises(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="fort")
    with pytest.raises(ValueError, match="steam"):
        await prefill_handler(_job(game_id, "epic"), Deps(pool=pool, steam_client=_StubSteam()))


async def test_unknown_game_raises(pool):
    with pytest.raises(ValueError, match="not found"):
        await prefill_handler(_job(99999), Deps(pool=pool, steam_client=_StubSteam()))


async def test_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "prefill" in HANDLERS
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

- [ ] **Step 3: Implement `prefill.py`:**

```python
"""F5 — prefill job handler.

Builds the game's deduped chunk list (latest manifest per depot →
manifest.expand, reusing F7's query) and downloads each chunk through the
lancache. On full success, enqueues a validate job (ID5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.prefill.downloader import prefill_chunks, steam_chunk_download_uri
from orchestrator.validator.disk_stat import _LATEST_PER_DEPOT_SQL

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


async def _load_chunk_uris(deps: Deps, game_id: int) -> list[str]:
    rows = await deps.pool.read_all(_LATEST_PER_DEPOT_SQL, (game_id,))
    seen: set[tuple[int, str]] = set()
    uris: list[str] = []
    for row in rows:
        depot_id = int(row["depot_id"])
        expanded = await deps.steam_client.manifest_expand(row["raw"])
        for sha in expanded.get("chunk_shas", []):
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uris.append(steam_chunk_download_uri(depot_id, sha))
    return uris


async def prefill_handler(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one Steam game's chunks through the lancache (F5).

    Raises:
        ValueError — non-steam platform or unknown game.
        RuntimeError — steam_client is None, or chunks failed to download.
    """
    if job.get("platform") != "steam":
        raise ValueError(f"prefill only supports steam (got {job.get('platform')!r})")
    if deps.steam_client is None:
        raise RuntimeError("steam_client is required for prefill handler")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("prefill job has no game_id")

    game = await deps.pool.read_one(
        "SELECT id, app_id, platform FROM games WHERE id=?", (game_id,)
    )
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game["platform"] != "steam":
        raise ValueError(f"game {game_id} platform is {game['platform']!r}, not steam")

    job_id = job.get("id")
    await deps.pool.execute_write(
        "UPDATE games SET status='downloading' WHERE id=?", (game_id,)
    )
    _log.info("prefill.started", job_id=job_id, game_id=game_id)

    # Ensure manifests exist (fetch once if the game has none).
    uris = await _load_chunk_uris(deps, game_id)
    if not uris:
        existing = await deps.pool.read_one(
            "SELECT 1 AS one FROM manifests WHERE game_id=? AND depot_id IS NOT NULL LIMIT 1",
            (game_id,),
        )
        if existing is None:
            try:
                app_id_int = int(game["app_id"])
            except (TypeError, ValueError) as e:
                raise ValueError(f"game {game_id} app_id not numeric") from e
            _log.info("prefill.fetching_manifests", job_id=job_id, game_id=game_id)
            await deps.steam_client.manifest_fetch(app_id_int)
            uris = await _load_chunk_uris(deps, game_id)

    settings = get_settings()
    result = await prefill_chunks(uris, settings)
    _log.info(
        "prefill.completed",
        job_id=job_id,
        game_id=game_id,
        total=result.chunks_total,
        ok=result.chunks_ok,
        failed=result.chunks_failed,
    )

    if result.chunks_failed > 0:
        await deps.pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=?",
            (f"prefill: {result.chunks_failed}/{result.chunks_total} chunks failed"[:200], game_id),
        )
        raise RuntimeError(
            f"prefill failed: {result.chunks_failed}/{result.chunks_total} chunks"
        )

    # ID5: success → enqueue a validate job (it sets the final status).
    await deps.pool.execute_write(
        "INSERT INTO jobs (kind, game_id, platform, state, source) "
        "VALUES ('validate', ?, 'steam', 'queued', 'prefill')",
        (game_id,),
    )
    await deps.pool.execute_write(
        "UPDATE games SET last_prefilled_at=CURRENT_TIMESTAMP WHERE id=?", (game_id,)
    )
    _log.info("prefill.validate_enqueued", job_id=job_id, game_id=game_id)
```

NOTE: `jobs.source` CHECK allows `('scheduler','cli','gameshelf','api')` — confirm whether `'prefill'` is allowed; if not, use `'scheduler'` for the auto-enqueued validate, OR widen the CHECK. **During execution, check `migrations/0001_initial.sql` for the `source` CHECK and pick an allowed value (likely `'scheduler'`).**

Register in `handlers/__init__.py`:
```python
    from orchestrator.jobs.handlers.prefill import prefill_handler
    register("prefill", prefill_handler)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/jobs/test_prefill_handler.py -q`

---

## Task 5: Prefill trigger endpoint

**Files:**
- Create: `src/orchestrator/api/routers/prefill_trigger.py`
- Modify: `src/orchestrator/api/main.py` (import + include_router)
- Test: `tests/api/test_prefill_trigger_router.py`

- [ ] **Step 1: Write failing tests** — copy `tests/api/test_validate_trigger_router.py`, swap `validate` → `prefill` and the path `/validate` → `/prefill` (cover 202 queue, dedup, 404, 400 non-steam, 401 auth, 503 PoolError).

- [ ] **Step 2: Run — expect FAIL** (route missing)

- [ ] **Step 3: Implement the router** (copy `validate_trigger.py`, swap `validate`→`prefill`, path, log names, tag):

```python
"""POST /api/v1/games/{game_id}/prefill — prefill trigger (F5)."""
from __future__ import annotations
from typing import TYPE_CHECKING
import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
if TYPE_CHECKING:
    from orchestrator.db.pool import Pool
_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/games", tags=["prefill"])

@router.post("/{game_id}/prefill", responses={
    202: {"description": "Prefill job queued or existing in-flight job returned"},
    400: {"description": "Game is on a non-steam platform"},
    404: {"description": "Game not found"},
    503: {"description": "Database unavailable"}})
async def trigger_prefill(game_id: int, pool: Pool = Depends(get_pool_dep)) -> JSONResponse:  # noqa: B008
    try:
        game = await pool.read_one("SELECT id, platform FROM games WHERE id=?", (game_id,))
        if game is None:
            raise HTTPException(status_code=404, detail=f"game {game_id} not found")
        if game["platform"] != "steam":
            raise HTTPException(status_code=400, detail=f"prefill only supports steam (got {game['platform']!r})")
        existing = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='prefill' AND game_id=? "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1", (game_id,))
        if existing is not None:
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('prefill', ?, 'steam', 'queued', 'api')", (game_id,))
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='prefill' AND game_id=? "
            "AND state='queued' ORDER BY id DESC LIMIT 1", (game_id,))
        if new_row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("prefill_trigger.queued", game_id=game_id, job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except HTTPException:
        raise
    except PoolError as e:
        _log.error("prefill_trigger.db_unavailable", game_id=game_id, reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
```

Wire in `main.py` (mirror validate_trigger): import `from orchestrator.api.routers.prefill_trigger import router as prefill_trigger_router` and `app.include_router(prefill_trigger_router)`.

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/api/test_prefill_trigger_router.py -q`

---

## Task 6: jobs response model — add `prefill`? (verify)

**Files:**
- Verify only: `src/orchestrator/api/routers/jobs.py:84` `kind` Literal.

- [ ] **Step 1:** `prefill` is ALREADY in the `JobResponse.kind` Literal (it predates this work). Confirm with: `grep -n "prefill" src/orchestrator/api/routers/jobs.py`. If absent, add it (mirrors the UAT-9 `manifest_fetch` fix) and extend `tests/api/test_jobs_router.py::test_job_response_accepts_all_db_job_kinds`. No change expected.

---

## Task 7: Full gate sweep + docs + combined commit + PR

- [ ] **Step 1: Full suite** — `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q` → all green.
- [ ] **Step 2:** `.venv/bin/ruff check src tests` · `.venv/bin/ruff format --check src tests` · `.venv/bin/mypy src` · `gitleaks detect --no-banner --no-git` · `semgrep --config=auto --quiet src/orchestrator/prefill src/orchestrator/jobs/handlers/prefill.py src/orchestrator/api/routers/prefill_trigger.py`.
- [ ] **Step 3: Security audit doc** `docs/security-audits/f5-steam-prefill-security-audit.md` (focus: SSRF/URL-building from validated int+hex onto fixed base_url; bounded concurrency/timeout; no secrets; stat-only readability check). Then `scripts/process-checklist.sh --complete-step build_loop:security_audit`.
- [ ] **Step 4: Docs** — CHANGELOG (Added F5 + the F7 readability fix), FEATURES.md (Feature 18). Then `--complete-step build_loop:documentation_updated`.
- [ ] **Step 5: Build-loop steps** — mark tests_written, tests_verified_failing, implemented (before audit), feature_recorded (after docs); `scripts/test-gate.sh --record-feature "F5-steam-prefill"`.
- [ ] **Step 6:** Confirm A/B/C commit structure with the Orchestrator → combined `feat(f5)` commit → push → PR. User merges. Then UAT (live prefill of Victoria 3's missing chunks → re-validate → cached count rises; confirms the MISS→upstream→cache path).

---

## Self-Review

**Spec coverage:** §2 components all mapped — Settings (T1), F7 readability (T2), downloader (T3), handler+ID5 (T4), trigger (T5), jobs model (T6 verify). Error handling (T3 retry/backoff, T4 failed-status) and testing (each task) covered. ✓

**Placeholder scan:** The T4 note about the `jobs.source` CHECK is a real execution-time verification (pick an allowed value), not a placeholder — the code shows `'prefill'` with an explicit instruction to confirm/fallback to `'scheduler'`. T5 Step 1 says "copy + swap" with the full router shown in Step 3. T6 is a verify-only task. Acceptable. ✓

**Type consistency:** `PrefillResult(chunks_total, chunks_ok, chunks_failed, failures)` consistent across T3/T4. `prefill_chunks(uris, settings, *, on_progress=None)` signature matches handler call + test stubs. `steam_chunk_download_uri` reused in T3/T4. `_LATEST_PER_DEPOT_SQL` imported from disk_stat (exists from F7). ✓
