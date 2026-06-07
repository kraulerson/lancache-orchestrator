# F13 Scheduled Validation Sweep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly cron job that re-runs F7 disk-stat validation across the cached Steam library (status `up_to_date` + `validation_failed`) in batches of 10, to catch LRU eviction drift and recovery.

**Architecture:** Follows the F12 pattern — the scheduler fires a thin, never-raises `enqueue_validation_sweep` callback that inserts one `sweep` job (DB-deduped to one in-flight via a partial unique index); the jobs worker claims it and runs `sweep_handler` inline, which pre-flight-skips on validator-unhealthy, enumerates the candidate games, and validates them 10-at-a-time through a `validate_one_game()` helper extracted from the existing validate handler (so both paths share identical record/status logic).

**Tech Stack:** Python 3.12, FastAPI, APScheduler 3.x (`CronTrigger.from_crontab`), aiosqlite pool, structlog, Pydantic-settings.

**Conventions:** `.venv/bin/pytest`; `ruff check` + `ruff format` (tests too); `mypy --strict src/`; gitleaks; semgrep (raw `sqlite3` only in `test_migrate.py`; parameterized SQL only). **No per-task commits** — implement every task TDD-style (write failing test → verify red → implement → verify green), then Task 8 is the single gate-sweep + audit + commit + PR.

**Facts to rely on (verified):** `jobs.kind` CHECK already includes `'sweep'`; `jobs.platform` is nullable; `jobs.source` ∈ (`scheduler`,`cli`,`gameshelf`,`api`); `games.status` ∈ (`unknown`,`not_downloaded`,`up_to_date`,`pending_update`,`downloading`,`validation_failed`,`blocked`,`failed`); `validator_self_test(settings: Settings) -> bool` lives in `validator/self_test.py`; `ValidationResult` (in `validator/disk_stat.py`) has `.outcome` (`cached`/`partial`/`missing`/`error`), `.manifest_version`, `.chunks_total`, `.chunks_cached`, `.chunks_missing`, `.error`.

---

### Task 1: Settings — sweep cron/enabled/batch-size with fail-fast cron validation

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_settings.py`:
```python
def test_sweep_settings_defaults():
    from orchestrator.core.settings import Settings

    s = Settings(orchestrator_token="a" * 32)
    assert s.validation_sweep_enabled is True
    assert s.validation_sweep_cron == "0 3 * * 0"
    assert s.sweep_batch_size == 10


def test_invalid_sweep_cron_fails_fast():
    import pytest
    from pydantic import ValidationError

    from orchestrator.core.settings import Settings

    with pytest.raises(ValidationError):
        Settings(orchestrator_token="a" * 32, validation_sweep_cron="not a cron")


def test_sweep_batch_size_must_be_ge_1():
    import pytest
    from pydantic import ValidationError

    from orchestrator.core.settings import Settings

    with pytest.raises(ValidationError):
        Settings(orchestrator_token="a" * 32, sweep_batch_size=0)
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/core/test_settings.py::test_sweep_settings_defaults tests/core/test_settings.py::test_invalid_sweep_cron_fails_fast tests/core/test_settings.py::test_sweep_batch_size_must_be_ge_1 -q`
Expected: FAIL (`AttributeError`/no such field).

- [ ] **Step 3: Implement**

In `src/orchestrator/core/settings.py`, add the import near the top (with the other third-party imports):
```python
from apscheduler.triggers.cron import CronTrigger
```
Add these fields to the `Settings` model, next to `scheduler_enabled` / `scheduler_library_sync_interval_sec`:
```python
    # F13 — scheduled validation sweep.
    validation_sweep_enabled: bool = True
    validation_sweep_cron: str = "0 3 * * 0"  # 5-field cron (min hour dom mon dow), UTC
    sweep_batch_size: int = Field(default=10, ge=1)
