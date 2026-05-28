# Security Audit — F12 Scheduler Subsystem

**Feature:** F12-scheduler-subsystem
**Audit date:** 2026-05-28
**Audited modules:**
- src/orchestrator/scheduler/__init__.py
- src/orchestrator/scheduler/manager.py
- src/orchestrator/scheduler/jobs.py
- src/orchestrator/api/main.py (lifespan + shutdown wiring)
- src/orchestrator/api/routers/health.py (running flag)
- src/orchestrator/core/settings.py (2 new fields)

<!-- Last Updated: 2026-05-28 -->

## Scope

Post-implementation security review of F12 scheduler subsystem and its integration with FastAPI lifespan + `/health`.

## Methodology

1. `ruff check` + `ruff format --check` + `mypy --strict src/` — all clean (44 source files)
2. `gitleaks detect` full-repo scan — no leaks
3. `semgrep p/owasp-top-ten` on `src/orchestrator/scheduler/` — 0 findings
4. Manual review against threat-model entries TM-005 (SQL injection),
   TM-014 (boot-time / scheduler resource pressure)
5. Test coverage review — 25 new tests cover the security-relevant
   edge cases (callback failure swallow, dedup correctness, settings
   bounds)

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-005 (SQL injection):** MITIGATED. `enqueue_library_sync` uses
  parameterized SQL exclusively (`?` placeholders or hard-coded
  literals). The dedup query is hard-coded with no user input. The
  insert is fully literal — no user-supplied data reaches SQL.

- **TM-014 (boot-time / scheduler resource pressure):** MITIGATED.
  - `max_instances=1` per scheduled job prevents pile-up if a fire
    arrives while the previous handler is still running.
  - `misfire_grace_time=None` + `coalesce=True` means after an outage,
    only ONE catch-up fire happens (not the missed N fires).
  - Cron callbacks NEVER raise — exceptions are logged + swallowed
    (verified by `test_returns_zero_on_pool_error_without_raising`),
    so a failing tick doesn't put the scheduler in a degraded state.
  - Default interval 6h × max_instances=1 caps activity at ~4
    enqueues/day per scheduled job.
  - Dedup at enqueue time prevents pile-up in the `jobs` table — if
    a queued/running library_sync exists, no new row is inserted.

## Defensive-programming review

- `SchedulerManager.start()` failures are caught at the lifespan layer
  (`api.boot.scheduler_start_failed` CRITICAL log) — boot continues
  with `scheduler_running=False`. JQ3 contract upheld: `/health`
  returns 503.
- `SchedulerManager.shutdown()` is idempotent and safe to call when
  the scheduler never started (the disabled-via-settings path) — no
  AttributeError on `self._scheduler is None`.
- Lifespan shuts down scheduler FIRST so it can't enqueue new work
  during teardown.
- Scheduler `enabled=False` is a true no-op: registers no jobs,
  reports `.running=False`, makes `start()` / `shutdown()` no-ops.
  Validated by `test_disabled_manager_registers_no_jobs`.

## Operator surfaces

- `scheduler.disabled_by_settings` (INFO) — emitted when `ORCH_SCHEDULER_ENABLED=false`
- `scheduler.started` (INFO) — emitted on successful boot, with interval + job count
- `scheduler.stopped` (INFO) — emitted on shutdown
- `scheduler.library_sync.queued` (INFO) — fires every successful enqueue
- `scheduler.library_sync.dedup_skip` (INFO) — fires when dedup hits
- `scheduler.library_sync.db_error` (ERROR) — DB failure swallowed
- `scheduler.library_sync.unexpected_error` (ERROR) — defensive catch
- `api.boot.scheduler_started` / `api.boot.scheduler_start_failed` —
  lifespan-level wrappers

No PII, no credentials, no job payloads in any log event.

## Sign-off

No SEV findings. F12 cleared to ship.

— Senior Security Engineer persona, 2026-05-28
