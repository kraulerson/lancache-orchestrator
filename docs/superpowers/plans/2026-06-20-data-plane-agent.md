# Data-Plane Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the orchestrator's data plane (chunk-puller + cache disk-stat + SteamPrefill runner) into a thin bearer-authed, source-allowlisted HTTP agent on the lancache VM; route the control-plane handlers through it behind an `agent_enabled` flag with zero externally-visible behavior change.

**Architecture:** A new `agent/` FastAPI app (own uvicorn entrypoint) wraps the EXISTING puller/disk-stat/`SteamPrefillDriver` and exposes `/v1/pull`, `/v1/stat`, `/v1/steam/*`. A control-plane `AgentClient` (`clients/agent_client.py`) calls it (POST-then-poll for async ops). The job handlers + `validate_game` gain an `agent_enabled` branch: ON → call `AgentClient`; OFF → call the existing in-process functions unchanged (the equivalence safety net). Async ops use an ephemeral in-memory job registry on the agent; durability is the orchestrator's DB job.

**Tech Stack:** Python 3.12, FastAPI, httpx (async, `MockTransport` test seam), uvicorn, structlog, pytest (`pytest.mark.asyncio`), ruff. Spec: `docs/superpowers/specs/2026-06-20-data-plane-agent-design.md`.

---

## Locked design decisions (read before starting)

- **(a) The agent gets its OWN puller** `agent/puller.py` taking `list[ChunkSpec(url, host)]` + `user_agent`, with the SAME retry/backoff/semaphore shape and the SAME `_build_transport()` test seam as `prefill/downloader.py`. `prefill/downloader.py` and `prefill/epic_downloader.py` stay BYTE-UNCHANGED so the flag-off path and its existing tests are untouched (the equivalence safety net). The later cleanup follow-up (spec §5 step 5) deletes the in-process modules, leaving the agent puller as the sole one — so there is **no permanent duplication**.
- **(b) Flag-OFF path calls the EXISTING in-process functions** verbatim. Existing prefill/validate tests must pass unchanged.
- **(c) Two distinct `PrefillResult` types exist — do not conflate.** `platform/steam/prefill_driver.PrefillResult(ok: bool, raw: str)` (SteamPrefill exit) vs `prefill/downloader.PrefillResult(chunks_total, chunks_ok, chunks_failed, failures)` (chunk pull). The agent puller introduces a THIRD, `agent/puller.PullResult`, field-identical to the downloader's. Keep imports explicit.
- **(d) Epic `verify_cached` (20-chunk HIT sample) stays control-side in ②.** It is a tiny verification, not the main byte-pull; control is still co-located with lancache in ②. Flagged as a step-④ relocation follow-up (it would hairpin once control moves to the LXC). The flag-on Epic path calls `agent_client.pull(...)` for the bulk download but keeps the existing control-side `epic_verify_cached(...)` call.
- **(e) `BearerAuthMiddleware` gains an optional `exempt_paths` kwarg** (default = the existing API `AUTH_EXEMPT_PATHS`), so the agent can pass `{("/v1/health", False)}`. Additive — the API call site is unchanged, API behavior preserved.
- **(f) The agent's boot guard reuses `_detect_non_loopback_bind`** (importable from `api/main.py`) with `agent_bind_host`.

## File Structure

**New files:**
- `src/orchestrator/agent/__init__.py` — package marker.
- `src/orchestrator/agent/jobs.py` — `AgentJobStore`: ephemeral in-memory async job registry (running/done/failed + done/total progress).
- `src/orchestrator/agent/puller.py` — `ChunkSpec`, `PullResult`, `pull_chunks(...)`: platform-agnostic stream-and-discard over `(url, host)` pairs (own `_build_transport()` seam).
- `src/orchestrator/agent/app.py` — `create_agent_app()`: FastAPI app, mounts reused middleware, registers routers, lifespan builds the `SteamPrefillDriver` + `AgentJobStore`.
- `src/orchestrator/agent/__main__.py` — uvicorn entrypoint (`python -m orchestrator.agent`).
- `src/orchestrator/agent/routers/__init__.py` — package marker.
- `src/orchestrator/agent/routers/pull.py` — `POST /v1/pull` (+ `GET /v1/pull/{id}`).
- `src/orchestrator/agent/routers/stat.py` — `POST /v1/stat`.
- `src/orchestrator/agent/routers/steam.py` — `POST /v1/steam/prefill` (+ `GET /v1/steam/prefill/{id}`), `GET /v1/steam/downloaded-state`, `GET /v1/steam/auth-status`.
- `src/orchestrator/agent/routers/health.py` — `GET /v1/health` (exempt, liveness).
- `src/orchestrator/clients/__init__.py` — package marker (if not present).
- `src/orchestrator/clients/agent_client.py` — `AgentClient`, `AgentError`.
- Tests: `tests/agent/__init__.py`, `tests/agent/test_jobs.py`, `tests/agent/test_puller.py`, `tests/agent/test_pull.py`, `tests/agent/test_stat.py`, `tests/agent/test_steam.py`, `tests/agent/test_agent_security.py`, `tests/agent/test_e2e.py`, `tests/clients/__init__.py`, `tests/clients/test_agent_client.py`.

**Modified files:**
- `src/orchestrator/core/settings.py` — add 4 agent settings.
- `src/orchestrator/api/middleware.py` — parametrize `BearerAuthMiddleware.__init__` with `exempt_paths`.
- `src/orchestrator/jobs/worker.py` — `Deps`: add `agent_client`, drop `prefill_driver`'s sole-use status (keep field for flag-off; see Task 9).
- `src/orchestrator/jobs/handlers/prefill.py` — Steam + Epic seams behind `agent_enabled`.
- `src/orchestrator/validator/disk_stat.py` — `validate_game` seam behind `agent_enabled` (the leaf `validate_chunks` stays unchanged).
- `src/orchestrator/api/main.py` — lifespan builds `AgentClient`, injects into `JobsDeps`.
- `src/orchestrator/api/routers/health.py` — `steam_auth_ok` via agent when enabled + new `agent_reachable`.

---

## Task 1: Agent settings

**Files:**
- Modify: `src/orchestrator/core/settings.py` (after the SteamPrefill block, ~line 94)
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/core/test_settings.py` (mirror the existing `TestSteamPrefillSettings` style; `_settings()`/`Settings(orchestrator_token="a"*32)` helper already exists in that file — reuse it):

```python
class TestDataPlaneAgentSettings:
    def test_defaults(self):
        s = Settings(orchestrator_token="a" * 32)
        assert s.agent_enabled is False
        assert s.agent_base_url == "http://127.0.0.1:8780"
        assert s.agent_bind_host == "127.0.0.1"
        assert s.agent_bind_port == 8780

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ORCH_AGENT_ENABLED", "true")
        monkeypatch.setenv("ORCH_AGENT_BASE_URL", "http://10.0.0.5:8780")
        monkeypatch.setenv("ORCH_AGENT_BIND_PORT", "9001")
        s = Settings(orchestrator_token="a" * 32)
        assert s.agent_enabled is True
        assert s.agent_base_url == "http://10.0.0.5:8780"
        assert s.agent_bind_port == 9001
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/core/test_settings.py::TestDataPlaneAgentSettings -v`
Expected: FAIL (`AttributeError`/`ValidationError` — fields don't exist).

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/core/settings.py`, after the SteamPrefill block (the `steam_prefill_config_dir` line ~94), add:

```python
    # --- Data-plane agent (re-architecture step 2) ------------------
    # The data plane (chunk-pull + cache disk-stat + SteamPrefill runner) runs
    # as a separate HTTP service (the agent) on the lancache host. agent_enabled
    # routes the control-plane handlers through it; OFF keeps the in-process path
    # (zero behavior change). agent_base_url is loopback while co-located (step
    # 2) and becomes the LXC->host LAN address in step 4.
    agent_enabled: bool = False
    agent_base_url: str = "http://127.0.0.1:8780"
    agent_bind_host: str = Field(default="127.0.0.1", min_length=1)
    agent_bind_port: int = Field(default=8780, ge=1, le=65535)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/core/test_settings.py::TestDataPlaneAgentSettings -v`
Expected: PASS. Then `.venv/bin/ruff check src/orchestrator/core/settings.py` → clean.

---

## Task 2: Agent job registry (`agent/jobs.py`)

