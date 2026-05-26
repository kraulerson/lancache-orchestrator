# BL11 — Steam Library Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline (per BL6-BL10 autonomy grant). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operationalize Steam library enumeration: a generic asyncio jobs worker in the orchestrator process, a `library_sync` handler that calls `library.enumerate` on the steam worker subprocess, upserts the operator's owned Steam apps into the `games` table, and exposes a `POST /api/v1/platforms/steam/library/sync` manual trigger. Successful Steam auth (both no-2FA and 2FA paths) auto-queues a `library_sync` job.

**Architecture:** Single-loop asyncio jobs worker spawned in FastAPI lifespan startup, claiming jobs atomically via SELECT-then-UPDATE inside `write_transaction()`. Handler registry indexed by `jobs.kind`. Steam worker grows one new IPC op (`library.enumerate`). Library upsert uses `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE` to make re-sync idempotent. Dedup of in-flight `library_sync` jobs by handler-side query before insert.

**Tech Stack:** Python 3.12, asyncio, aiosqlite (via existing BL4 pool), FastAPI, structlog. No new third-party deps.

**Spec reference:** `docs/superpowers/specs/2026-05-24-f1-steam-credentials-fetcher-design.md` §5 (BL11) — locked decisions D6, D7, D10, D15.

**Build Loop pattern (per BL6-BL10):** ONE combined commit at end covering feat + tests + docs + ADR notes. Process-checklist marks at each step.

---

## File Structure

**Create:**
- `src/orchestrator/jobs/__init__.py` — package marker (~5 LoC)
- `src/orchestrator/jobs/worker.py` — generic asyncio job dispatcher (~120 LoC)
- `src/orchestrator/jobs/handlers/__init__.py` — `HANDLERS` registry (~30 LoC)
- `src/orchestrator/jobs/handlers/library_sync.py` — Steam library sync handler (~150 LoC)
- `src/orchestrator/api/routers/sync.py` — manual sync endpoint (~80 LoC)
- `tests/jobs/__init__.py` — package marker
- `tests/jobs/conftest.py` — jobs-specific fixtures
- `tests/jobs/test_worker.py` — generic-dispatcher tests
- `tests/jobs/test_library_sync_handler.py` — handler-level tests
- `tests/api/test_sync_router.py` — endpoint tests

**Modify:**
- `src/orchestrator/api/main.py` — lifespan spawns jobs worker; wires sync router
- `src/orchestrator/api/routers/auth.py` — both auth-success paths queue `library_sync` job
- `src/orchestrator/platform/steam/worker.py` — `library.enumerate` handler
- `src/orchestrator/platform/steam/client.py` — `library_enumerate()` method
- `src/orchestrator/core/settings.py` — add `jobs_worker_poll_interval_sec`
- `tests/api/test_auth_router.py` — assert auth success queues a job
- `tests/api/conftest.py` — add a `mock_steam_client_with_library()` helper if needed
- `CHANGELOG.md` — 8 categories
- `FEATURES.md` — new "Feature 11: BL11 — Steam Library Sync" entry
- `PROJECT_BIBLE.md` — status pointer to BL11

**No DB migration.** `jobs.kind` CHECK constraint already permits `'library_sync'`; `games` table schema unchanged.

---

## Locked design decisions (from spec §5 + this plan)

| # | Decision | Rationale |
|---|---|---|
| P1 | **Single jobs worker task** spawned via `asyncio.create_task` in lifespan | Spec D10. Concurrent multi-job deferred. |
| P2 | **SELECT-then-UPDATE inside `write_transaction()`** for atomic claim | BL4 pool's `WriteTx` exposes `read_one` + `execute` under one `BEGIN IMMEDIATE`. Simpler than adding an `execute_write_returning` helper to pool. |
| P3 | **Handler signature:** `async def handler(job: dict, deps: Deps) -> None` | `deps` carries `pool`, `steam_client`, `log` — single injection point for test override. |
| P4 | **`Deps` dataclass** in `jobs/worker.py` | Avoids passing 3 separate args; lets tests construct a minimal one. |
| P5 | **Handler failures** caught in worker loop → `mark_failed(job_id, "ExceptionName: trunc-msg")` | Spec §5.3 pseudo-code; error stored truncated to 200 chars (matches platforms.last_error convention). |
| P6 | **Unknown `kind`** → `mark_failed(job_id, "no handler for kind X")` | Spec §5.7 "unknown kind → failed". |
| P7 | **Worker loop swallows ALL exceptions** from handlers; never crashes | Spec §5.7 "handler-crash isolation". |
| P8 | **Dedup query** in sync endpoint before INSERT: `SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' AND state IN ('queued','running') LIMIT 1` | Spec §5.6. Race: two concurrent POSTs can both pass the check; the second INSERT creates a duplicate. Acceptable for BL11 (auth is rare; cost is one extra job that no-ops). Real dedup enforcement deferred to F12. |
| P9 | **Auto-trigger** is best-effort: a `PoolError` queuing the job in auth-success path is logged but does NOT fail the auth response | Auth succeeded; the operator can manually re-sync. |
| P10 | **App ID type:** steam-next returns `app_id: int`; `games.app_id` is `TEXT`. Handler converts via `str(int_app_id)` before insert. | Spec §5.4 + games schema. |
| P11 | **Handler upserts ONE row per app**, not bulk via `execute_many` | Lower-throughput path; lets individual-row failures (e.g., title constraint) propagate clearly. Easier to add per-row structured logging. |
| P12 | **`metadata` JSON shape locked:** `{"depots": [int, ...], "steam_packages": []}` | Spec §5.4. `steam_packages` reserved for future use; emit empty list. |
| P13 | **Jobs worker shutdown signaling** via `asyncio.Event` set in lifespan teardown; worker loop polls `_shutdown.is_set()` between iterations | Mirrors steam_worker shutdown semantics. 5s join timeout. |
| P14 | **Jobs worker poll interval:** `Settings.jobs_worker_poll_interval_sec` default `1.0` | Spec §5.3, §7.2. |
| P15 | **NO library enumeration mock in steam worker tests** | Test the IPC dispatcher dispatching the new op; steam-next interaction itself is UAT-6 territory. |

