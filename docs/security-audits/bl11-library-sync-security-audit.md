# Security Audit — BL11 Steam Library Sync

**Feature:** BL11-library-sync
**Audit date:** 2026-05-25
**Audited modules:**
- src/orchestrator/jobs/__init__.py
- src/orchestrator/jobs/worker.py
- src/orchestrator/jobs/handlers/__init__.py
- src/orchestrator/jobs/handlers/library_sync.py
- src/orchestrator/api/routers/sync.py
- src/orchestrator/api/routers/auth.py (auto-trigger helper)
- src/orchestrator/api/main.py (lifespan jobs-worker integration)
- src/orchestrator/platform/steam/worker.py (`library.enumerate` handler)
- src/orchestrator/platform/steam/client.py (`library_enumerate` method)

<!-- Last Updated: 2026-05-25 -->

## Scope

Post-implementation security review of the BL11 surface — generic asyncio
jobs dispatcher, library_sync handler, manual sync endpoint, auth-success
auto-trigger, and the steam worker's `library.enumerate` IPC op.

## Methodology

1. **ruff / ruff format / mypy --strict** — all clean (37 source files).
2. **semgrep p/owasp-top-ten** on `src/orchestrator/jobs/` + `src/orchestrator/api/routers/sync.py` — 0 findings.
3. **gitleaks** full-repo scan — no leaks (151 commits scanned).
4. **Manual review** against threat-model entries TM-001 (auth bypass),
   TM-005 (SQL injection), TM-009 (resource exhaustion), TM-012
   (log/credential leak).
5. **Test coverage review** — ~39 new tests across 5 files; assertions
   target the security-relevant edge cases (auth boundary, concurrent
   dedup, error propagation without partial writes).

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-001 (auth bypass):** MITIGATED. `POST /api/v1/platforms/steam/library/sync`
  is mounted on the existing FastAPI app — it inherits
  `BearerAuthMiddleware` and is NOT in `LOOPBACK_ONLY_PATTERNS` (matches
  spec §5.6: status-style endpoint callable from Game_shelf, bearer
  required). Verified by `test_missing_bearer_returns_401` +
  `test_wrong_bearer_returns_401`.

- **TM-005 (SQL injection):** MITIGATED. All BL11 SQL is parameterized:
  - `claim_next_job`: 2 statements, both with `?` placeholders.
  - `mark_succeeded`/`mark_failed`: `?` placeholders.
  - `library_sync_handler` UPSERT: `?` placeholders; `app_id` coerced to
    `str(int)` before binding, never interpolated.
  - `trigger_library_sync`: hard-coded `'library_sync'`, `'steam'` constants
    plus parameterized INSERT.
  - `_queue_library_sync_job_best_effort`: same pattern.
  No format strings or f-strings touch SQL.

- **TM-009 (resource exhaustion):** PARTIALLY MITIGATED (acceptable for F1).
  Library enumeration is bounded by Steam's product_info pagination at
  the steam-next layer; the orchestrator does not impose its own ceiling.
  A 100k-app library would still fit comfortably under the existing
  10 MiB IPC line cap (BL10 D20). Jobs worker is a single-loop dispatcher
  — `worker_loop` cannot be flooded into multiple parallel handlers. The
  manual sync endpoint deduplicates queued/running jobs, so a request
  storm produces at most ONE extra `queued` row per race window
  (handler is idempotent, so any extra job no-ops at UPSERT). No new
  attack surface.

- **TM-012 (credential / log leak):** MITIGATED. No credentials traverse
  the new code paths:
  - Jobs worker logs `job_id`, `kind`, `elapsed_ms`, `kind_error` (exception
    class name) — never row contents or steam_client state.
  - library_sync_handler logs `app_count`, `upserted`, `skipped` — all
    derived counts; per-app `raw` truncated to 200 chars in the
    skip-reason path (public catalog data, not credentials).
  - sync.py logs `job_id` only.
  - auth.py auto-trigger logs `existing_job_id` or nothing.
  Steam-side `library.enumerate` returns app metadata only (app_id,
  name, depot ids) — all public Steam catalog data. No token round-trip,
  no session blob exposure.

## Concurrency model review

- `claim_next_job` uses SELECT-then-UPDATE inside `pool.write_transaction()`.
  The pool's `write_transaction` opens with `BEGIN IMMEDIATE`, which
  serializes writers on the same DB. The `UPDATE ... WHERE id=? AND
  state='queued'` clause is a defensive double-check — a second
  concurrent claim that somehow gets past the SELECT (impossible under
  BEGIN IMMEDIATE; defense-in-depth) will UPDATE zero rows and the
  re-read returns the row in `running` state. Verified by
  `test_atomic_under_concurrency` (4 parallel claims → 4 distinct ids).

- `worker_loop` exception handling: every handler exception is caught and
  routed through `mark_failed`. If `mark_failed` itself raises, the
  loop logs `jobs.handler.mark_failed_failed` and continues — a single
  poison-pill job cannot kill the dispatcher. Verified by
  `test_handler_crash_does_not_kill_loop`.

## Lifespan integration

- Jobs worker is spawned AFTER pool + steam-client are initialized and
  is stopped FIRST in the shutdown sequence (5 s graceful, then cancel).
  This ordering ensures the worker isn't still holding refs to the pool
  / steam-client at the moment those resources unwind.

- A long-running handler is bounded by `steam_worker_ipc_timeout_sec`
  (30s default) on the steam-side calls. Pool writes are bounded by
  the pool's busy_timeout. Worst-case shutdown delay is the 5 s join
  timeout plus one in-flight handler's outstanding IPC — acceptable.

## Sign-off

No SEV-1/2/3 findings. Two SEV-4 deferred items already documented in
the spec / FEATURES.md (live Steam validation in UAT-6; concurrent
multi-job dispatch deferred per D10). BL11 is cleared to ship.

— Senior Security Engineer persona, 2026-05-25