**Files:**
- Create: `src/orchestrator/agent/__init__.py` (empty), `src/orchestrator/agent/jobs.py`
- Create: `tests/agent/__init__.py` (empty), `tests/agent/test_jobs.py`

- [ ] **Step 1: Write the failing test**

`tests/agent/test_jobs.py`:

```python
"""Tests for the agent's ephemeral in-memory job registry."""

from __future__ import annotations

import pytest

from orchestrator.agent.jobs import AgentJobStore

pytestmark = pytest.mark.asyncio


async def test_create_starts_running():
    store = AgentJobStore()
    job_id = store.create()
    snap = store.get(job_id)
    assert snap["state"] == "running"
    assert snap["done"] == 0
    assert snap["total"] == 0


async def test_progress_updates():
    store = AgentJobStore()
    job_id = store.create()
    store.set_progress(job_id, 7, 20)
    snap = store.get(job_id)
    assert (snap["done"], snap["total"]) == (7, 20)
    assert snap["state"] == "running"


async def test_done_carries_result():
    store = AgentJobStore()
    job_id = store.create()
    store.set_done(job_id, {"chunks_ok": 5})
    snap = store.get(job_id)
    assert snap["state"] == "done"
    assert snap["result"] == {"chunks_ok": 5}


async def test_failed_carries_error():
    store = AgentJobStore()
    job_id = store.create()
    store.set_failed(job_id, "boom")
    snap = store.get(job_id)
    assert snap["state"] == "failed"
    assert snap["error"] == "boom"


async def test_unknown_job_is_none():
    assert AgentJobStore().get("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_jobs.py -v`
Expected: FAIL (`ModuleNotFoundError: orchestrator.agent.jobs`).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/__init__.py`: empty file.

`src/orchestrator/agent/jobs.py`:

```python
"""Ephemeral in-memory job registry for the agent's async operations.

Durability lives in the orchestrator's DB job that drives a call; an agent
restart simply loses in-flight jobs (the orchestrator job retries). State is
not persisted by design. Single-event-loop access; the dict ops are atomic
within a coroutine step so no lock is needed for these simple mutations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Job:
    state: str = "running"  # running | done | failed
    done: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}

    def create(self) -> str:
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = _Job()
        return job_id

    def set_progress(self, job_id: str, done: int, total: int) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.done = done
            job.total = total

    def set_done(self, job_id: str, result: dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.state = "done"
            job.result = result

    def set_failed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.state = "failed"
            job.error = error

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "state": job.state,
            "done": job.done,
            "total": job.total,
            "result": job.result,
            "error": job.error,
        }
```

Note: `field` import is unused above — omit it (only `dataclass` is needed). Final import line: `from dataclasses import dataclass`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_jobs.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/jobs.py` → clean.

---

## Task 3: Agent puller (`agent/puller.py`)

**Files:**
- Create: `src/orchestrator/agent/puller.py`
- Create: `tests/agent/test_puller.py`

The puller is a platform-agnostic copy of `prefill/downloader.py`'s loop, taking `(url, host)` specs + a per-batch `user_agent`. Host is set per-request (a batch may be single-platform, but the contract allows mixed). Same `_build_transport()` test seam.

- [ ] **Step 1: Write the failing test**

`tests/agent/test_puller.py`:

```python
"""Tests for the agent's platform-agnostic chunk puller."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.agent.puller import ChunkSpec, pull_chunks
from orchestrator.core.settings import Settings

pytestmark = pytest.mark.asyncio

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token="a" * 32, **kw)


async def _noop_sleep(_seconds):
    return None