---

## Task Decomposition

### Task 1: Settings — add `jobs_worker_poll_interval_sec`

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Modify: `tests/core/test_settings.py`

- [ ] **Step 1.1: Write the failing test**

Add to `tests/core/test_settings.py` (alongside other field tests):

```python
def test_jobs_worker_poll_interval_default():
    s = reload_settings(orchestrator_token="a" * 32)
    assert s.jobs_worker_poll_interval_sec == 1.0


def test_jobs_worker_poll_interval_must_be_positive(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    monkeypatch.setenv("ORCH_JOBS_WORKER_POLL_INTERVAL_SEC", "0")
    with pytest.raises(ValidationError):
        reload_settings()


def test_jobs_worker_poll_interval_warns_above_ceiling(monkeypatch, caplog):
    # 60s would be silly — emit a config.jobs_poll_interval_high warning.
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    monkeypatch.setenv("ORCH_JOBS_WORKER_POLL_INTERVAL_SEC", "61")
    s = reload_settings()
    # Implementation must emit structured log; capsys not caplog.
    # See test pattern for `config.cors_wildcard` warning.
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/core/test_settings.py::test_jobs_worker_poll_interval_default -xvs
```
Expected: AttributeError (field doesn't exist).

- [ ] **Step 1.3: Implement the field**

In `src/orchestrator/core/settings.py`, add alongside other pool/worker fields:

```python
jobs_worker_poll_interval_sec: float = Field(
    default=1.0,
    ge=0.05,
    le=300.0,
    description="Empty-queue poll cadence for the jobs worker loop (BL11).",
)
```

And in the `@model_validator(mode="after")`:

```python
if self.jobs_worker_poll_interval_sec > 60.0:
    _log.warning(
        "config.jobs_poll_interval_high",
        value=self.jobs_worker_poll_interval_sec,
        hint="A poll interval > 60s noticeably delays library/manifest sync responses",
    )
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/core/test_settings.py::test_jobs_worker_poll_interval_default \
                 tests/core/test_settings.py::test_jobs_worker_poll_interval_must_be_positive \
                 tests/core/test_settings.py::test_jobs_worker_poll_interval_warns_above_ceiling -xvs
```
Expected: all 3 pass.

- [ ] **Step 1.5: Mark process step**

```bash
# No commit yet — combined commit at the end.
```

---

### Task 2: Jobs worker — generic asyncio dispatcher

**Files:**
- Create: `src/orchestrator/jobs/__init__.py`
- Create: `src/orchestrator/jobs/worker.py`
- Create: `src/orchestrator/jobs/handlers/__init__.py`
- Create: `tests/jobs/__init__.py`
- Create: `tests/jobs/conftest.py`
- Create: `tests/jobs/test_worker.py`

- [ ] **Step 2.1: Create package skeleton**

```python
# src/orchestrator/jobs/__init__.py
"""Async jobs subsystem (BL11)."""
```

```python
# src/orchestrator/jobs/handlers/__init__.py
"""Handler registry for the jobs worker (BL11)."""
from __future__ import annotations
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

Handler = Callable[[dict, "Deps"], Awaitable[None]]

HANDLERS: dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    """Register a handler under a jobs.kind value.

    Idempotent re-registration overwrites — tests need this when they
    swap a real handler for a stub via dependency injection.
    """
    HANDLERS[kind] = handler


def clear() -> None:
    """Test helper: empty the registry."""
    HANDLERS.clear()
```

```python
# tests/jobs/__init__.py
```

- [ ] **Step 2.2: Write the failing test for atomic claim**

Create `tests/jobs/conftest.py`:

```python
import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from orchestrator.db.pool import Pool, init_pool, close_pool


@pytest_asyncio.fixture
async def jobs_pool(tmp_path, monkeypatch) -> AsyncIterator[Pool]:
    """Fresh DB with migrations + a `steam` platform row + empty jobs."""
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(tmp_path / "test.db"))
    from orchestrator.core.settings import reload_settings
    reload_settings()
    from orchestrator.db.migrate import run_migrations
    run_migrations(str(tmp_path / "test.db"))
    await init_pool()
    pool = (await __import__("orchestrator.db.pool", fromlist=["get_pool"]).get_pool())
    await pool.execute_write(
        "INSERT INTO platforms (name, auth_status) VALUES (?, 'never')", ("steam",)
    )
    try:
        yield pool
    finally:
        await close_pool()
```

Then `tests/jobs/test_worker.py`:

```python
import asyncio

import pytest
import pytest_asyncio

from orchestrator.jobs.worker import Deps, claim_next_job, mark_succeeded, mark_failed


pytestmark = pytest.mark.asyncio


async def test_claim_next_job_returns_none_when_empty(jobs_pool):
    row = await claim_next_job(jobs_pool)
    assert row is None


async def test_claim_next_job_returns_queued_job(jobs_pool):
    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),
    )
    row = await claim_next_job(jobs_pool)
    assert row is not None
    assert row["kind"] == "library_sync"
    assert row["state"] == "running"
    assert row["started_at"] is not None


async def test_claim_next_job_atomic_under_concurrency(jobs_pool):
    """Two concurrent claim_next_job calls must return distinct rows."""
    for _ in range(2):
        await jobs_pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
            ("library_sync", "steam"),
        )
    a, b = await asyncio.gather(claim_next_job(jobs_pool), claim_next_job(jobs_pool))
    assert a is not None and b is not None
    assert a["id"] != b["id"]


async def test_mark_succeeded_sets_state_and_finished_at(jobs_pool):
    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),
    )
    row = await claim_next_job(jobs_pool)
    assert row is not None
    await mark_succeeded(jobs_pool, row["id"])
    after = await jobs_pool.read_one("SELECT state, finished_at FROM jobs WHERE id=?", (row["id"],))
    assert after["state"] == "succeeded"
    assert after["finished_at"] is not None


async def test_mark_failed_truncates_error_to_200_chars(jobs_pool):
    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),
    )
    row = await claim_next_job(jobs_pool)
    long_msg = "x" * 500
    await mark_failed(jobs_pool, row["id"], long_msg)
    after = await jobs_pool.read_one(
        "SELECT state, error FROM jobs WHERE id=?", (row["id"],)
    )
    assert after["state"] == "failed"
    assert len(after["error"]) == 200
```

- [ ] **Step 2.3: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/jobs/test_worker.py -xvs
```
Expected: ImportError (module doesn't exist).

- [ ] **Step 2.4: Implement `worker.py`**

```python
# src/orchestrator/jobs/worker.py
"""Generic asyncio jobs dispatcher (BL11).

Atomic claim via SELECT-then-UPDATE under BEGIN IMMEDIATE
(see write_transaction). Single-loop topology (spec D10).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.jobs.handlers import HANDLERS

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool
    from orchestrator.platform.steam.client import SteamWorkerClient

_log = structlog.get_logger(__name__)

JOB_ERROR_TRUNCATE = 200


@dataclass(frozen=True, slots=True)
class Deps:
    """Handler dependency bundle.

    Tests construct a Deps with minimal handlers; production builds one
    in the lifespan that carries the singleton SteamWorkerClient.
    """

    pool: Pool
    steam_client: SteamWorkerClient | None  # None during tests that don't need steam


async def claim_next_job(pool: Pool) -> dict[str, Any] | None:
    """Atomically claim the oldest queued job. Returns the job row dict
    with `state='running'` and `started_at` set, or None if nothing queued.

    Uses SELECT-then-UPDATE under BEGIN IMMEDIATE so concurrent claims
    on the same DB don't race.
    """
    async with pool.write_transaction() as tx:
        row = await tx.read_one(
            "SELECT id, kind, game_id, platform, payload "
            "FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
        )
        if row is None:
            return None
        await tx.execute(
            "UPDATE jobs SET state='running', started_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
        # Re-read started_at so callers see the persisted timestamp.
        updated = await tx.read_one(
            "SELECT id, kind, game_id, platform, state, started_at, payload "
            "FROM jobs WHERE id=?",
            (row["id"],),
        )
        return updated


async def mark_succeeded(pool: Pool, job_id: int) -> None:
    await pool.execute_write(
        "UPDATE jobs SET state='succeeded', finished_at=CURRENT_TIMESTAMP, error=NULL "
        "WHERE id=? AND state='running'",
        (job_id,),
    )


async def mark_failed(pool: Pool, job_id: int, error: str) -> None:
    truncated = error[:JOB_ERROR_TRUNCATE]
    await pool.execute_write(
        "UPDATE jobs SET state='failed', finished_at=CURRENT_TIMESTAMP, error=? "
        "WHERE id=? AND state='running'",
        (truncated, job_id),
    )


async def worker_loop(deps: Deps, *, shutdown: asyncio.Event, poll_interval_sec: float) -> None:
    """Generic job dispatcher. Runs until `shutdown` is set.

    On unknown `kind`: marks job failed with structured error.
    On handler exception: catches, marks failed, continues loop.
    """
    _log.info("jobs.worker.started", poll_interval=poll_interval_sec)
    while not shutdown.is_set():
        try:
            row = await claim_next_job(deps.pool)
        except Exception as e:  # pool failure — back off and retry
            _log.error("jobs.worker.claim_failed", reason=str(e)[:200])
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_sec)
            except TimeoutError:
                pass
            continue

        if row is None:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_sec)
            except TimeoutError:
                pass
            continue

        _log.info("jobs.worker.claimed_job", job_id=row["id"], kind=row["kind"])
        handler = HANDLERS.get(row["kind"])
        if handler is None:
            await mark_failed(deps.pool, row["id"], f"no handler for kind {row['kind']!r}")
            _log.warning("jobs.handler.no_handler", kind=row["kind"], job_id=row["id"])
            continue

        t0 = time.monotonic()
        _log.info("jobs.handler.started", kind=row["kind"], job_id=row["id"])
        try:
            await handler(row, deps)
            await mark_succeeded(deps.pool, row["id"])
            _log.info(
                "jobs.handler.completed",
                kind=row["kind"],
                job_id=row["id"],
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:JOB_ERROR_TRUNCATE - 50]}"
            try:
                await mark_failed(deps.pool, row["id"], err)
            except Exception as mark_e:
                _log.error(
                    "jobs.handler.mark_failed_failed",
                    job_id=row["id"],
                    original_error=err,
                    reason=str(mark_e)[:200],
                )
            _log.warning(
                "jobs.handler.failed",
                kind=row["kind"],
                job_id=row["id"],
                kind_error=type(e).__name__,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
    _log.info("jobs.worker.stopped")
```

- [ ] **Step 2.5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/jobs/test_worker.py -xvs
```
Expected: 5/5 pass.

- [ ] **Step 2.6: Write loop-level tests for the dispatcher**

Add to `tests/jobs/test_worker.py`:

```python
async def test_worker_loop_dispatches_to_registered_handler(jobs_pool):
    from orchestrator.jobs.handlers import HANDLERS, register, clear
    called: list[int] = []

    async def my_handler(row, deps):
        called.append(row["id"])

    clear()
    register("library_sync", my_handler)

    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),
    )

    shutdown = asyncio.Event()
    deps = Deps(pool=jobs_pool, steam_client=None)

    async def stop_after():
        # Wait for the handler to fire, then stop the loop.
        for _ in range(50):
            if called:
                break
            await asyncio.sleep(0.02)
        shutdown.set()

    await asyncio.gather(
        worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.05),
        stop_after(),
    )
    assert len(called) == 1
    clear()


async def test_worker_loop_marks_unknown_kind_failed(jobs_pool):
    from orchestrator.jobs.handlers import clear
    clear()
    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),  # registered nowhere
    )
    shutdown = asyncio.Event()
    deps = Deps(pool=jobs_pool, steam_client=None)

    async def stop_after():
        # Poll for job state until failed, then stop.
        for _ in range(50):
            row = await jobs_pool.read_one("SELECT state FROM jobs LIMIT 1")
            if row and row["state"] == "failed":
                break
            await asyncio.sleep(0.02)
        shutdown.set()

    await asyncio.gather(
        worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.05),
        stop_after(),
    )
    row = await jobs_pool.read_one("SELECT state, error FROM jobs LIMIT 1")
    assert row["state"] == "failed"
    assert "no handler for kind 'library_sync'" in row["error"]


async def test_worker_loop_isolates_handler_crash(jobs_pool):
    from orchestrator.jobs.handlers import register, clear
    crashed_ids: list[int] = []
    ran_ids: list[int] = []

    async def crashing(row, deps):
        crashed_ids.append(row["id"])
        raise RuntimeError("boom")

    async def normal(row, deps):
        ran_ids.append(row["id"])

    clear()
    register("library_sync", crashing)
    register("validate", normal)

    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("library_sync", "steam"),
    )
    await jobs_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        ("validate", "steam"),
    )

    shutdown = asyncio.Event()
    deps = Deps(pool=jobs_pool, steam_client=None)

    async def stop_after():
        for _ in range(80):
            if ran_ids:
                break
            await asyncio.sleep(0.02)
        shutdown.set()

    await asyncio.gather(
        worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.05),
        stop_after(),
    )
    assert crashed_ids and ran_ids  # both processed; crash didn't kill loop
    clear()
```

- [ ] **Step 2.7: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/jobs/test_worker.py -xvs
```
Expected: 8/8 pass.

---

### Task 3: library_sync handler

**Files:**
- Create: `src/orchestrator/jobs/handlers/library_sync.py`
- Create: `tests/jobs/test_library_sync_handler.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/jobs/test_library_sync_handler.py`:

```python
import asyncio
import json

import pytest
import pytest_asyncio

from orchestrator.jobs.handlers.library_sync import library_sync_handler
from orchestrator.jobs.worker import Deps


class _StubSteam:
    """Stand-in for SteamWorkerClient — only `library_enumerate()` is exercised."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = 0

    async def library_enumerate(self):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


pytestmark = pytest.mark.asyncio


async def test_handler_upserts_owned_games(jobs_pool):
    stub = _StubSteam(result={"apps": [
        {"app_id": 730, "name": "Counter-Strike 2", "depots": [731, 734]},
        {"app_id": 440, "name": "Team Fortress 2", "depets": []},
    ]})
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    await library_sync_handler(job, deps)

    rows = await jobs_pool.read_all("SELECT platform, app_id, title, owned, metadata FROM games ORDER BY app_id")
    assert len(rows) == 2
    cs2 = next(r for r in rows if r["app_id"] == "730")
    assert cs2["title"] == "Counter-Strike 2"
    assert cs2["owned"] == 1
    md = json.loads(cs2["metadata"])
    assert md["depots"] == [731, 734]
    assert md["steam_packages"] == []


async def test_handler_idempotent_re_sync(jobs_pool):
    stub = _StubSteam(result={"apps": [
        {"app_id": 730, "name": "Counter-Strike 2", "depots": [731]},
    ]})
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    await library_sync_handler(job, deps)
    await library_sync_handler(job, deps)

    rows = await jobs_pool.read_all("SELECT app_id FROM games")
    assert len(rows) == 1


async def test_handler_updates_title_and_metadata_on_resync(jobs_pool):
    stub_v1 = _StubSteam(result={"apps": [
        {"app_id": 730, "name": "Counter-Strike: Global Offensive", "depots": [731]},
    ]})
    stub_v2 = _StubSteam(result={"apps": [
        {"app_id": 730, "name": "Counter-Strike 2", "depots": [731, 734]},
    ]})

    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    await library_sync_handler(job, Deps(pool=jobs_pool, steam_client=stub_v1))
    await library_sync_handler(job, Deps(pool=jobs_pool, steam_client=stub_v2))

    row = await jobs_pool.read_one("SELECT title, metadata FROM games WHERE app_id=?", ("730",))
    assert row["title"] == "Counter-Strike 2"
    md = json.loads(row["metadata"])
    assert md["depots"] == [731, 734]


async def test_handler_empty_library_zero_inserts(jobs_pool):
    stub = _StubSteam(result={"apps": []})
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    await library_sync_handler(job, deps)

    rows = await jobs_pool.read_all("SELECT id FROM games")
    assert rows == []


async def test_handler_steam_offline_raises_no_partial_writes(jobs_pool):
    from orchestrator.platform.steam.client import IPCTimeoutError
    stub = _StubSteam(raises=IPCTimeoutError("worker timeout"))
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}

    with pytest.raises(IPCTimeoutError):
        await library_sync_handler(job, deps)

    rows = await jobs_pool.read_all("SELECT id FROM games")
    assert rows == []


