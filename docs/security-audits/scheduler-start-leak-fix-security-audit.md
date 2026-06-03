# Security Audit — Scheduler `start()` stale-instance leak fix (SEV-2)

**Date:** 2026-06-02
**Scope:** `src/orchestrator/scheduler/manager.py` (`SchedulerManager.start`, `shutdown`,
new `_dispose_stale_scheduler`, new `asyncio.Lock`); `tests/scheduler/test_manager.py`.
**Origin:** Backlog item from the 2026-06-02 full code review (see
`project_code_review_2026_06_02`). Persona: Senior Security Engineer — hunt concrete
exploits, not check boxes.

## Change summary

`start()` previously short-circuited only when the held scheduler was *running*; on the
non-running path it overwrote `self._scheduler`, abandoning the prior instance. The fix
disposes of any held-but-stopped scheduler (warning + best-effort `_dispose_stale_scheduler`)
before rebuilding, and serializes `start()`/`shutdown()` behind an `asyncio.Lock` so the
lifecycle stays atomic under any future concurrent caller.

## Threat review

| # | Vector | Assessment |
|---|--------|------------|
| 1 | **Deadlock / DoS via the new lock** | The lock is acquired only in `start()`/`shutdown()`, never nested, and never held across a blocking await. Inside `start()` the lock-held body is fully synchronous; inside `shutdown()` the only call is `AsyncIOScheduler.shutdown(wait=True)`, which APScheduler defers via `call_soon_threadsafe` and returns immediately (it does not block the loop). No path holds the lock indefinitely → no deadlock. **No reachable trigger** anyway: neither method is exposed to a request; the lifespan calls each once. |
| 2 | **Exception swallowing hides a security-relevant failure** | `_dispose_stale_scheduler` swallows exceptions from `stale.shutdown(wait=False)` (defensive `stale.running` branch only) and logs `scheduler.stale_dispose_failed` with `str(e)[:200]`. Scheduler-shutdown errors carry no secrets; the message is truncated. Net effect is *more* robust teardown, not less. |
| 3 | **Information disclosure via new log events** | New events `scheduler.replacing_stale_instance` (no fields) and `scheduler.stale_dispose_failed` (truncated error string). No credentials, tokens, PII, or job payloads are logged. |
| 4 | **Resource exhaustion** | The fix *removes* a resource leak (abandoned `AsyncIOScheduler` + executor + jobstore + loop timer). Disposal clears the reference (and stops a still-running instance), enabling GC. Net-positive posture. |
| 5 | **New race introduced** | Verified empirically that `await` of the (now removed) non-suspending dispose coroutine did not yield; the synchronous dispose + lock make the rebuild atomic. The change *closes* a TOCTOU window rather than opening one. |
| 6 | **New external surface** | None. No new inputs, endpoints, auth changes, SQL, filesystem, or network I/O. |

## Findings

**0 security findings.** The change is a stability/robustness fix with a net-positive
security posture: it eliminates a resource leak and closes a (currently unreachable)
concurrency window, adds no attack surface, and discloses no sensitive data.

## Out of scope (noted, not fixed here)

A theoretical APScheduler-internal race — a `wakeup()` callback firing after a deferred
`_shutdown()` set `_eventloop = None` — was raised during adversarial review. It is an
APScheduler shutdown-mechanics concern, **not introduced by this fix**, and unreachable
with this deployment's job intervals (21600s / 60s): `_shutdown()` cancels the timer
before it can fire. No mitigation added.