```
Add a validator method on the model (mirror the existing `_validate_cache_levels` field_validator style):
```python
    @field_validator("validation_sweep_cron", mode="after")
    @classmethod
    def _validate_sweep_cron(cls, v: str) -> str:
        """Fail-fast on a malformed cron (IS2) by constructing the trigger now."""
        try:
            CronTrigger.from_crontab(v)
        except Exception as e:  # apscheduler raises ValueError on bad expressions
            raise ValueError(f"invalid validation_sweep_cron {v!r}: {e}") from e
        return v
```
(`field_validator` and `Field` are already imported in this file — confirm; if not, add `from pydantic import Field, field_validator`.)

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/core/test_settings.py -q`
Expected: PASS.

---

### Task 2: Migration 0005 — single-in-flight sweep unique index

**Files:**
- Create: `src/orchestrator/db/migrations/0005_jobs_sweep_unique.sql`
- Modify: `src/orchestrator/db/migrations/CHECKSUMS`
- Test: `tests/db/test_sweep_dedup.py` (pool-based, so no raw `sqlite3` — semgrep-safe)

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_sweep_dedup.py`:
```python
"""F13: the migration-0005 partial unique index allows at most one in-flight sweep."""

from __future__ import annotations

import pytest

from orchestrator.db.pool import IntegrityViolationError

pytestmark = pytest.mark.asyncio


async def test_only_one_inflight_sweep_allowed(pool):
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('sweep', 'queued', 'scheduler')"
    )
    with pytest.raises(IntegrityViolationError):
        await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) VALUES ('sweep', 'queued', 'scheduler')"
        )


async def test_finished_sweep_does_not_block_new(pool):
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('sweep', 'succeeded', 'scheduler')"
    )
    # A new queued sweep is allowed once the prior one is no longer in-flight.
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('sweep', 'queued', 'scheduler')"
    )
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/db/test_sweep_dedup.py -q`
Expected: FAIL — `test_only_one_inflight_sweep_allowed` does NOT raise (no unique index yet).

- [ ] **Step 3: Implement the migration**

Create `src/orchestrator/db/migrations/0005_jobs_sweep_unique.sql`:
```sql
-- 0005_jobs_sweep_unique.sql
-- F13: at most one queued/running validation `sweep` job at a time. Mirrors the
-- library_sync inflight guard (0004). The cron enqueue uses
-- INSERT ... ON CONFLICT DO NOTHING; the worker's queued -> running ->
-- succeeded/failed transitions keep at most one row in this partial index.

-- Cancel any pre-existing duplicate in-flight sweeps before creating the index,
-- so it applies cleanly to already-deployed databases. Keep the earliest.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight sweep (migration 0005 dedup)'
WHERE kind = 'sweep'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'sweep' AND state IN ('queued', 'running')
  );

CREATE UNIQUE INDEX idx_jobs_sweep_inflight
    ON jobs(kind)
    WHERE kind = 'sweep' AND state IN ('queued', 'running');