async def test_handler_rejects_non_steam_platform(jobs_pool):
    stub = _StubSteam(result={"apps": []})
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "epic", "game_id": None, "payload": None}
    with pytest.raises(ValueError, match="library_sync only supports steam"):
        await library_sync_handler(job, deps)


async def test_handler_requires_steam_client(jobs_pool):
    deps = Deps(pool=jobs_pool, steam_client=None)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    with pytest.raises(RuntimeError, match="steam_client is required"):
        await library_sync_handler(job, deps)


async def test_handler_preserves_existing_status_on_upsert(jobs_pool):
    # Pre-seed a game with status='up_to_date'
    await jobs_pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, ?, 1, 'up_to_date')",
        ("steam", "730", "CS:GO"),
    )
    stub = _StubSteam(result={"apps": [
        {"app_id": 730, "name": "Counter-Strike 2", "depots": [731]},
    ]})
    deps = Deps(pool=jobs_pool, steam_client=stub)
    job = {"id": 1, "kind": "library_sync", "platform": "steam", "game_id": None, "payload": None}
    await library_sync_handler(job, deps)

    row = await jobs_pool.read_one("SELECT title, status FROM games WHERE app_id=?", ("730",))
    assert row["title"] == "Counter-Strike 2"  # updated
    assert row["status"] == "up_to_date"  # preserved
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/jobs/test_library_sync_handler.py -xvs
```
Expected: ImportError.

- [ ] **Step 3.3: Implement `library_sync_handler`**

```python
# src/orchestrator/jobs/handlers/library_sync.py
"""Steam library sync handler (BL11).

Called by the jobs worker when a `library_sync` job is claimed. Asks the
steam-worker subprocess to enumerate the operator's owned apps, then
upserts the `games` table.

Idempotent re-sync: `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE`
updates title + owned + metadata only — `status`, `cached_version`,
`last_validated_at`, etc. are preserved (locked decision P11).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


async def library_sync_handler(job: dict[str, Any], deps: Deps) -> None:
    """Library-sync handler.

    Raises:
        ValueError — non-steam platform (only steam supported in F1).
        RuntimeError — no steam_client in Deps.
        IPCTimeoutError / WorkerDiedError / WorkerDisabledError — propagate from
            SteamWorkerClient; the worker loop translates to job state=failed.
        SteamWorkerError — propagate (e.g., NotAuthenticated → user must re-auth).
    """
    if job.get("platform") != "steam":
        raise ValueError(f"library_sync only supports steam (got {job.get('platform')!r})")
    if deps.steam_client is None:
        raise RuntimeError("steam_client is required for library_sync handler")

    _log.info("library_sync.enumerate.started", job_id=job["id"])
    result = await deps.steam_client.library_enumerate()
    apps = result.get("apps") or []
    _log.info(
        "library_sync.enumerate.returned",
        job_id=job["id"],
        app_count=len(apps),
    )

    upsert_sql = (
        "INSERT INTO games (platform, app_id, title, owned, metadata) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(platform, app_id) DO UPDATE SET "
        "  title = excluded.title, "
        "  owned = 1, "
        "  metadata = excluded.metadata"
    )

    upserted = 0
    for app in apps:
        app_id_int = app.get("app_id")
        title = app.get("name")
        depots = app.get("depots") or []
        if app_id_int is None or title is None:
            _log.warning(
                "library_sync.skipped_app",
                job_id=job["id"],
                reason="missing app_id or name",
                raw=str(app)[:200],
            )
            continue
        metadata = json.dumps(
            {"depots": list(depots), "steam_packages": []},
            separators=(",", ":"),
        )
        await deps.pool.execute_write(
            upsert_sql, ("steam", str(app_id_int), title, metadata)
        )
        upserted += 1

    _log.info(
        "library_sync.upserted",
        job_id=job["id"],
        upserted=upserted,
        skipped=len(apps) - upserted,
    )
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/jobs/test_library_sync_handler.py -xvs
```
Expected: 8/8 pass.

- [ ] **Step 3.5: Register the handler at import time**

Add to `src/orchestrator/jobs/handlers/__init__.py` after the `register()` function:

```python
def _register_builtin_handlers() -> None:
    """Called once at module import to wire built-in handlers."""
    from orchestrator.jobs.handlers.library_sync import library_sync_handler
    register("library_sync", library_sync_handler)


_register_builtin_handlers()
```

Note: tests that use the registry call `clear()` first to drop the auto-registered handler, then re-register a stub. This is documented in `Handler` docstring.

---

### Task 4: Steam worker `library.enumerate` op

**Files:**
- Modify: `src/orchestrator/platform/steam/worker.py`
- Modify: `src/orchestrator/platform/steam/client.py`
- Modify: `tests/platform/steam/test_worker.py` (if exists, else create)
- Modify: `tests/platform/steam/test_client.py` (if exists, else create)

- [ ] **Step 4.1: Inspect existing worker/client tests**

```bash
ls tests/platform/steam/ 2>/dev/null
```

- [ ] **Step 4.2: Write the failing client-side test**

Add to `tests/platform/steam/test_client.py` (or create):

```python
import pytest
pytestmark = pytest.mark.asyncio


async def test_library_enumerate_returns_apps_list(mock_worker_pipe):
    """mock_worker_pipe is a fixture that lets us pre-program responses."""
    mock_worker_pipe.queue_response({
        "msg_id": "ANY",  # client correlates; fixture rewrites
        "ok": True,
        "result": {"apps": [{"app_id": 730, "name": "CS2", "depots": [731]}]},
    })
    from orchestrator.platform.steam.client import SteamWorkerClient
    client = SteamWorkerClient()
    # Bypass start(); inject the mock pipe directly. Pattern matches BL10 tests.
    ...
```

Pragmatic alternative (because the existing test plumbing is the source of truth): match whatever the BL10 tests do. Read `tests/platform/steam/test_client.py` first, then add a `test_library_enumerate_*` parallel to `test_auth_status_*`.

- [ ] **Step 4.3: Add `library_enumerate()` method to client**

```python
# In src/orchestrator/platform/steam/client.py, alongside auth_status():
async def library_enumerate(self) -> dict[str, Any]:
    return await self._send_and_await("library.enumerate", {})
```

- [ ] **Step 4.4: Add the worker-side handler**

```python
# In src/orchestrator/platform/steam/worker.py:

def _handle_library_enumerate(msg_id: str, _params: dict[str, str]) -> None:
    global _client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    try:
        # steam-next API: SteamClient.licenses is a list of CMsgClientLicenseList.License
        # objects. The app_id is the package's appid_for_log_view; depots are listed
        # under each package's app_ids.
        # Spike A established the iteration pattern; replicate it here.
        apps: list[dict[str, object]] = []
        seen_app_ids: set[int] = set()
        for license_obj in _client.licenses or []:
            package_id = getattr(license_obj, "package_id", None)
            if package_id is None:
                continue
            # Resolve package -> apps via steam-next's products cache.
            pkg_info = _client.get_product_info(packages=[package_id]).get("packages", {}).get(package_id)
            if not pkg_info:
                continue
            for app_id in pkg_info.get("appids", {}).values():
                if app_id in seen_app_ids:
                    continue
                seen_app_ids.add(app_id)
                app_info = _client.get_product_info(apps=[app_id]).get("apps", {}).get(app_id, {})
                name = app_info.get("common", {}).get("name") or f"app_{app_id}"
                depots_dict = app_info.get("depots", {}) or {}
                depot_ids = [int(d) for d in depots_dict.keys() if str(d).isdigit()]
                apps.append({"app_id": int(app_id), "name": str(name), "depots": depot_ids})
        _ok(msg_id, {"apps": apps})
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


# Register in _HANDLERS dict:
_HANDLERS = {
    "auth.begin": _handle_auth_begin,
    "auth.complete": _handle_auth_complete,
    "auth.status": _handle_auth_status,
    "library.enumerate": _handle_library_enumerate,
}
```

**Important — spike-validated code path:** the exact steam-next API for product_info + license enumeration was established in Spike A. Re-validate against the spike code if anything looks off; the iteration above is the documented pattern.

- [ ] **Step 4.5: Run client + worker IPC plumbing test**

```bash
.venv/bin/pytest tests/platform/steam/test_client.py -xvs
```

---

### Task 5: Manual sync endpoint

**Files:**
- Create: `src/orchestrator/api/routers/sync.py`
- Create: `tests/api/test_sync_router.py`

- [ ] **Step 5.1: Write the failing endpoint tests**

```python
# tests/api/test_sync_router.py
import pytest
pytestmark = pytest.mark.asyncio


async def test_sync_endpoint_queues_job(client_with_pool):
    r = await client_with_pool.post("/api/v1/platforms/steam/library/sync")
    assert r.status_code == 202
    body = r.json()
    assert "job_id" in body
    # Verify the job actually landed in the table.
    pool = client_with_pool.app.state.pool
    row = await pool.read_one("SELECT kind, state, platform, source FROM jobs WHERE id=?", (body["job_id"],))
    assert row == {"kind": "library_sync", "state": "queued", "platform": "steam", "source": "api"}


async def test_sync_endpoint_dedupes_in_flight(client_with_pool):
    first = await client_with_pool.post("/api/v1/platforms/steam/library/sync")
    second = await client_with_pool.post("/api/v1/platforms/steam/library/sync")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_sync_endpoint_dedupes_running_job(client_with_pool):
    pool = client_with_pool.app.state.pool
    await pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source, started_at) VALUES (?, ?, 'running', 'api', CURRENT_TIMESTAMP)",
        ("library_sync", "steam"),
    )
    running_id = (await pool.read_one("SELECT id FROM jobs WHERE state='running'"))["id"]
    r = await client_with_pool.post("/api/v1/platforms/steam/library/sync")
    assert r.status_code == 202
    assert r.json()["job_id"] == running_id


async def test_sync_endpoint_returns_new_job_after_finished_state(client_with_pool):
    pool = client_with_pool.app.state.pool
    await pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'succeeded', 'api')",
        ("library_sync", "steam"),
    )
    r = await client_with_pool.post("/api/v1/platforms/steam/library/sync")
    assert r.status_code == 202
    new_id = r.json()["job_id"]
    row = await pool.read_one("SELECT state FROM jobs WHERE id=?", (new_id,))
    assert row["state"] == "queued"


async def test_sync_endpoint_requires_bearer(client_with_pool_no_auth):
    r = await client_with_pool_no_auth.post("/api/v1/platforms/steam/library/sync")
    assert r.status_code == 401


async def test_sync_endpoint_503_on_db_unavailable(client_with_broken_pool):
    r = await client_with_broken_pool.post("/api/v1/platforms/steam/library/sync")
    assert r.status_code == 503
```

- [ ] **Step 5.2: Implement the endpoint**

```python
# src/orchestrator/api/routers/sync.py
"""POST /api/v1/platforms/steam/library/sync — manual library-sync trigger (BL11)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/platforms/steam/library", tags=["sync"])


