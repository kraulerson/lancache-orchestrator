# F12 — Scheduler subsystem (design)

**Date:** 2026-05-28
**Spec source:** PROJECT_BIBLE §1.2 (MVP must-have F12), §3.1 (APScheduler in main asyncio loop), §1.5 JQ3 (`/api/v1/health` surfaces `scheduler_running: bool` and returns 503 on scheduler death), FRD §2.
**Scope:** Periodic scheduled triggers for `library_sync` (default every 6h) via APScheduler 3.11.2 AsyncIOScheduler. Flips the BL5 `/health.scheduler_running` stub-false to real.

## Locked decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **APScheduler 3.11.2 `AsyncIOScheduler`** | Pinned via requirements.txt; matches our asyncio loop. v4 API exists but adds SQLAlchemy + event broker complexity we don't need. |
| D2 | **In-memory job store (MemoryJobStore — APScheduler default)** | Scheduled jobs re-register on every boot. No persistence needed; ID6 reaper already handles "stale `running` rows on restart". Avoids a SQLAlchemy/aiosqlite contention layer. |
| D3 | **`AsyncIOExecutor` (default)** | All scheduled callbacks are `async def`. |
| D4 | **`max_instances=1`** per scheduled job | If the previous library_sync is still running when the next fire time arrives, skip (don't pile up). |
| D5 | **`misfire_grace_time=None`** | Always fire on next opportunity even if late — operator may have rebooted mid-interval and we want the next sync to run, not be skipped. |
| D6 | **Scheduler invokes a thin "enqueue" function**, not the handler directly | The enqueue function inserts a `jobs` row (kind='library_sync', source='scheduler'); the BL11 jobs worker picks it up. Decouples cron firing from handler execution → handler can be slow without blocking the scheduler. |
| D7 | **Dedup at enqueue time** — skip insert if a queued/running `library_sync` exists | Same query the manual sync endpoint uses (`POST /library/sync`). Prevents pile-up after a long outage. |
| D8 | **`scheduler_enabled: bool = True`** Settings field | Diagnostic / dev escape hatch; container boots with scheduler disabled when `ORCH_SCHEDULER_ENABLED=false`. |
| D9 | **`scheduler_library_sync_interval_sec: int = 21600` (6h)** Settings field | Configurable in [60, 86400] range (1 min .. 24h). FRD says "6h default"; tunable for testing. |
| D10 | **`SchedulerManager.running` property** drives `/health.scheduler_running` | Property delegates to the underlying `AsyncIOScheduler.running` plus a "not None" check. |
| D11 | **Scheduler errors do NOT crash boot** | If `scheduler.start()` raises, log critical, set running=False, continue. `/health` will reflect 503 via JQ3. |
| D12 | **Lifespan shutdown calls `scheduler.shutdown(wait=True)`** | Wait for in-flight callbacks (the enqueue function is fast — <1s typically). |

## Files

- `src/orchestrator/scheduler/__init__.py` — package marker
- `src/orchestrator/scheduler/manager.py` — `SchedulerManager` class wrapping `AsyncIOScheduler`
- `src/orchestrator/scheduler/jobs.py` — cron callbacks (`enqueue_library_sync(pool)`)
- `tests/scheduler/test_jobs.py` — enqueue function: empty, dedup-hit, dedup-miss, DB failure
- `tests/scheduler/test_manager.py` — manager: starts, stops, exposes `.running`, registers configured jobs
- `tests/api/test_lifespan_scheduler.py` — integration: lifespan starts scheduler, `/health.scheduler_running=True`

## Wire-up

`main.py` lifespan order after this lands:

1. Migrations
2. Pool init
2b. Job reaper (ID6, already there)
3. Steam worker
4. Jobs worker (BL11)
4b. **Scheduler (this BL)** — depends on pool, runs cron callbacks that enqueue into the jobs table
5. Lancache probe (ID2)
6. Boot metadata, etc.

Shutdown order (LIFO):
- Scheduler shutdown FIRST (so no new jobs enqueue during teardown)
- Then jobs worker stops
- Then steam client
- Then pool

## Tests strategy

- Unit: pure tests for `enqueue_library_sync` using a real pool fixture (no scheduler)
- Manager: construct a `SchedulerManager` with `AsyncIOScheduler`, call `.start()`, assert `.running`, verify jobs registered via `get_jobs()`, `.shutdown()` cleanly
- Integration: lifespan_app fixture boots scheduler, `/health` returns `scheduler_running: True`. Real fire-on-interval is NOT exercised (interval is 6h default; tests would have to wait or use 1s interval + cooperative sleep — leave for live UAT)

## Out of scope (explicit)

- Spike F (perf gate against asyncio-only design) — pre-shipped per Bible §3.1, no scheduler-specific perf test needed
- Per-platform sync customization (F12 in spec is "library_sync" only; F2 Epic adds its own schedule when it ships)
- F13 weekly full-library validation sweep — separate scheduled job, separate BL
- Distributed scheduler coordination — out of scope (single-orchestrator deployment)