```

- [ ] **Step 4: Update CHECKSUMS**

Run: `.venv/bin/python -m orchestrator.db.migrate_tools regenerate-checksums`
Then confirm a `0005  <sha256>  0005_jobs_sweep_unique.sql` line was appended to `src/orchestrator/db/migrations/CHECKSUMS`.

- [ ] **Step 5: Run to verify green**

Run: `.venv/bin/pytest tests/db/test_sweep_dedup.py tests/db/test_migrate.py -q`
Expected: PASS (both new tests + the existing migration/checksum tests).

---

### Task 3: `enqueue_validation_sweep` cron callback

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py`
- Test: `tests/scheduler/test_jobs.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/scheduler/test_jobs.py`:
```python
async def test_enqueue_validation_sweep_inserts_one_row(pool):
    from orchestrator.scheduler.jobs import enqueue_validation_sweep

    n = await enqueue_validation_sweep(pool)
    assert n == 1
    row = await pool.read_one(
        "SELECT kind, platform, state, source FROM jobs WHERE kind='sweep'"
    )
    assert row == {"kind": "sweep", "platform": None, "state": "queued", "source": "scheduler"}


async def test_enqueue_validation_sweep_dedup_skips(pool):
    from orchestrator.scheduler.jobs import enqueue_validation_sweep

    assert await enqueue_validation_sweep(pool) == 1
    assert await enqueue_validation_sweep(pool) == 0  # dedup: one in-flight already


async def test_enqueue_validation_sweep_swallows_db_error():
    from orchestrator.db.pool import PoolError
    from orchestrator.scheduler.jobs import enqueue_validation_sweep

    class _BrokenPool:
        async def execute_write(self, *_a, **_kw):
            raise PoolError("boom")

    assert await enqueue_validation_sweep(_BrokenPool()) == 0  # never raises
```
(These mirror the existing `enqueue_library_sync` tests in this file — match its `pool` fixture usage.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/scheduler/test_jobs.py -q -k validation_sweep`
Expected: FAIL (`ImportError`: cannot import `enqueue_validation_sweep`).

- [ ] **Step 3: Implement**

In `src/orchestrator/scheduler/jobs.py`, add after `enqueue_library_sync`:
```python
async def enqueue_validation_sweep(pool: Pool) -> int:
    """Insert a `sweep` job row if none is queued/running (F13).

    Mirrors `enqueue_library_sync`: at most one in-flight sweep (DB-enforced by
    `idx_jobs_sweep_inflight`, migration 0005) via `ON CONFLICT DO NOTHING`.
    Returns the rowcount (1 queued / 0 deduped-or-failed). Never raises — a
    failing scheduler tick must not degrade APScheduler.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) "
            "VALUES ('sweep', 'queued', 'scheduler') ON CONFLICT DO NOTHING"
        )
        if inserted:
            _log.info("scheduler.sweep.queued")
        else:
            _log.info("scheduler.sweep.dedup_skip")
        return inserted
    except PoolError as e:
        _log.error("scheduler.sweep.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.sweep.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/scheduler/test_jobs.py -q`
Expected: PASS.

---

### Task 4: Scheduler — register the validation-sweep cron job

**Files:**
- Modify: `src/orchestrator/scheduler/manager.py`
- Test: `tests/scheduler/test_manager.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/scheduler/test_manager.py` (match the file's existing construction of `SchedulerManager` + the `pool` fixture; use `_wait_until_*`/poll helpers if the file already defines them):
```python
async def test_sweep_job_registered_when_enabled(pool):
    from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID, SchedulerManager

    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        validation_sweep_enabled=True,
        validation_sweep_cron="0 3 * * 0",
    )
    await mgr.start()
    try:
        assert VALIDATION_SWEEP_JOB_ID in mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()


async def test_sweep_job_absent_when_disabled(pool):
    from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID, SchedulerManager

    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        validation_sweep_enabled=False,
        validation_sweep_cron="0 3 * * 0",
    )
    await mgr.start()
    try:
        assert VALIDATION_SWEEP_JOB_ID not in mgr.get_registered_job_ids()
        # library_sync still registered — disabling the sweep doesn't disable the scheduler
        assert "library_sync_steam" in mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/scheduler/test_manager.py -q -k sweep`
Expected: FAIL (`ImportError` for `VALIDATION_SWEEP_JOB_ID` / unexpected kwargs).

- [ ] **Step 3: Implement**

In `src/orchestrator/scheduler/manager.py`:

Add the import next to the IntervalTrigger import:
```python
from apscheduler.triggers.cron import CronTrigger
```
Add the import of the new callback:
```python
from orchestrator.scheduler.jobs import enqueue_library_sync, enqueue_validation_sweep
```
Add the job-id constant next to `LIBRARY_SYNC_JOB_ID`:
```python
VALIDATION_SWEEP_JOB_ID = "validation_sweep"
```
Extend `__init__` to accept and store the two new args:
```python
    def __init__(
        self,
        *,
        pool: Pool,
        enabled: bool,
        library_sync_interval_sec: int,
        validation_sweep_enabled: bool,
        validation_sweep_cron: str,
    ) -> None:
        self._pool = pool
        self._enabled = enabled
        self._library_sync_interval_sec = library_sync_interval_sec
        self._validation_sweep_enabled = validation_sweep_enabled
        self._validation_sweep_cron = validation_sweep_cron
        self._scheduler = None
        self._lock = asyncio.Lock()
```
In `start()`, immediately after the existing `scheduler.add_job(enqueue_library_sync, ...)` block and before `scheduler.start()`, add:
```python
            if self._validation_sweep_enabled:
                scheduler.add_job(
                    enqueue_validation_sweep,
                    trigger=CronTrigger.from_crontab(
                        self._validation_sweep_cron, timezone="UTC"
                    ),
                    args=(self._pool,),
                    id=VALIDATION_SWEEP_JOB_ID,
                    name="Enqueue validation sweep",
                    replace_existing=True,
                )
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/scheduler/test_manager.py -q`
Expected: PASS (new + existing manager tests).

---

### Task 5: Extract `validate_one_game` (DRY for validate handler + sweep)

**Files:**
- Modify: `src/orchestrator/jobs/handlers/validate.py`
- Test: `tests/jobs/test_validate_handler.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/jobs/test_validate_handler.py` (reuses the file's `_seed_game`/`_seed_manifest`/`_make_cache_file`/`cache_root`/`_StubSteam` helpers):
```python
async def test_validate_one_game_returns_result_and_records(pool, cache_root):
    from orchestrator.core.settings import get_settings
    from orchestrator.jobs.handlers.validate import validate_one_game

    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    _make_cache_file(cache_root, 731, SHA_A)
    _make_cache_file(cache_root, 731, SHA_B)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))

    result = await validate_one_game(pool, deps, game_id, get_settings())

    assert result.outcome == "cached"
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "cached"
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_validate_handler.py::test_validate_one_game_returns_result_and_records -q`
Expected: FAIL (`ImportError`: cannot import `validate_one_game`).

- [ ] **Step 3: Implement the extraction**

In `src/orchestrator/jobs/handlers/validate.py`, add a new function and refactor the handler to call it. The new function contains the current handler body from the `started_row` line through the status update; it takes `settings` as a param (instead of calling `get_settings()` internally) and returns the `ValidationResult`:
```python
async def validate_one_game(
    pool: Pool, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate one game against the on-disk cache, record a validation_history
    row, and update games.status. Shared by the validate job handler (F7) and the
    scheduled sweep (F13). Assumes the caller has confirmed steam_client and the
    game's steam platform."""
    started_row = await pool.read_one("SELECT CURRENT_TIMESTAMP AS t")
    started_at = started_row["t"] if started_row is not None else None

    result = await validate_game(pool, deps, game_id, settings)

    await pool.execute_write(
        _INSERT_VH,
        (
            game_id,
            result.manifest_version,
            started_at,
            result.chunks_total,
            result.chunks_cached,
            result.chunks_missing,
            result.outcome,
            (result.error[:200] if result.error else None),
        ),
    )

    new_status = _STATUS_FOR.get(result.outcome)
    if new_status is not None:
        await pool.execute_write(
            "UPDATE games SET status=?, last_validated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, game_id),
        )
    else:
        # outcome='error' (infra failure). Never clobber a classified status, but
        # resolve the transient 'downloading' so a freshly-prefilled game isn't
        # stuck (UAT-10 #3).
        await pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=? AND status='downloading'",
            ((f"validate: {result.error}"[:200] if result.error else "validate: error"), game_id),
        )
    return result