@router.post(
    "/sync",
    responses={
        202: {"description": "Job queued or existing in-flight job returned"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_library_sync(pool: Pool = Depends(get_pool_dep)) -> JSONResponse:  # noqa: B008
    try:
        existing = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        if existing is not None:
            _log.info("sync.library.dedup_hit", existing_job_id=existing["id"])
            return JSONResponse(status_code=202, content={"job_id": existing["id"]})

        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
            ("library_sync", "steam"),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' "
            "AND state='queued' ORDER BY id DESC LIMIT 1"
        )
        if new_row is None:  # should never happen — execute_write succeeded
            raise PoolError("library_sync job inserted but not visible on read-back")
        _log.info("sync.library.queued", job_id=new_row["id"])
        return JSONResponse(status_code=202, content={"job_id": new_row["id"]})
    except PoolError as e:
        _log.error("sync.library.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
```

- [ ] **Step 5.3: Run endpoint tests**

```bash
.venv/bin/pytest tests/api/test_sync_router.py -xvs
```
Expected: all pass (after wiring the router in Task 7).

---

### Task 6: Auth auto-trigger

**Files:**
- Modify: `src/orchestrator/api/routers/auth.py`
- Modify: `tests/api/test_auth_router.py`

- [ ] **Step 6.1: Write the failing auto-trigger tests**

Add to `tests/api/test_auth_router.py`:

```python
async def test_auth_success_no_2fa_queues_library_sync_job(client_with_pool, mock_steam_authenticated):
    r = await client_with_pool.post(
        "/api/v1/platforms/steam/auth",
        json={"username": "u", "password": "p"},
    )
    assert r.status_code == 200
    pool = client_with_pool.app.state.pool
    rows = await pool.read_all(
        "SELECT kind, platform, state, source FROM jobs WHERE kind='library_sync'"
    )
    assert len(rows) == 1
    assert rows[0] == {"kind": "library_sync", "platform": "steam", "state": "queued", "source": "api"}


async def test_auth_complete_2fa_queues_library_sync_job(client_with_pool, mock_steam_2fa_then_success):
    begin = await client_with_pool.post(
        "/api/v1/platforms/steam/auth",
        json={"username": "u", "password": "p"},
    )
    challenge_id = begin.json()["challenge_id"]
    r = await client_with_pool.post(
        f"/api/v1/platforms/steam/auth/{challenge_id}",
        json={"code": "12345"},
    )
    assert r.status_code == 200
    pool = client_with_pool.app.state.pool
    rows = await pool.read_all("SELECT kind FROM jobs WHERE kind='library_sync'")
    assert len(rows) == 1


async def test_auth_success_db_failure_during_job_queue_does_not_fail_auth(...):
    """If the auth update succeeds but the library_sync INSERT raises,
    auth still returns 200. (Spec D7 + plan P9.)"""
    # ... arrange a pool whose 2nd execute_write raises PoolError ...
```

- [ ] **Step 6.2: Implement the auto-trigger**

In `src/orchestrator/api/routers/auth.py`, factor the queue-job into a helper:

```python
async def _queue_library_sync_job(pool: Pool) -> None:
    """Best-effort enqueue of a library_sync job after auth success.

    Plan P9: failures are logged but do NOT cause the auth response to
    fail — auth succeeded; the operator can manually re-sync.

    Plan P8: handler-side dedup is in the sync endpoint, NOT here, so an
    auth flow that races with an in-flight job will create one extra
    `queued` row. The worker will pick the first one; the second one
    will no-op when the in-flight sync upserts its rows (the second
    handler call is idempotent).
    """
    try:
        # Skip if there's already a queued/running job — avoid duplicate work.
        existing = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1"
        )
        if existing is not None:
            _log.info("auth.auto_sync.dedup_skip", existing_job_id=existing["id"])
            return
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
            ("library_sync", "steam"),
        )
        _log.info("auth.auto_sync.queued")
    except PoolError as e:
        _log.warning("auth.auto_sync.queue_failed", reason=str(e)[:200])
```

Then in BOTH auth-success paths (auth_begin no-2FA branch + auth_complete success), call `await _queue_library_sync_job(pool)` AFTER `_update_platform_row_success(...)`.

- [ ] **Step 6.3: Run auth tests**

```bash
.venv/bin/pytest tests/api/test_auth_router.py -xvs
```
Expected: existing tests still pass; 3 new tests pass.

---

### Task 7: Lifespan wiring

**Files:**
- Modify: `src/orchestrator/api/main.py`
- Modify: `tests/api/test_main_lifespan.py` (if exists, else extend an existing test)

- [ ] **Step 7.1: Write the failing lifespan tests**

Add tests to confirm:
1. `app.state.jobs_worker_task` is created at startup.
2. The task is `done()` after shutdown.
3. The sync router is mounted.

- [ ] **Step 7.2: Implement lifespan changes**

In `_lifespan()`, after steam worker startup:

```python
# 4. Jobs worker — spawn the background asyncio task
from orchestrator.jobs.worker import Deps, worker_loop

jobs_shutdown = asyncio.Event()
deps = Deps(pool=await get_pool(), steam_client=steam_client)
app.state.jobs_shutdown = jobs_shutdown
app.state.jobs_worker_task = asyncio.create_task(
    worker_loop(
        deps,
        shutdown=jobs_shutdown,
        poll_interval_sec=settings.jobs_worker_poll_interval_sec,
    ),
    name="jobs_worker",
)
log.info("api.boot.jobs_worker_started")
```

And in the shutdown finally block:

```python
log.info("api.shutdown.jobs_worker_stopping")
jobs_shutdown.set()
try:
    await asyncio.wait_for(app.state.jobs_worker_task, timeout=5.0)
except TimeoutError:
    log.warning("api.shutdown.jobs_worker_join_timeout")
    app.state.jobs_worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.jobs_worker_task
```

Wire the sync router:

```python
from orchestrator.api.routers.sync import router as sync_router
...
app.include_router(sync_router)
```

- [ ] **Step 7.3: Run lifespan + sync endpoint tests**

```bash
.venv/bin/pytest tests/api/test_sync_router.py tests/api/test_main_lifespan.py -xvs
```
Expected: all pass.

---

### Task 8: Full-suite green + security audit

- [ ] **Step 8.1: Run full test suite**

```bash
.venv/bin/pytest tests/ -q
```
Expected: ≥683 + ~25 new = ~708 pass. (1 pre-existing `test_licenses.py` failure is OK.)

- [ ] **Step 8.2: Run gates**

```bash
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/mypy --strict src/
gitleaks detect --no-banner
.venv/bin/semgrep --config=p/owasp-top-ten --error src/ 2>&1 | tail -10
```

All must be clean. ruff format will auto-fix on `ruff format src/ tests/` if needed.

- [ ] **Step 8.3: Security audit — manual review**

Per Phase 2.4 checklist; focus areas for BL11:
1. **Auth bypass** — Confirm sync endpoint goes through `BearerAuthMiddleware` (default route-protection; verified by test).
2. **SQL injection** — All queries use `?` placeholders. No string-formatted SQL in jobs/ or auth.py changes.
3. **Credential leakage** — `library.enumerate` IPC returns app metadata only; no token round-trip. Worker uses cached licenses, not env vars. Log fields are app_id/name/depots — public Steam catalog data.
4. **Resource exhaustion** — Library size bounded by steam-next's product info pagination; no unbounded loop. Worker IPC line size still capped at 10 MiB (BL10 guard); a 100k-app library would fit comfortably.
5. **Concurrent claim races** — Verified by `test_claim_next_job_atomic_under_concurrency`.

- [ ] **Step 8.4: Process-checklist marks**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
scripts/process-checklist.sh --complete-step build_loop:implemented
scripts/process-checklist.sh --complete-step build_loop:security_audit
```

---

### Task 9: Documentation

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`
- Modify: `PROJECT_BIBLE.md` (status line only — `<!-- Last Updated -->` markers as needed)
- Modify: `README.md` (add `jobs_worker_poll_interval_sec` to env-var table)
- (No new ADR — BL11 inherits ADR-0013 subprocess pattern; jobs-loop choice doesn't warrant its own ADR.)

- [ ] **Step 9.1: CHANGELOG entry**

```markdown
## [Unreleased] — BL11 Steam Library Sync — 2026-05-25

### Added
- `src/orchestrator/jobs/` package: generic asyncio job dispatcher
  (`worker.py`, `handlers/__init__.py` registry) with atomic
  SELECT-then-UPDATE claim under `BEGIN IMMEDIATE`.
- `library_sync` handler (`src/orchestrator/jobs/handlers/library_sync.py`)
  that calls `library.enumerate` on the steam worker and upserts the
  `games` table.
- `POST /api/v1/platforms/steam/library/sync` — manual trigger with
  handler-side dedup of in-flight jobs.
- `library.enumerate` IPC op on the steam worker subprocess.
- `SteamWorkerClient.library_enumerate()` async method.
- Auto-queue `library_sync` job after both Steam auth success paths
  (no-2FA and 2FA), best-effort.

### Changed
- FastAPI lifespan now spawns + cleanly stops the jobs worker
  asyncio task (5 s shutdown timeout, then cancel).

### Infrastructure
- New Settings field `jobs_worker_poll_interval_sec` (default 1.0,
  range 0.05–300.0, warn above 60.0).

### Documentation
- BL11 feature added to FEATURES.md.
- README env-var table updated.

### Security
- No new third-party dependencies. SQL parameterized. No token
  round-trip on the new IPC op. Worker loop isolates handler
  crashes (verified by `test_worker_loop_isolates_handler_crash`).
```

- [ ] **Step 9.2: FEATURES.md entry**

Append a new "Feature 11: BL11 — Steam Library Sync" section following the BL10 template — Status, Summary, Key Interfaces, Related ADRs (point to ADR-0013), Test Coverage (N new tests), Known Limitations (concurrent-multi-job deferred per D10; auto-trigger race between auth+manual POST may create extra queued row that no-ops idempotently).

- [ ] **Step 9.3: README env-var table**

Add one row to the env-var table:

```
| `ORCH_JOBS_WORKER_POLL_INTERVAL_SEC` | float (0.05..300.0) | `1.0` | Warns if >60 |
```

- [ ] **Step 9.4: PROJECT_BIBLE.md status marker**

Update the `<!-- Last Updated -->` marker on §1.2 (MVP Must-Haves) and §3.4 (Process topology) to today's date and add one line noting "BL11 ships the asyncio jobs worker (single-loop, atomic SELECT-then-UPDATE claim)".

- [ ] **Step 9.5: Process-checklist documentation step**

```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```

---

### Task 10: Combined commit + PR

- [ ] **Step 10.1: Final gate check**

```bash
.venv/bin/ruff check src/ tests/ && \
.venv/bin/ruff format --check src/ tests/ && \
.venv/bin/mypy --strict src/ && \
gitleaks detect --no-banner 2>&1 | tail -3 && \
.venv/bin/pytest tests/ -q 2>&1 | tail -10
```

- [ ] **Step 10.2: Record the feature**

```bash
scripts/test-gate.sh --record-feature "BL11-library-sync"
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
```

This will increment the test-gate counter to 2/2 → **UAT-6 will be required before BL12 starts**.

- [ ] **Step 10.3: Stage + commit**

```bash
git add -A  # all BL11 files
git commit -m "$(cat <<'EOF'
feat(jobs+platform/steam): BL11 library sync — F1 milestone 2/3

Operationalize Steam library enumeration end-to-end:

- New `src/orchestrator/jobs/` package with generic asyncio dispatcher
  (single-loop, atomic SELECT-then-UPDATE claim under BEGIN IMMEDIATE
  per spec D10 + P2). Handler registry indexed by jobs.kind.
- `library_sync` handler upserts owned Steam apps into the `games`
  table via INSERT...ON CONFLICT(platform, app_id) DO UPDATE.
  Idempotent re-sync; existing status/cached_version preserved.
- `library.enumerate` IPC op on the steam worker subprocess uses
  steam-next product_info iteration pattern established in Spike A.
- `POST /api/v1/platforms/steam/library/sync` manual trigger with
  handler-side dedup of queued|running jobs.
- Both Steam auth-success paths auto-queue a `library_sync` job
  (best-effort; queue failures logged but don't fail the auth).
- FastAPI lifespan spawns + cleanly stops the jobs worker
  (5 s shutdown timeout, then cancel).

Test coverage: ~25 new tests across tests/jobs/ and tests/api/.
Full suite green; ruff/mypy/gitleaks/semgrep clean.

Spec: docs/superpowers/specs/2026-05-24-f1-steam-credentials-fetcher-design.md §5
Plan: docs/superpowers/plans/2026-05-25-bl11-library-sync.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 10.4: Push + open PR**

```bash
git push -u origin feat/bl11-library-sync
gh pr create --title "feat(jobs+platform/steam): BL11 library sync — F1 milestone 2/3" --body "..."
```

PR body should reference: spec §5, plan, test counts, UAT-6 gate triggering after merge.

- [ ] **Step 10.5: Stop — wait for user merge**

Per `feedback_pr_merge_ownership`: do NOT call `gh pr merge`. Report the PR URL and stop.

---

## Self-Review (writing-plans skill §Self-Review)

**Spec coverage:**
- §5.1 (files) → Task 2/3/4/5
- §5.2 (modified files) → Task 6/7
- §5.3 (jobs worker design) → Task 2 (write_transaction-based atomic claim — P2 deviation from spec's UPDATE...RETURNING; rationale documented)
- §5.4 (library upsert SQL) → Task 3 (handler)
- §5.5 (auto-trigger) → Task 6
- §5.6 (manual endpoint + dedup) → Task 5
- §5.7 (~25 tests) → Tasks 2/3/5/6 collectively

**Placeholder scan:** Step 4.2 (worker test) and Step 4.5 (client method) reference "match BL10 pattern" rather than spelling out the full plumbing — this is **deliberate**, because the existing test fixtures are the source of truth; pretending to know their exact shape risks a non-applying example. Execution will read the existing `tests/platform/steam/test_client.py` first and then mirror its idioms.

**Type consistency:** `Deps` dataclass defined once (Task 2), referenced consistently in handler signature (Task 3) and lifespan wiring (Task 7). `Handler` type alias defined in `handlers/__init__.py`.

**Decision deviation from spec:** P2 (write_transaction SELECT-then-UPDATE) instead of spec §5.3's `UPDATE...RETURNING`. Rationale: BL4 pool's `execute_write` discards cursor data; adding `execute_write_returning` is out of BL11 scope. The write_transaction approach is atomic and tested.