async def test_all_ok_sets_host_and_ua_per_request(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x" * 10)

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    specs = [
        ChunkSpec(url=f"/depot/1/chunk/{SHA}", host="lancache.steamcontent.com"),
        ChunkSpec(url="/Builds/x/chunk0", host="epicgames-download1.akamaized.net"),
    ]
    result = await pull_chunks(specs, user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (2, 2, 0)
    assert seen[0].headers["User-Agent"] == "UA/1.0"
    assert seen[0].headers["Host"] == "lancache.steamcontent.com"
    assert seen[1].headers["Host"] == "epicgames-download1.akamaized.net"
    assert str(seen[0].url) == f"http://127.0.0.1/depot/1/chunk/{SHA}"


async def test_4xx_not_retried_recorded(monkeypatch):
    def handler(request):
        return httpx.Response(404)

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    specs = [ChunkSpec(url="/depot/1/chunk/x", host="h")]
    result = await pull_chunks(specs, user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_ok, result.chunks_failed) == (0, 1)
    assert result.failures == [("/depot/1/chunk/x", "http 404")]


async def test_empty_is_zero():
    result = await pull_chunks([], user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (0, 0, 0)


async def test_progress_callback(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x")

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    seen_progress = []
    specs = [ChunkSpec(url=f"/c/{i}", host="h") for i in range(3)]
    await pull_chunks(
        specs, user_agent="UA/1.0", settings=_settings(),
        on_progress=lambda d, t: seen_progress.append((d, t)),
    )
    assert seen_progress[-1] == (3, 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_puller.py -v`
Expected: FAIL (`ModuleNotFoundError: orchestrator.agent.puller`).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/puller.py`:

```python
"""Platform-agnostic chunk puller for the data-plane agent.

Streams each chunk THROUGH the lancache (stream-and-discard) so lancache caches
it. Mirrors prefill/downloader.py's retry/backoff/semaphore loop but takes
explicit (url, host) specs + a per-batch User-Agent, so Steam and Epic collapse
into one puller. `_build_transport()` is the test seam (None -> real network).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_BACKOFFS_SEC = (1.0, 4.0, 16.0)
_FAILURE_CAP = 50


@dataclass(frozen=True)
class ChunkSpec:
    url: str   # relative path joined to lancache_base_url
    host: str  # routing Host header (the spoofed CDN host)


@dataclass
class PullResult:
    chunks_total: int
    chunks_ok: int
    chunks_failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an httpx.MockTransport. None -> real network."""
    return None


def _backoff(attempt: int) -> float:
    return _BACKOFFS_SEC[min(attempt, len(_BACKOFFS_SEC) - 1)]


async def pull_chunks(
    specs: list[ChunkSpec],
    *,
    user_agent: str,
    settings: Settings,
    concurrency: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> PullResult:
    """GET each spec through lancache, streaming + discarding the body."""
    total = len(specs)
    if total == 0:
        return PullResult(0, 0, 0)

    sem = asyncio.Semaphore(concurrency or settings.chunk_concurrency)
    timeout = httpx.Timeout(settings.prefill_chunk_timeout_sec, connect=10.0)
    max_attempts = settings.prefill_chunk_max_attempts

    done = 0
    ok = 0
    failures: list[tuple[str, str]] = []
    lock = asyncio.Lock()

    transport = _build_transport()
    client_kwargs: dict[str, Any] = {
        "base_url": settings.lancache_base_url,
        "timeout": timeout,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def record(url: str, reason: str | None) -> None:
            nonlocal done, ok
            async with lock:
                done += 1
                if reason is None:
                    ok += 1
                else:
                    failures.append((url, reason))
                if on_progress is not None:
                    on_progress(done, total)

        async def fetch(spec: ChunkSpec) -> None:
            headers = {"User-Agent": user_agent, "Host": spec.host}
            reason = "unknown"
            for attempt in range(max_attempts):
                try:
                    async with client.stream("GET", spec.url, headers=headers) as resp:
                        if 200 <= resp.status_code < 300:
                            async for _ in resp.aiter_bytes():
                                pass  # stream + discard
                            await record(spec.url, None)
                            return
                        reason = f"http {resp.status_code}"
                        if resp.status_code < 500:
                            break
                except httpx.RequestError as e:
                    reason = type(e).__name__
                if attempt < max_attempts - 1:
                    await asyncio.sleep(_backoff(attempt))
            await record(spec.url, reason)

        async def guarded(spec: ChunkSpec) -> None:
            async with sem:
                await fetch(spec)

        await asyncio.gather(*(guarded(s) for s in specs))

    return PullResult(
        chunks_total=total,
        chunks_ok=ok,
        chunks_failed=total - ok,
        failures=failures[:_FAILURE_CAP],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_puller.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/puller.py` → clean.

---

## Task 4: Agent app skeleton + `/v1/health` + `/v1/pull`

**Files:**
- Create: `src/orchestrator/agent/app.py`, `src/orchestrator/agent/routers/__init__.py`, `src/orchestrator/agent/routers/health.py`, `src/orchestrator/agent/routers/pull.py`
- Test: `tests/agent/test_pull.py`

This task builds `create_agent_app()` WITHOUT auth middleware yet (added in Task 6) so the route logic is testable in isolation. The app holds `app.state.agent_jobs` (an `AgentJobStore`) and `app.state.settings`.

The `/v1/pull` SSRF guard `_validate_pull_url`: reject any `url` that is empty, does not start with `/`, starts with `//`, contains `://`, contains `..`, or contains an `@`. (Relative-path-only; joined to the agent's fixed `lancache_base_url`.)

- [ ] **Step 1: Write the failing test**

`tests/agent/test_pull.py`:

```python
"""Tests for the agent /v1/pull endpoint."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token="a" * 32, **kw)


def _client(monkeypatch, handler) -> TestClient:
    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    app = create_agent_app(settings=_settings())
    return TestClient(app)


def test_pull_runs_to_done(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * 8)

    client = _client(monkeypatch, handler)
    resp = client.post(
        "/v1/pull",
        json={
            "chunks": [{"url": f"/depot/1/chunk/{SHA}", "host": "lancache.steamcontent.com"}],
            "user_agent": "UA/1.0",
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    # Poll until done (TestClient runs the background task on the same loop;
    # poll a few times to let it complete).
    for _ in range(50):
        snap = client.get(f"/v1/pull/{job_id}").json()
        if snap["state"] == "done":
            break
    assert snap["state"] == "done"
    assert snap["result"]["chunks_ok"] == 1
    assert snap["result"]["chunks_failed"] == 0


@pytest.mark.parametrize(
    "bad_url",
    ["http://evil.com/x", "//evil.com/x", "/depot/../../etc/passwd", "user@host/x", ""],
)
def test_pull_rejects_ssrf_urls(monkeypatch, bad_url):
    def handler(request):  # must never be called
        raise AssertionError("transport must not be hit for a rejected URL")

    client = _client(monkeypatch, handler)
    resp = client.post(
        "/v1/pull",
        json={"chunks": [{"url": bad_url, "host": "h"}], "user_agent": "UA/1.0"},
    )
    assert resp.status_code == 400


def test_pull_unknown_job_404(monkeypatch):
    def handler(request):
        return httpx.Response(200)

    client = _client(monkeypatch, handler)
    assert client.get("/v1/pull/nope").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_pull.py -v`
Expected: FAIL (`ModuleNotFoundError: orchestrator.agent.app`).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/routers/__init__.py`: empty file.

`src/orchestrator/agent/routers/health.py`:

```python
"""Agent liveness endpoint (auth-exempt)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
```

`src/orchestrator/agent/routers/pull.py`:

```python
"""Agent /v1/pull — platform-agnostic chunk puller (async job + poll)."""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from orchestrator.agent.puller import ChunkSpec, pull_chunks

_log = structlog.get_logger(__name__)

router = APIRouter()


class _ChunkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    host: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunks: list[_ChunkIn]
    user_agent: str
    concurrency: int | None = None


def _validate_pull_url(url: str) -> None:
    """Anti-SSRF: relative-path-only. The agent joins this to its fixed
    lancache_base_url; the Host header is the only routing input."""
    if (
        not url
        or not url.startswith("/")
        or url.startswith("//")
        or "://" in url
        or ".." in url
        or "@" in url
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid chunk url")


@router.post("/v1/pull", status_code=status.HTTP_202_ACCEPTED)
async def start_pull(body: PullRequest, request: Request) -> dict[str, str]:
    for c in body.chunks:
        _validate_pull_url(c.url)
    specs = [ChunkSpec(url=c.url, host=c.host) for c in body.chunks]
    store = request.app.state.agent_jobs
    settings = request.app.state.settings
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await pull_chunks(
                specs,
                user_agent=body.user_agent,
                settings=settings,
                concurrency=body.concurrency,
                on_progress=lambda d, t: store.set_progress(job_id, d, t),
            )
            store.set_done(
                job_id,
                {
                    "chunks_total": result.chunks_total,
                    "chunks_ok": result.chunks_ok,
                    "chunks_failed": result.chunks_failed,
                    "failures": result.failures,
                },
            )
        except Exception as e:  # noqa: BLE001 — record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    asyncio.create_task(_run())
    return {"job_id": job_id}


@router.get("/v1/pull/{job_id}")
async def get_pull(job_id: str, request: Request) -> Response | dict:
    snap = request.app.state.agent_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap
```

`src/orchestrator/agent/app.py`:

```python
"""The data-plane agent FastAPI app. Wraps the existing puller / disk-stat /
SteamPrefillDriver and exposes them over HTTP. Runs on the lancache host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from orchestrator.agent.jobs import AgentJobStore
from orchestrator.agent.routers import health, pull
from orchestrator.core.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_agent_app(*, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.agent_jobs = AgentJobStore()
        yield

    app = FastAPI(title="lancache-orchestrator data-plane agent", lifespan=_lifespan)
    # Attach eagerly too, so TestClient routes work before lifespan in some paths.
    app.state.settings = settings
    app.state.agent_jobs = AgentJobStore()
    app.include_router(health.router)
    app.include_router(pull.router)
    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_pull.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/` → clean.

Note: if `enforce-context7` blocks the FastAPI/pydantic import edits, run `resolve-library-id fastapi` + `query-docs` first (already researched this project — markers likely exist).

---

## Task 5: Agent `/v1/stat`

**Files:**
- Create: `src/orchestrator/agent/routers/stat.py`
- Modify: `src/orchestrator/agent/app.py` (register the router)
- Test: `tests/agent/test_stat.py`

The agent builds paths from its OWN `settings.lancache_nginx_cache_path` + `settings.cache_levels` via `cache_path`, then runs the UNCHANGED `validate_chunks`. Returns `{cached, missing, errors}`. (`validate_chunks` returns `(cached, missing)`; the agent computes `errors` as the per-batch error tally — but the existing `validate_chunks` aggregates errors into `missing` and only logs the count. To surface `errors`, the agent re-derives it: re-run is wasteful, so instead expose `errors=0` placeholder is WRONG. Resolution: the stat router calls `validate_chunks` for `(cached, missing)` and reports `errors` as part of `missing`'s breakdown is not available. Keep the contract simple: return `{cached, missing}` and OMIT `errors` from the response — `validate_chunks` already folds stat-errors into `missing` and WARN-logs them, matching today's `validate_game` behavior exactly.)

> Decision: the `/v1/stat` response is `{"cached": int, "missing": int}` (matching `validate_chunks`'s return), NOT `{cached, missing, errors}`. This keeps behavior byte-identical to today's `validate_game` (which only consumes `(cached, missing)`). The spec's `errors` mention is dropped as YAGNI — errors are already folded into `missing` + WARN-logged inside `validate_chunks`.

- [ ] **Step 1: Write the failing test**

`tests/agent/test_stat.py`:

```python
"""Tests for the agent /v1/stat endpoint."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import cache_path


def _settings(cache_root: Path, **kw) -> Settings:
    return Settings(
        orchestrator_token="a" * 32,
        lancache_nginx_cache_path=cache_root,
        cache_levels="2:2",
        **kw,
    )


def _make_cached_file(cache_root: Path, h: str) -> None:
    p = cache_path(cache_root, h, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"cached-bytes")  # size>0, owner-read bit set by default


def test_stat_counts_cached_and_missing(tmp_path):
    cached_h = hashlib.md5(b"present").hexdigest()
    missing_h = hashlib.md5(b"absent").hexdigest()
    _make_cached_file(tmp_path, cached_h)

    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/v1/stat", json={"hashes": [cached_h, missing_h]})
    assert resp.status_code == 200
    assert resp.json() == {"cached": 1, "missing": 1}


def test_stat_rejects_non_hex_hash(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/v1/stat", json={"hashes": ["not-a-32-hex-hash"]})
    assert resp.status_code == 400


def test_stat_empty(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/v1/stat", json={"hashes": []})
    assert resp.json() == {"cached": 0, "missing": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_stat.py -v`
Expected: FAIL (404 — route not registered).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/routers/stat.py`:

```python
"""Agent /v1/stat — cache disk-stat over control-supplied cache-key hashes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from orchestrator.validator.cache_key import cache_path
from orchestrator.validator.disk_stat import validate_chunks

router = APIRouter()

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class StatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hashes: list[str]


@router.post("/v1/stat")
async def stat(body: StatRequest, request: Request) -> dict[str, int]:
    for h in body.hashes:
        if not _HEX32.match(h):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cache-key hash"
            )
    settings = request.app.state.settings
    cache_root = Path(settings.lancache_nginx_cache_path)
    levels = settings.cache_levels
    paths = [cache_path(cache_root, h, levels) for h in body.hashes]
    cached, missing = await validate_chunks(paths)
    return {"cached": cached, "missing": missing}
```

In `src/orchestrator/agent/app.py`, add `stat` to the import and `app.include_router(stat.router)`:

```python
from orchestrator.agent.routers import health, pull, stat
...
    app.include_router(pull.router)
    app.include_router(stat.router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_stat.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/` → clean.

---

## Task 6: Agent `/v1/steam/*`

**Files:**
- Create: `src/orchestrator/agent/routers/steam.py`
- Modify: `src/orchestrator/agent/app.py` (build a `SteamPrefillDriver` in lifespan; register router)
- Test: `tests/agent/test_steam.py`

The lifespan builds `SteamPrefillDriver(binary=settings.steam_prefill_binary, config_dir=settings.steam_prefill_config_dir)` and attaches it as `app.state.prefill_driver`. The test injects a fake driver via `app.state.prefill_driver` override.

- [ ] **Step 1: Write the failing test**

`tests/agent/test_steam.py`:

```python
"""Tests for the agent /v1/steam/* endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.platform.steam.prefill_driver import PrefillResult, SteamAuthStatus


class _FakeDriver:
    def __init__(self):
        self.calls = []

    async def prefill_apps(self, app_ids, *, force=False):
        self.calls.append((app_ids, force))
        return PrefillResult(ok=True, raw="OK done")

    def downloaded_state(self):
        return {440: [111, 222]}

    def auth_status(self):
        return SteamAuthStatus(ok=True)


def _client(driver) -> TestClient:
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.prefill_driver = driver
    return TestClient(app)


def test_steam_prefill_runs_to_done():
    driver = _FakeDriver()
    client = _client(driver)
    resp = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/steam/prefill/{job_id}").json()
        if snap["state"] == "done":
            break
    assert snap["state"] == "done"
    assert snap["result"] == {"ok": True, "raw": "OK done"}
    assert driver.calls == [([440], False)]


def test_downloaded_state():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/downloaded-state")
    assert resp.status_code == 200
    assert resp.json() == {"440": [111, 222]}


def test_auth_status():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/auth-status")
    assert resp.json() == {"ok": True, "reason": ""}


def test_steam_prefill_rejects_negative_app_id():
    client = _client(_FakeDriver())
    resp = client.post("/v1/steam/prefill", json={"app_ids": [-5], "force": False})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_steam.py -v`
Expected: FAIL (404 — routes not registered).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/routers/steam.py`:

```python
"""Agent /v1/steam/* — drives the host SteamPrefill binary via SteamPrefillDriver."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class SteamPrefillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_ids: list[int] = Field(..., min_length=1)
    force: bool = False

    @classmethod
    def _no_negatives(cls, v: list[int]) -> list[int]:
        if any(a < 0 for a in v):
            raise ValueError("app_ids must be non-negative")
        return v


def _validate_app_ids(app_ids: list[int]) -> None:
    if any(a < 0 for a in app_ids):
        raise HTTPException(status_code=422, detail="app_ids must be non-negative")


@router.post("/v1/steam/prefill", status_code=status.HTTP_202_ACCEPTED)
async def start_prefill(body: SteamPrefillRequest, request: Request) -> dict[str, str]:
    _validate_app_ids(body.app_ids)
    driver = request.app.state.prefill_driver
    store = request.app.state.agent_jobs
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await driver.prefill_apps(body.app_ids, force=body.force)
            store.set_done(job_id, {"ok": result.ok, "raw": result.raw})
        except Exception as e:  # noqa: BLE001
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    asyncio.create_task(_run())
    return {"job_id": job_id}


@router.get("/v1/steam/prefill/{job_id}")
async def get_prefill(job_id: str, request: Request) -> dict:
    snap = request.app.state.agent_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap


@router.get("/v1/steam/downloaded-state")
async def downloaded_state(request: Request) -> dict[str, list[int]]:
    state = request.app.state.prefill_driver.downloaded_state()
    return {str(k): v for k, v in state.items()}


@router.get("/v1/steam/auth-status")
async def auth_status(request: Request) -> dict:
    st = request.app.state.prefill_driver.auth_status()
    return {"ok": st.ok, "reason": st.reason}
```

(Remove the unused `_no_negatives` classmethod from the model — keep the model minimal; the `_validate_app_ids` guard returns 422 explicitly. Final model has only `app_ids`/`force` + `model_config`.)

In `src/orchestrator/agent/app.py`, build the driver in lifespan and register the router:

```python
from orchestrator.agent.routers import health, pull, stat, steam
from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver
...
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.agent_jobs = AgentJobStore()
        app.state.prefill_driver = SteamPrefillDriver(
            binary=settings.steam_prefill_binary,
            config_dir=settings.steam_prefill_config_dir,
        )
        yield
    ...
    # eager attach (mirror lifespan for TestClient paths that read state pre-lifespan)
    app.state.prefill_driver = SteamPrefillDriver(
        binary=settings.steam_prefill_binary,
        config_dir=settings.steam_prefill_config_dir,
    )
    app.include_router(stat.router)
    app.include_router(steam.router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_steam.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/` → clean.

---

## Task 7: Parametrize `BearerAuthMiddleware` + agent security wiring

**Files:**
- Modify: `src/orchestrator/api/middleware.py` (`BearerAuthMiddleware.__init__`)
- Modify: `src/orchestrator/agent/app.py` (mount middleware + boot guard)
- Test: `tests/agent/test_agent_security.py`, plus re-run the existing API middleware tests (must stay green).

`BearerAuthMiddleware` gains `exempt_paths` (default `None` → use module `AUTH_EXEMPT_PATHS`). The agent passes `exempt_paths={("/v1/health", False)}`. The agent app also mounts `SourceAllowlistMiddleware` (reused as-is) and runs an agent boot guard reusing `_detect_non_loopback_bind`.

- [ ] **Step 1: Write the failing test**

`tests/agent/test_agent_security.py`:

```python
"""Agent auth + allowlist + boot-guard tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import _enforce_agent_lan_bind_policy, create_agent_app
from orchestrator.core.settings import Settings

TOKEN = "a" * 32


def _app(**settings_kw):
    return create_agent_app(settings=Settings(orchestrator_token=TOKEN, **settings_kw))


def test_health_is_exempt():
    client = TestClient(_app())
    assert client.get("/v1/health").status_code == 200


def test_pull_requires_bearer():
    client = TestClient(_app())
    resp = client.post(
        "/v1/pull", json={"chunks": [], "user_agent": "UA/1.0"}
    )
    assert resp.status_code == 401


def test_pull_accepts_valid_bearer():
    client = TestClient(_app())
    resp = client.post(
        "/v1/pull",
        json={"chunks": [], "user_agent": "UA/1.0"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 202


def test_boot_guard_refuses_non_loopback_without_allowlist():
    s = Settings(orchestrator_token=TOKEN, agent_bind_host="0.0.0.0")
    with pytest.raises(SystemExit):
        _enforce_agent_lan_bind_policy(s)


def test_boot_guard_allows_non_loopback_with_allowlist(monkeypatch):
    monkeypatch.setenv("ORCH_ALLOWED_SOURCE_IPS", "10.0.0.0/24")
    s = Settings(orchestrator_token=TOKEN, agent_bind_host="0.0.0.0")
    _enforce_agent_lan_bind_policy(s)  # must NOT raise
```

(Source-allowlist 403 behavior is covered by the existing `SourceAllowlistMiddleware` tests for the API; the agent reuses the identical middleware, so we don't re-test the allowlist internals here — just bearer + boot guard.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_agent_security.py -v`
Expected: FAIL (`ImportError: _enforce_agent_lan_bind_policy` / 200 where 401 expected — middleware not mounted).

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/api/middleware.py`, change `BearerAuthMiddleware.__init__` and the exempt-path loop to use an instance attribute:

```python
class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, exempt_paths=None) -> None:
        self.app = app
        self._exempt_paths = exempt_paths if exempt_paths is not None else AUTH_EXEMPT_PATHS
```

Then in `__call__`, replace `for exempt_path, allow_subpaths in AUTH_EXEMPT_PATHS:` with `for exempt_path, allow_subpaths in self._exempt_paths:`. (No other change — API call site passes no `exempt_paths`, so it keeps `AUTH_EXEMPT_PATHS`; behavior identical.)

In `src/orchestrator/agent/app.py`, add the boot guard + mount the two middlewares:

```python
import os
import sys

import structlog

from orchestrator.api.main import _detect_non_loopback_bind
from orchestrator.api.middleware import BearerAuthMiddleware, SourceAllowlistMiddleware

_AGENT_EXEMPT_PATHS = {("/v1/health", False)}


def _enforce_agent_lan_bind_policy(settings: Settings) -> None:
    """Fail-closed: a non-loopback agent bind MUST declare ORCH_ALLOWED_SOURCE_IPS."""
    log = structlog.get_logger()
    bind_signal = _detect_non_loopback_bind(settings.agent_bind_host)
    if bind_signal is None:
        return
    if not settings.allowed_source_ips:
        log.critical(
            "agent.boot.lan_bind_without_allowlist",
            agent_bind_host=bind_signal,
            hint="Set ORCH_ALLOWED_SOURCE_IPS before binding the agent off-loopback.",
        )
        raise SystemExit(1)
    log.info("agent.boot.lan_bind_gated", agent_bind_host=bind_signal)
```

> NOTE: `_detect_non_loopback_bind` also inspects `UVICORN_HOST` and `--host` argv. For the agent test `test_boot_guard_refuses_non_loopback_without_allowlist`, the `agent_bind_host="0.0.0.0"` signal alone triggers it. (If a stray `--host` loopback arg in the test runner interferes, the `agent_bind_host` non-loopback value is returned first, so the guard still fires.)

In `create_agent_app`, after registering routers, add the middleware (FastAPI applies middleware in reverse-add order; add allowlist first so bearer runs inside it — match the API's relative order where SourceAllowlist is outer of BearerAuth):

```python
    app.add_middleware(BearerAuthMiddleware, exempt_paths=_AGENT_EXEMPT_PATHS)
    app.add_middleware(SourceAllowlistMiddleware)
    return app
```

(`add_middleware` wraps outermost-last: adding BearerAuth then SourceAllowlist makes SourceAllowlist the OUTER layer — requests hit allowlist first, then bearer — matching the API's ordering.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_agent_security.py -v`
Then re-run the existing API middleware tests to prove the parametrization didn't regress:
Run: `.venv/bin/python -m pytest tests/api/ -k "auth or middleware or allowlist" -v`
Expected: BOTH PASS. `.venv/bin/ruff check src/orchestrator/api/middleware.py src/orchestrator/agent/` → clean.

---

## Task 8: Agent entrypoint `__main__.py`

**Files:**
- Create: `src/orchestrator/agent/__main__.py`
- Test: covered by import + a tiny smoke assertion in `tests/agent/test_agent_security.py` extension (Step 1 below) OR a dedicated test.

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_agent_security.py`:

```python
def test_main_module_exposes_app_factory():
    import orchestrator.agent.__main__ as m

    assert hasattr(m, "main")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_agent_security.py::test_main_module_exposes_app_factory -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/agent/__main__.py`:

```python
"""uvicorn entrypoint for the data-plane agent: `python -m orchestrator.agent`."""

from __future__ import annotations

import uvicorn

from orchestrator.agent.app import _enforce_agent_lan_bind_policy, create_agent_app
from orchestrator.core.settings import get_settings


def main() -> None:
    settings = get_settings()
    _enforce_agent_lan_bind_policy(settings)
    app = create_agent_app(settings=settings)
    uvicorn.run(app, host=settings.agent_bind_host, port=settings.agent_bind_port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_agent_security.py::test_main_module_exposes_app_factory -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/agent/__main__.py` → clean.

---

## Task 9: `AgentClient` (control-plane HTTP client)

**Files:**
- Create: `src/orchestrator/clients/__init__.py` (if missing), `src/orchestrator/clients/agent_client.py`
- Test: `tests/clients/__init__.py` (empty), `tests/clients/test_agent_client.py`

`AgentClient` wraps the agent over httpx; `pull()`/`steam_prefill()` POST-then-poll until terminal; `stat()`/`downloaded_state()`/`auth_status()` are single calls. `AgentError` on unreachable/401/non-2xx. Poll interval is short and configurable (default 0.5s); tests inject a `MockTransport` and a no-op sleep.

- [ ] **Step 1: Write the failing test**

`tests/clients/test_agent_client.py`:

```python
"""Tests for the control-plane AgentClient."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.clients.agent_client import AgentClient, AgentError

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


def _client(handler) -> AgentClient:
    transport = httpx.MockTransport(handler)
    return AgentClient(
        base_url="http://agent:8780",
        token=TOKEN,
        transport=transport,
        poll_interval_sec=0.0,
    )


async def test_pull_posts_then_polls_to_done():
    state = {"polls": 0}

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "POST" and request.url.path == "/v1/pull":
            return httpx.Response(202, json={"job_id": "j1"})
        if request.url.path == "/v1/pull/j1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"state": "running", "done": 1, "total": 2})
            return httpx.Response(
                200,
                json={"state": "done", "result": {"chunks_ok": 2, "chunks_failed": 0}},
            )
        raise AssertionError(request.url.path)

    client = _client(handler)
    result = await client.pull(
        [{"url": "/depot/1/chunk/x", "host": "h"}], user_agent="UA/1.0"
    )
    assert result["chunks_ok"] == 2


async def test_steam_prefill_polls_to_done():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "s1"})
        return httpx.Response(200, json={"state": "done", "result": {"ok": True, "raw": "x"}})

    client = _client(handler)
    result = await client.steam_prefill([440], force=False)
    assert result["ok"] is True


async def test_stat_single_call():
    def handler(request):
        assert request.url.path == "/v1/stat"
        return httpx.Response(200, json={"cached": 3, "missing": 1})

    client = _client(handler)
    assert await client.stat(["a" * 32]) == {"cached": 3, "missing": 1}


async def test_auth_status_single_call():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "reason": ""})

    client = _client(handler)
    assert (await client.auth_status())["ok"] is True


async def test_unreachable_raises_agent_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_401_raises_agent_error():
    def handler(request):
        return httpx.Response(401)

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_failed_job_raises_agent_error():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"state": "failed", "error": "boom"})

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.pull([{"url": "/x", "host": "h"}], user_agent="UA/1.0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/clients/test_agent_client.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

`src/orchestrator/clients/__init__.py`: empty (create if missing).

`src/orchestrator/clients/agent_client.py`:

```python
"""Control-plane HTTP client for the data-plane agent.

POST-then-poll for async ops (pull, steam_prefill); single call for stat /
downloaded_state / auth_status. Raises AgentError on transport failure, non-2xx,
or a failed agent job — the handlers catch it to fail a job cleanly (never a
crash-loop)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)


class AgentError(RuntimeError):
    """The agent was unreachable, returned an error, or its job failed."""


class AgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_sec: float = 0.5,
        timeout_sec: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        self._poll = poll_interval_sec
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0)

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._headers,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            async with self._new_client() as client:
                resp = await client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise AgentError(f"agent unreachable: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise AgentError(f"agent returned {resp.status_code} for {path}")
        return resp

    async def _post_then_poll(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", path, json=payload)
        job_id = resp.json()["job_id"]
        poll_path = f"{path}/{job_id}"
        while True:
            snap = (await self._request("GET", poll_path)).json()
            state = snap.get("state")
            if state == "done":
                return snap.get("result") or {}
            if state == "failed":
                raise AgentError(f"agent job failed: {snap.get('error')}")
            await asyncio.sleep(self._poll)

    async def pull(
        self, chunks: list[dict[str, str]], *, user_agent: str, concurrency: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chunks": chunks, "user_agent": user_agent}
        if concurrency is not None:
            payload["concurrency"] = concurrency
        return await self._post_then_poll("/v1/pull", payload)

    async def steam_prefill(self, app_ids: list[int], *, force: bool = False) -> dict[str, Any]:
        return await self._post_then_poll(
            "/v1/steam/prefill", {"app_ids": app_ids, "force": force}
        )

    async def stat(self, hashes: list[str]) -> dict[str, int]:
        return (await self._request("POST", "/v1/stat", json={"hashes": hashes})).json()

    async def downloaded_state(self) -> dict[str, list[int]]:
        return (await self._request("GET", "/v1/steam/downloaded-state")).json()

    async def auth_status(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/steam/auth-status")).json()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/clients/test_agent_client.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/clients/` → clean.

---

## Task 10: `Deps.agent_client` + Steam prefill seam

**Files:**
- Modify: `src/orchestrator/jobs/worker.py` (`Deps`)
- Modify: `src/orchestrator/jobs/handlers/prefill.py` (`_steam_prefill` / `_steam_prefill_inner`)
- Test: `tests/jobs/test_prefill_handler.py`

`Deps` gains `agent_client: AgentClient | None = None` (keep `prefill_driver` for the flag-off path). The Steam handler branches on `get_settings().agent_enabled`: ON → `agent_client.steam_prefill([app_id_int], force=force)` returning `{"ok", "raw"}`; OFF → existing `prefill_driver.prefill_apps(...)`. The rest of `_steam_prefill_inner` (failure marking, validate enqueue, F8 cached_version) is unchanged — it reads `ok`/`raw` from either source.

- [ ] **Step 1: Write the failing test**

Add to `tests/jobs/test_prefill_handler.py` (reuse the file's existing fakes/fixtures; if it has a `_Deps`/fake-pool helper, follow it). Two new tests:

```python
async def test_steam_prefill_uses_agent_when_enabled(monkeypatch):
    # agent_enabled=True -> handler calls agent_client.steam_prefill, NOT the driver
    from orchestrator.core import settings as settings_mod

    calls = {"agent": [], "driver": []}

    class _FakeAgent:
        async def steam_prefill(self, app_ids, *, force=False):
            calls["agent"].append((app_ids, force))
            return {"ok": True, "raw": "ok"}

    class _FakeDriver:
        async def prefill_apps(self, app_ids, *, force=False):
            calls["driver"].append((app_ids, force))
            raise AssertionError("driver must not be called when agent_enabled")

    monkeypatch.setattr(
        settings_mod, "get_settings",
        lambda: settings_mod.Settings(orchestrator_token="a" * 32, agent_enabled=True),
    )
    # ALSO patch the get_settings imported into the handler module:
    monkeypatch.setattr(
        "orchestrator.jobs.handlers.prefill.get_settings",
        lambda: settings_mod.Settings(orchestrator_token="a" * 32, agent_enabled=True),
    )
    # ... construct deps with agent_client=_FakeAgent(), prefill_driver=_FakeDriver(),
    #     a fake pool returning a steam game row, then call prefill_handler with a
    #     steam job and assert calls["agent"] == [([<app_id>], False)] and the same
    #     DB writes (status, validate enqueue, cached_version) as the flag-off path.
```

> The implementer fills the deps/pool construction by mirroring the FILE'S EXISTING steam-prefill happy-path test (there is already a passing `_steam_prefill` test in this file from ① — copy its harness, flip `agent_enabled`, swap the driver for `agent_client`). The key assertions: (1) the agent path is taken, (2) the driver is NOT called, (3) the resulting DB writes are identical to the flag-off test.

Also add the explicit equivalence assertion that the EXISTING flag-off steam test still passes unchanged (it should — `agent_enabled` defaults False).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/jobs/test_prefill_handler.py -k "agent" -v`
Expected: FAIL (`Deps` has no `agent_client`, or the handler always calls the driver).

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/jobs/worker.py`, extend `Deps`:

```python
@dataclass(frozen=True, slots=True)
class Deps:
    pool: Pool
    steam_client: SteamWorkerClient | None
    epic_client: EpicClient | None = None
    prefill_driver: SteamPrefillDriver | None = None
    agent_client: AgentClient | None = None
```

Add the import under `TYPE_CHECKING` (matching the file's existing pattern):

```python
    from orchestrator.clients.agent_client import AgentClient
```

In `src/orchestrator/jobs/handlers/prefill.py`, change `_steam_prefill` to require EITHER an agent (when enabled) or the driver, and `_steam_prefill_inner` to branch. Replace the `if deps.prefill_driver is None:` guard in `_steam_prefill` with:

```python
    settings = get_settings()
    if settings.agent_enabled:
        if deps.agent_client is None:
            raise RuntimeError("agent_client is required when agent_enabled")
    elif deps.prefill_driver is None:
        raise RuntimeError("prefill_driver is required for prefill handler")
```

Keep passing both into `_steam_prefill_inner` (signature gains `agent_enabled`). In `_steam_prefill_inner`, replace the single `result = await prefill_driver.prefill_apps(...)` line with:

```python
    if agent_enabled:
        agent_result = await deps.agent_client.steam_prefill([app_id_int], force=force)
        ok = bool(agent_result["ok"])
        raw = str(agent_result.get("raw", ""))
    else:
        driver_result = await prefill_driver.prefill_apps([app_id_int], force=force)
        ok = driver_result.ok
        raw = driver_result.raw
```

Then change the downstream `if not result.ok:` to `if not ok:` and `result.raw[-150:]` to `raw[-150:]`. Pass `agent_enabled=settings.agent_enabled` from `_steam_prefill` into `_steam_prefill_inner`, and `deps` is already passed (use `deps.agent_client`). Keep `prefill_driver` param optional/nullable.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/jobs/test_prefill_handler.py -v`
Expected: PASS (both new agent tests AND all existing steam tests — flag-off identical). `.venv/bin/ruff check src/orchestrator/jobs/` → clean.

---

## Task 11: Epic prefill seam

**Files:**
- Modify: `src/orchestrator/jobs/handlers/prefill.py` (`_epic_prefill_inner`)
- Test: `tests/jobs/test_prefill_handler.py`

When `agent_enabled`, the Epic path builds `{url, host}` specs from the manifest chunks (`url = _full_path(cdn_base, epic_chunk_path(chunk, version))`, `host = cdn_host`) and calls `agent_client.pull(specs, user_agent=settings.epic_user_agent)` returning `{chunks_total, chunks_ok, chunks_failed, failures}` — same shape as `EpicPrefillResult`. `epic_verify_cached(...)` stays control-side (decision (d)). Flag-off path unchanged.

> The `_full_path` join currently lives in `epic_downloader.py` (`_full_path(cdn_base_path, chunk_path)`). Import it for reuse: `from orchestrator.prefill.epic_downloader import _full_path` — it is a stable pure helper. (Acceptable to import a `_`-prefixed helper within the same package; alternatively inline `f"{cdn_base.rstrip('/')}/{p}"`.)

- [ ] **Step 1: Write the failing test**

Add to `tests/jobs/test_prefill_handler.py`, mirroring the existing Epic happy-path test's harness (fake `epic_client.fetch_manifest`, fake pool):

```python
async def test_epic_prefill_uses_agent_pull_when_enabled(monkeypatch):
    # agent_enabled -> _epic_prefill_inner calls agent_client.pull with
    # {url,host} specs (url = cdn_base + epic_chunk_path), NOT epic_prefill_chunks.
    # Assert: agent.pull called with the right specs+UA; epic_verify_cached still
    # called control-side; same DB writes (up_to_date, cached_version) as flag-off.
    ...
```

> The implementer copies the existing Epic test's manifest/pool fakes, sets `agent_enabled=True` (patch `orchestrator.jobs.handlers.prefill.get_settings`), provides a `_FakeAgent` whose `pull` records the specs and returns `{"chunks_total": N, "chunks_ok": N, "chunks_failed": 0, "failures": []}`, and asserts the bulk pull went through the agent while `epic_verify_cached` (monkeypatched to return 1.0) was still called. Assert the final `games` row writes match the flag-off path.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/jobs/test_prefill_handler.py -k "epic and agent" -v`
Expected: FAIL (Epic path always calls `epic_prefill_chunks`).

- [ ] **Step 3: Write minimal implementation**

In `_epic_prefill_inner`, replace the single `result = await epic_prefill_chunks(paths, cdn_host, cdn_base, settings)` with a branch. Keep `paths` (the list of dedup'd `epic_chunk_path` strings) as-is for `verify_cached`. Add at the top of the file's imports: `from orchestrator.prefill.epic_downloader import _full_path`. Then:

```python
    settings = get_settings()
    if settings.agent_enabled:
        if deps.agent_client is None:
            raise RuntimeError("agent_client is required when agent_enabled")
        specs = [{"url": _full_path(cdn_base, p), "host": cdn_host} for p in paths]
        result_d = await deps.agent_client.pull(specs, user_agent=settings.epic_user_agent)
        chunks_total = result_d["chunks_total"]
        chunks_ok = result_d["chunks_ok"]
        chunks_failed = result_d["chunks_failed"]
        failures = result_d.get("failures", [])
    else:
        result = await epic_prefill_chunks(paths, cdn_host, cdn_base, settings)
        chunks_total = result.chunks_total
        chunks_ok = result.chunks_ok
        chunks_failed = result.chunks_failed
        failures = result.failures
```

Then replace the downstream `result.chunks_failed`/`result.chunks_total`/`result.failures` references with the local `chunks_failed`/`chunks_total`/`failures` variables. `epic_verify_cached(paths[:20], cdn_host, cdn_base, settings)` stays exactly as-is (decision (d)). `chunks_ok` is logged in the completion log line (currently `result.chunks_total`); keep using `chunks_total` there.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/jobs/test_prefill_handler.py -v`
Expected: PASS (new Epic-agent test + all existing Epic tests unchanged). `.venv/bin/ruff check src/orchestrator/jobs/` → clean.

---

## Task 12: `validate_game` seam

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py` (`validate_game` only — `validate_chunks`/`_stat_batch` UNCHANGED)
- Test: `tests/validator/test_disk_stat.py` (add agent-path tests; existing tests stay green)

When `agent_enabled`, after computing the per-chunk cache-key hashes (the same `cache_key(identifier, uri, slice_range)` compute), `validate_game` calls `deps.agent_client.stat(hashes)` for `{cached, missing}` instead of building `cache_path` paths and calling `validate_chunks(paths)`. The hash compute stays control-side (it is pure and needs no FS). The drift guard: the hashes sent to the agent are the SAME `cache_key` outputs the agent would localize.

- [ ] **Step 1: Write the failing test**

Add to `tests/validator/test_disk_stat.py` (mirror the existing `validate_game` test harness — fake `deps.steam_client.manifest_expand`, fake pool returning a manifest row):

```python
async def test_validate_game_uses_agent_stat_when_enabled(monkeypatch, tmp_path):
    # agent_enabled -> validate_game computes hashes then calls agent_client.stat(hashes),
    # NOT validate_chunks. Assert: stat called with the expected hash list; the
    # ValidationResult counts come from the agent's {cached,missing}; outcome classified
    # identically. Drift guard: the hashes equal cache_key(identifier, steam_chunk_uri(...),
    # slice_range) for each chunk sha in the expanded manifest.
    ...
```

> The implementer copies the existing `validate_game` happy-path test, sets `agent_enabled=True` (patch the `get_settings` used by `disk_stat.py` if it reads settings; NOTE `validate_game` takes `settings` as a param, so it reads `settings.agent_enabled` directly — no patching needed, just pass a `Settings(..., agent_enabled=True)`), supplies `deps.agent_client` with a `_FakeAgent.stat` that records the hashes and returns `{"cached": K, "missing": M}`, and asserts: (1) the recorded hashes equal the control-side `cache_key(...)` outputs, (2) `validate_chunks` was NOT called (monkeypatch it to raise), (3) the `ValidationResult` matches.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/validator/test_disk_stat.py -k "agent" -v`
Expected: FAIL (`validate_game` always builds paths + calls `validate_chunks`).

- [ ] **Step 3: Write minimal implementation**

In `validate_game`, the loop currently appends `cache_path(cache_root, h, levels)` to `paths`. Change it to ALSO collect the bare hash `h` (or restructure to collect hashes, deriving paths only in the flag-off branch). Minimal diff: collect `hashes: list[str] = []` alongside, append `h` to it. Then replace the final `cached, missing = await validate_chunks(paths)` with:

```python
    if settings.agent_enabled:
        if deps.agent_client is None:
            return ValidationResult(0, 0, 0, "error", ",".join(sorted(versions)), "agent_client unavailable")
        counts = await deps.agent_client.stat(hashes)
        cached, missing = counts["cached"], counts["missing"]
        total = cached + missing
    else:
        cached, missing = await validate_chunks(paths)
        total = len(paths)
```

(Keep building `paths` only matters for the flag-off branch; either always build both, or guard the `cache_path` append behind `if not settings.agent_enabled`. Simplest: keep appending both `h` to `hashes` and the path to `paths`; the agent branch uses `hashes`, the flag-off branch uses `paths`. `total` is `len(paths)` in flag-off — which equals `len(hashes)`.) Note `cache_root.is_dir()` guard at the top: when `agent_enabled`, the control plane may NOT have the cache mounted, so that guard would wrongly error. Move the `cache_root.is_dir()` check to be flag-off-only:

```python
    cache_root = Path(settings.lancache_nginx_cache_path)
    if not settings.agent_enabled and not cache_root.is_dir():
        return ValidationResult(0, 0, 0, "error", "", f"cache root not a directory: {cache_root}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/validator/test_disk_stat.py -v`
Expected: PASS (new agent test + all existing — flag-off identical). `.venv/bin/ruff check src/orchestrator/validator/disk_stat.py` → clean.

---

## Task 13: Lifespan wiring + `/health` (`agent_reachable`)

**Files:**
- Modify: `src/orchestrator/api/main.py` (lifespan: build `AgentClient`, inject into `JobsDeps`)
- Modify: `src/orchestrator/api/routers/health.py` (`steam_auth_ok` via agent when enabled; add `agent_reachable`)
- Test: `tests/api/test_health_endpoint.py`

The lifespan always constructs an `AgentClient(base_url=settings.agent_base_url, token=settings.orchestrator_token.get_secret_value())` and injects it into `JobsDeps`. The `SteamPrefillDriver` is STILL constructed control-side when `not agent_enabled` (flag-off needs it); when `agent_enabled`, the driver construction can be skipped (it lives on the agent) — but constructing it is harmless (it does no I/O at construction). To keep the diff minimal and the flag-off path intact, KEEP constructing the driver unconditionally and ALSO build the `AgentClient`; both go into `JobsDeps`. `/health.steam_auth_ok`: when `agent_enabled`, read it via `agent_client.auth_status()` (with a try/except → `agent_reachable=False` + `steam_auth_ok=False`); when off, keep the existing `app.state.prefill_driver.auth_status().ok`.

- [ ] **Step 1: Write the failing test**

Add to `tests/api/test_health_endpoint.py` (follow the file's existing app-construction harness):

```python
def test_health_agent_reachable_field_present():
    # default (agent_enabled False): agent_reachable should be True-by-default
    # (not consulted) OR reported; assert the field exists and the endpoint still 200s.
    ...

def test_health_agent_unreachable_when_enabled(monkeypatch):
    # agent_enabled True + an AgentClient whose auth_status raises AgentError ->
    # health reports agent_reachable False and steam_auth_ok False, endpoint still
    # returns its normal status code (the agent being down is surfaced, not a hard 500).
    ...
```

> The implementer mirrors the existing `steam_auth_ok` health test from ①. Construct the app/health with `agent_enabled=True` and an `app.state.agent_client` (or `JobsDeps.agent_client`) whose `auth_status` raises `AgentError`; assert the JSON has `agent_reachable is False` and `steam_auth_ok is False`. For the default case, assert the field is present and the endpoint behaves as today.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/api/test_health_endpoint.py -k "agent" -v`
Expected: FAIL (`agent_reachable` not in the response model).

- [ ] **Step 3: Write minimal implementation**

In `src/orchestrator/api/main.py` lifespan, after building the `prefill_driver` and before `JobsDeps`, add:

```python
    from orchestrator.clients.agent_client import AgentClient

    agent_client = AgentClient(
        base_url=settings.agent_base_url,
        token=settings.orchestrator_token.get_secret_value(),
    )
    app.state.agent_client = agent_client
```

and pass `agent_client=agent_client` into the `JobsDeps(...)` constructor.

In `src/orchestrator/api/routers/health.py`, add `agent_reachable: bool = True` to `HealthResponse`, and compute the steam-auth + reachability:

```python
    settings = get_settings()
    agent_reachable = True
    if settings.agent_enabled:
        try:
            st = await request.app.state.agent_client.auth_status()
            steam_auth_ok = bool(st["ok"])
        except Exception:  # noqa: BLE001 — agent down is reported, not fatal
            steam_auth_ok = False
            agent_reachable = False
    else:
        steam_auth_ok = request.app.state.prefill_driver.auth_status().ok
```

(Adapt to the health router's existing structure — it already computes `steam_auth_ok` from ①; wrap that existing logic in the `else` branch and add the `if settings.agent_enabled` branch. Add `agent_reachable` to the response construction.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/api/test_health_endpoint.py -v`
Expected: PASS. `.venv/bin/ruff check src/orchestrator/api/` → clean.

---

## Task 14: End-to-end (in-process agent + real AgentClient)

**Files:**
- Create: `tests/agent/test_e2e.py`

Drive a real `create_agent_app()` with a real `AgentClient` over an httpx ASGI transport (`httpx.ASGITransport(app=agent_app)`) — no network, no seam mocks — through a pull and a stat. Proves the full control→agent→result loop.

- [ ] **Step 1: Write the failing test**

`tests/agent/test_e2e.py`:

```python
"""End-to-end: real agent app + real AgentClient over ASGI transport."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from orchestrator.agent.app import create_agent_app
from orchestrator.clients.agent_client import AgentClient
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import cache_path

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


def _agent_client(app) -> AgentClient:
    transport = httpx.ASGITransport(app=app)
    return AgentClient(base_url="http://agent", token=TOKEN, transport=transport, poll_interval_sec=0.0)


async def test_e2e_stat(tmp_path):
    h = hashlib.md5(b"present").hexdigest()
    p = cache_path(tmp_path, h, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"data")
    settings = Settings(orchestrator_token=TOKEN, lancache_nginx_cache_path=tmp_path, cache_levels="2:2")
    app = create_agent_app(settings=settings)
    client = _agent_client(app)
    assert await client.stat([h, "b" * 32]) == {"cached": 1, "missing": 1}


async def test_e2e_pull(monkeypatch, tmp_path):
    # Pull hits the agent's puller; inject a fake lancache transport into the puller.
    def handler(request):
        return httpx.Response(200, content=b"x")

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    settings = Settings(orchestrator_token=TOKEN)
    app = create_agent_app(settings=settings)
    client = _agent_client(app)
    result = await client.pull(
        [{"url": "/depot/1/chunk/x", "host": "lancache.steamcontent.com"}],
        user_agent="UA/1.0",
    )
    assert result["chunks_ok"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_e2e.py -v`
Expected: initially may FAIL if anything in the wiring is off (e.g. ASGI background-task completion). Resolve any real wiring issue; do NOT weaken assertions.

> Known wrinkle: the agent's async `/v1/pull` spawns `asyncio.create_task(_run())`; under `ASGITransport`, the POST returns 202 immediately and the task runs on the same loop. `AgentClient._post_then_poll` then GETs with `poll_interval_sec=0.0` (an `await asyncio.sleep(0)` yields the loop so the task progresses). If the task hasn't completed on the first poll, the loop continues until it does. This is exactly the production poll loop, so it validates the real mechanism.

- [ ] **Step 3: (no new impl — this is an integration test over Tasks 4/5/9)**

If a genuine wiring bug surfaces, fix it in the relevant module (puller/app/client), re-running its unit tests too.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_e2e.py -v`
Expected: PASS.

---

## Task 15: Full verification + single commit + push + PR

**Files:** none (verification + git)

- [ ] **Step 1: Full suite + ruff**

Run: `.venv/bin/python -m pytest -q --ignore=tests/scripts`
Expected: all pass except the known pre-existing `tests/test_licenses.py` (pip-licenses-not-on-PATH) failure. (`tests/scripts` is ignored due to the pre-existing `check-phase-gate.sh` hang — unrelated to this work.)
Run: `.venv/bin/ruff check src tests` → `All checks passed!`
Run: `.venv/bin/ruff format --check src tests` (the pre-commit hook runs `ruff format`; run `.venv/bin/ruff format` if it reports reformatting, then re-stage).

- [ ] **Step 2: Present A/B/C commit-structure options, WAIT for the pick**

Per the standing commit-approval protocol — do NOT commit until the operator picks. Recommended A = single `feat(agent): data-plane HTTP agent + control-plane AgentClient (re-arch ②)`.

- [ ] **Step 3: Commit (after the pick), push, open PR**

Single `feat` commit. Push `feat/data-plane-agent`. Open the PR. The operator merges.

**PR body must note the deploy + rollout:**
- Deploy = a SECOND container from the same image running `python -m orchestrator.agent`, with host mounts: cache dir **read-only**, `/SteamPrefill` + `Config` + `~/.cache/SteamPrefill`, and reachability to the lancache loopback. Set `ORCH_AGENT_BIND_HOST`/`ORCH_AGENT_BIND_PORT` (default `127.0.0.1:8780`).
- Rollout (spec §5): merge with `ORCH_AGENT_ENABLED=false` (zero behavior change) → deploy the agent container → flip `ORCH_AGENT_ENABLED=true` + restart the orchestrator → **operator live smoke** (one Steam prefill + one validate, counts vs baseline) → flag-rollback if needed → the orchestrator container drops its `/SteamPrefill` mount after the flip.
- Named follow-ups (spec §5 step 5 + §7): delete the in-process `prefill/downloader.py`/`epic_downloader.py`/`validate_chunks`-control-side once stable; relocate Epic `verify_cached` to the agent (avoids the ④ hairpin); modern validate-manifest source then delete the steam worker; the ④ LXC move.

---

## Self-Review

**1. Spec coverage:**
- §1 architecture (wrapper-not-rewrite, control-computes/agent-executes) → Tasks 3,5,9,10–12. ✓
- §2.1 `/v1/pull` async + SSRF guard → Tasks 3,4. ✓
- §2.2 `/v1/stat` sync + 32-hex guard → Task 5. ✓ (errors-field dropped — documented decision in Task 5.)
- §2.3 `/v1/steam/*` async + status reads → Task 6. ✓
- §2.4 `AgentClient` POST-then-poll + typed error → Task 9. ✓
- §3 structure (new `agent/` + `clients/`, imports-unchanged, seam edits) → all tasks. ✓
- §4 security (reused middleware param, boot guard, anti-SSRF, 32-hex, no-token-log) → Tasks 5,6,7. ✓ (read-only cache mount = deploy concern, in Task 15 PR notes.)
- §5 migration (`agent_enabled` flag, deploy/flip/rollback, F14–F17 untouched, clean agent-down failure) → Tasks 10–13,15. ✓
- §6 testing (endpoint tests w/ reused harnesses, AgentClient, flag-off=identical + flag-on=same-writes equivalence, e2e, drift guard) → Tasks 3–14. ✓
- §7 scope (deferred items) → Task 15 PR notes. ✓

**2. Placeholder scan:** Tasks 10–13 use "implementer mirrors the existing test harness" for the seam tests rather than full literal test bodies — this is deliberate because those tests must copy each file's existing happy-path fixtures (fake pool/manifest/deps) which differ per file; the assertions to make are spelled out explicitly. The PRODUCTION code edits in those tasks are fully literal. Acceptable: the engineer has the exact assertions + the existing tests to copy.

**3. Type consistency:** `ChunkSpec(url, host)` (Task 3) used in Tasks 4,14. `PullResult` fields = downloader's (Task 3). `AgentClient.pull/stat/steam_prefill/auth_status` signatures (Task 9) match call sites (Tasks 10–13). `agent_client.steam_prefill` returns `{"ok","raw"}` (Task 6 sets, Task 9 returns `result`, Task 10 reads). `agent_client.stat` returns `{"cached","missing"}` (Task 5 returns, Task 9 passes through, Task 12 reads). `Deps.agent_client` (Task 10) referenced in Tasks 11,12,13. ✓

---

**Plan complete.** Two execution options follow at handoff.