```
Update the imports/`TYPE_CHECKING` block so `Pool`, `Settings`, and `ValidationResult` are available:
```python
from orchestrator.validator.disk_stat import ValidationResult, validate_game
...
if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
    from orchestrator.db.pool import Pool
    from orchestrator.jobs.worker import Deps
```
Replace the body of `validate_handler` from the `started_row = ...` line through the status-update `else:` block with a single call, keeping the guards and the trailing `validate.recorded` log:
```python
    settings = get_settings()
    _log.info("validate.started", job_id=job_id, game_id=game_id)

    result = await validate_one_game(deps.pool, deps, game_id, settings)

    _log.info(
        "validate.recorded",
        job_id=job_id,
        game_id=game_id,
        outcome=result.outcome,
        total=result.chunks_total,
        cached=result.chunks_cached,
        missing=result.chunks_missing,
    )
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_validate_handler.py -q`
Expected: PASS — the new test plus every pre-existing validate test (parity preserved).

---

### Task 6: `sweep_handler` + registration

**Files:**
- Create: `src/orchestrator/jobs/handlers/sweep.py`
- Modify: `src/orchestrator/jobs/handlers/__init__.py`
- Test: `tests/jobs/test_sweep_handler.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/jobs/test_sweep_handler.py`:
```python
"""F13: scheduled validation sweep handler."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


class _StubSteam:
    def __init__(self, response):
        self._response = response

    async def manifest_expand(self, raw: bytes):
        return self._response


def _job():
    return {"id": 1, "kind": "sweep", "platform": None, "game_id": None}


async def _seed(pool, *, platform="steam", status="up_to_date", app_id="730"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, 't', 1, ?)",
        (platform, app_id, status),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_sweep_skips_when_validator_unhealthy(pool, tmp_path, monkeypatch):
    # Point the cache path at a non-existent dir → validator_self_test False.
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path / "nope"))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    await _seed(pool)
    # Must NOT raise (skip + succeed); the game is untouched.
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"
    get_settings.cache_clear()


async def test_sweep_validates_only_candidate_steam_games(pool, cache_root, monkeypatch):
    # candidates: up_to_date + validation_failed steam. NOT: epic, blocked, not_downloaded.
    g_ok = await _seed(pool, status="up_to_date", app_id="1")
    g_vf = await _seed(pool, status="validation_failed", app_id="2")
    await _seed(pool, status="blocked", app_id="3")
    await _seed(pool, status="not_downloaded", app_id="4")
    await _seed(pool, platform="epic", status="up_to_date", app_id="5")

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(
            chunks_total=1, chunks_cached=1, chunks_missing=0,
            outcome="cached", manifest_version="100", error=None,
        )

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    assert sorted(seen) == sorted([g_ok, g_vf])


async def test_sweep_isolates_per_game_errors(pool, cache_root, monkeypatch):
    g1 = await _seed(pool, app_id="1")
    g2 = await _seed(pool, app_id="2")

    validated: list[int] = []

    async def flaky_validate_one(pool_, deps_, game_id, settings):
        from orchestrator.validator.disk_stat import ValidationResult

        if game_id == g1:
            raise RuntimeError("boom")
        validated.append(game_id)
        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", flaky_validate_one)
    # One game raising must NOT abort the sweep.
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    assert validated == [g2]


async def test_sweep_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "sweep" in HANDLERS
```
(`ValidationResult` field order above matches `validator/disk_stat.py`: `chunks_total, chunks_cached, chunks_missing, outcome, manifest_version, error`. Confirm and adjust the positional construction if the dataclass differs.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_sweep_handler.py -q`
Expected: FAIL (`ModuleNotFoundError: orchestrator.jobs.handlers.sweep`).

- [ ] **Step 3: Implement the handler**

Create `src/orchestrator/jobs/handlers/sweep.py`:
```python
"""F13 — scheduled validation sweep handler.

Re-runs F7 disk-stat validation across the cached Steam library (status
up_to_date + validation_failed) in batches, to catch LRU eviction drift and
recovery. Pre-flight-skips on validator-unhealthy; per-game errors are isolated.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.validator.self_test import validator_self_test

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE platform='steam' AND status IN ('up_to_date','validation_failed') "
    "ORDER BY id"
)


async def sweep_handler(job: dict[str, Any], deps: Deps) -> None:
    """Validate every cached, non-blocked Steam game in batches (F13).

    Best-effort: an unhealthy validator or a missing steam client is a SKIP (the
    job succeeds — nothing to do), and a per-game failure never aborts the sweep.
    """
    job_id = job.get("id")
    settings = get_settings()

    if deps.steam_client is None:
        _log.info("sweep.skipped", job_id=job_id, reason="no_steam_client")
        return
    if not await validator_self_test(settings):
        _log.info("sweep.skipped", job_id=job_id, reason="validator_unhealthy")
        return

    rows = await deps.pool.read_all(_CANDIDATE_SQL)
    _log.info("sweep.started", job_id=job_id, candidates=len(rows))

    sem = asyncio.Semaphore(settings.sweep_batch_size)
    counts = {"cached": 0, "partial": 0, "missing": 0, "error": 0}
    errors = 0
    evicted = 0
    recovered = 0
    lock = asyncio.Lock()

    async def _one(game_id: int, prior: str) -> None:
        nonlocal errors, evicted, recovered
        async with sem:
            try:
                result = await validate_one_game(deps.pool, deps, game_id, settings)
            except Exception as e:  # isolate — one bad game never aborts the sweep
                async with lock:
                    errors += 1
                _log.warning(
                    "sweep.game_error", job_id=job_id, game_id=game_id,
                    error=type(e).__name__, reason=str(e)[:200],
                )
                return
            async with lock:
                counts[result.outcome] = counts.get(result.outcome, 0) + 1
                if prior == "up_to_date" and result.outcome != "cached":
                    evicted += 1
                elif prior == "validation_failed" and result.outcome == "cached":
                    recovered += 1

    await asyncio.gather(*(_one(int(r["id"]), str(r["status"])) for r in rows))

    _log.info(
        "sweep.completed",
        job_id=job_id,
        total=len(rows),
        cached=counts["cached"],
        validation_failed=counts["partial"] + counts["missing"],
        evicted=evicted,
        recovered=recovered,
        errors=errors,
    )
```

- [ ] **Step 4: Register the handler**

In `src/orchestrator/jobs/handlers/__init__.py`, inside `_register_builtin_handlers()`, add the import and registration:
```python
    from orchestrator.jobs.handlers.sweep import sweep_handler
    ...
    register("sweep", sweep_handler)
```

- [ ] **Step 5: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_sweep_handler.py -q`
Expected: PASS.

---

### Task 7: Wire the new settings into `SchedulerManager` at boot

**Files:**
- Modify: `src/orchestrator/api/main.py`
- Test: `tests/api/test_app_boot.py` (or wherever the scheduler-manager boot is asserted; if no such test exists, add the assertion to the existing app-boot/lifespan test)

- [ ] **Step 1: Write the failing test**

Add a test that the constructed manager registers the sweep job at boot (adjust the fixture to the file's existing app/lifespan harness):
```python
async def test_boot_registers_validation_sweep_job(app_with_lifespan):
    mgr = app_with_lifespan.state.scheduler_manager
    from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID

    assert VALIDATION_SWEEP_JOB_ID in mgr.get_registered_job_ids()
```
If there is no existing lifespan-boot fixture to reuse, instead assert via a direct unit check that `main.py` passes the settings through — but prefer reusing the existing boot test harness.

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest -q -k validation_sweep_job`
Expected: FAIL (`TypeError`: missing kwargs / job not registered).

- [ ] **Step 3: Implement**

In `src/orchestrator/api/main.py`, extend the `SchedulerManager(...)` construction (~line 178) with the two new args:
```python
    scheduler_manager = SchedulerManager(
        pool=get_pool(),
        enabled=settings.scheduler_enabled,
        library_sync_interval_sec=settings.scheduler_library_sync_interval_sec,
        validation_sweep_enabled=settings.validation_sweep_enabled,
        validation_sweep_cron=settings.validation_sweep_cron,
    )
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest -q -k validation_sweep_job`
Expected: PASS.

---

### Task 8: Gate sweep + security audit + adversarial review + docs + commit/PR

**No code changes beyond docs.** This task closes the build loop.

- [ ] **Step 1: Start the build loop**

Run: `scripts/process-checklist.sh --start-feature "f13-scheduled-sweep"`

- [ ] **Step 2: Full gate sweep — all must be green**

```bash
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/pytest -q
.venv/bin/mypy --strict src/
.venv/bin/ruff check .
.venv/bin/ruff format --check .
gitleaks detect --no-banner --redact
semgrep --config .semgrep/orchestrator-rules.yaml src/ --error --quiet
```
Fix anything that fails, then re-run. (License test passes only with the venv on PATH.)

- [ ] **Step 3: Adversarial-verify Workflow over the batch**

Run a multi-agent review (the last several batches each caught a real defect) covering: the cron/enqueue path (never-raises, dedup), the sweep handler (skip semantics, enumeration filter correctness, per-game isolation, summary tallies, concurrency bound, no event-loop starvation), the `validate_one_game` extraction (behavioral parity), migration 0005 (the partial-index invariant + the cleanup UPDATE), and settings cron fail-fast. Fix any confirmed findings test-first, re-green.

- [ ] **Step 4: Security audit doc**

Write `docs/security-audits/f13-scheduled-sweep-security-audit.md` (threat review: the sweep touches no credentials and no untrusted input; SQL is constant/parameterized; DoS surface = the batch-of-10 bound + weekly cadence; validator-skip avoids failure storms). Record 0 open findings (or fix + record).

- [ ] **Step 5: CHANGELOG + FEATURES**

- `CHANGELOG.md` → new `### Added — F13 Scheduled Validation Sweep` entry under `[Unreleased]` (8-category format).
- `FEATURES.md` → add the F13 row/section.

- [ ] **Step 6: Mark build-loop steps**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
scripts/process-checklist.sh --complete-step build_loop:implemented
scripts/process-checklist.sh --complete-step build_loop:security_audit
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```

- [ ] **Step 7: Commit + PR**

Bring the A/B/C commit-structure options to the user FIRST (standing rule), then a single `feat(f13): ...` commit on `feat/f13-scheduled-sweep`, push, open a PR. The user merges (never `gh pr merge`).

---

## Self-Review

**Spec coverage:** trigger (Task 4) · cron/enabled/batch settings + fail-fast (Task 1) · enqueue callback + dedup (Tasks 2/3) · sweep handler with pre-flight skip, enumeration, batch-of-10, per-game isolation, summary (Task 6) · validate_one_game refactor (Task 5) · boot wiring (Task 7) · migration 0005 (Task 2) · no new table ✓ · steam-only + up_to_date/validation_failed ✓ · deferred items untouched ✓. All spec sections map to a task.

**Type consistency:** `enqueue_validation_sweep(pool) -> int`, `validate_one_game(pool, deps, game_id, settings) -> ValidationResult`, `sweep_handler(job, deps) -> None`, `VALIDATION_SWEEP_JOB_ID = "validation_sweep"`, settings `validation_sweep_enabled`/`validation_sweep_cron`/`sweep_batch_size` — names identical across all tasks and the spec.

**Placeholder scan:** no TBD/TODO; every code step shows full code. The one verify-before-use note (ValidationResult field order) is an explicit "confirm against disk_stat.py" instruction, not a placeholder.
