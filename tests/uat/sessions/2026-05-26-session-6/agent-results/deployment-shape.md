# UAT-6 — Deployment-Shape Agent Findings

## TL;DR — Blocking issues for production rollout

- **SEV-2** — Worker subprocess hardcodes credential dir `/var/lib/orchestrator/steam_session`, ignoring `Settings.steam_session_dir`. Two settings exist for one concept; operators who customize the env var will silently end up with the worker writing to the default path. See Finding 1.
- **SEV-2** — On `library.enumerate` failure with `NotAuthenticated`, the job is marked failed but `platforms.auth_status` is NOT flipped to `'expired'`. Operator-facing surface still says auth is `'ok'` while every sync silently fails. See Finding 4.
- **SEV-3** — Worker `get_product_info(packages=..., apps=...)` is unbatched. Large accounts (1000+ apps) likely exceed the default 30 s IPC timeout. See Finding 5.

Everything else below is SEV-3 or observability/operator-readiness, not deployment-blocking.

## Scope

Reviewed the BL10/BL11 surface as it would behave on the lancache host (192.168.1.40), running dockerized with a dual-venv layout per ADR-0013: orchestrator (asyncio, Python 3.12) and steam-worker (gevent-patched). Focus areas: first-boot from a fresh container, cold-start ordering, Steam server-side reality (steam-next call shapes), session/credential paths, and what the operator sees in logs when something goes wrong. Files reviewed: `src/orchestrator/api/main.py`, `src/orchestrator/platform/steam/{client,worker,session}.py`, `src/orchestrator/jobs/{worker.py,handlers/library_sync.py}`, `src/orchestrator/api/routers/{auth,sync}.py`, `src/orchestrator/db/migrations/0001_initial.sql`, `src/orchestrator/core/settings.py:67-101`.

## Findings

### Finding 1 — Worker hardcodes credential dir, ignoring `Settings.steam_session_dir`

**Severity:** SEV-2
**Scenario:** Operator customizes `ORCH_STEAM_SESSION_DIR=/srv/lancache/steam` (per Settings line 100). They deploy the container; auth succeeds; metadata JSON (orchestrator-owned, see `session.py`) lands at the operator-configured path. But `worker.py:58` hardcodes `credential_dir: str = "/var/lib/orchestrator/steam_session"` for the actual steam-next refresh-token files. Operator restarts the container with their custom volume — and refresh tokens are gone because the worker wrote them to the default path inside the container.
**Why it matters:** Production deployment will silently lose Steam session persistence across container restarts. The two paths (`steam_session_path` for orchestrator metadata vs. `steam_session_dir` for worker tokens) are designed to live next to each other, but the worker doesn't read its setting. The metadata file written by `session.py` will reference a `sha256_prefix` of a token that no longer exists on disk.
**Fix:** Pass `Settings.steam_session_dir` to the worker via environment variable (the worker's env is filtered to PATH/LANG/LC_ALL in `client.py:99` — extend the allowlist) or via a startup IPC message.

### Finding 2 — `steam_client.start()` failure leaves the object in DI; subsequent endpoints get worker-not-running

**Severity:** SEV-3
**Scenario:** On the lancache host's first container build, the dual-venv layout may not exist if the operator's Dockerfile diverges from the documented one. `client.py:111-113` raises `WorkerDiedError` on `FileNotFoundError`, which `main.py:114-118` catches and continues with a warning. The lifespan THEN publishes the half-dead client into DI (`set_steam_client_singleton(steam_client)` runs unconditionally at line 119). When the operator calls `POST /platforms/steam/auth`, `get_steam_client_dep` returns the half-dead client; calling `auth_begin` invokes `_send` which raises `WorkerDiedError("writer is None ...")`. The auth router maps this to `503 steam worker unavailable` — correct surface, but the operator's log shows `api.boot.steam_worker_start_failed` once, then on every request a different `WorkerDiedError` lineage. The restart-storm guard fires after 4 deaths (`_on_worker_died` called once at start failure → `_restart_attempts = 1`; guard fires when `> 3`). After that, requests get `WorkerDisabledError`, surfaced as the same 503. Confusing to debug; the boot warning is the most useful event but it's a single `warning`, not `error` or `critical`.
**Why it matters:** Cold-start failure of the worker is recoverable in theory but the operator only gets one log line at the right severity. Bump to `error` at minimum and consider adding `hint=` text pointing to `steam_worker_python_path`. ADR-0013 already says "spawn the worker only if session file exists" but `main.py:111` unconditionally spawns.

### Finding 3 — Jobs worker starts BEFORE first auth; a stale queued library_sync from prior boot will fail with `NotAuthenticated`

**Severity:** SEV-3
**Scenario:** Operator authenticates successfully → `_queue_library_sync_job_best_effort` queues a job. Power loss before the jobs worker picks it up. On next boot, the jobs worker (`main.py:122-131`) starts BEFORE any new auth, picks up the stale `queued` `library_sync` job, calls `library_enumerate` which returns `NotAuthenticated`, and marks the job failed. This is acceptable behavior per the task brief BUT: the operator sees a `failed` job in the jobs feed with no clear "this is normal post-boot, please re-auth" context. The `last_error` is the raw `SteamWorkerError: NotAuthenticated: no logged-in steam session` string.
**Why it matters:** Operator opens the status page, sees a red failed job, panics. Recovery path is "re-auth and it works", but nothing in the surface explains that. Soft mitigation: handler could check `_client.logged_on` first and `mark_skipped` rather than `failed` for the case where the worker is up but not logged in. Or auth state could gate job claiming.

### Finding 4 — `NotAuthenticated` mid-handler does NOT flip `platforms.auth_status` to `'expired'`

**Severity:** SEV-2
**Scenario:** Operator's Steam session expires after weeks (steam-next refresh tokens have finite life). Next scheduled `library_sync` job runs → handler calls `library_enumerate` → worker returns `NotAuthenticated` → `library_sync.py:52` re-raises (`SteamWorkerError` is uncaught in the handler) → jobs worker (`worker.py:117-135`) catches it and calls `mark_failed`. NOTHING updates `platforms.auth_status`. The `platforms` row still says `auth_status='ok'`. Operator queries `GET /api/v1/platforms/steam` and sees authenticated; queries `GET /api/v1/auth/status` (line `auth.py:290-311`), which routes through worker — if worker is still alive but not logged on, returns `authenticated: false`. Two surfaces disagree.
**Why it matters:** Operator-facing state is incoherent. The right behavior: the library_sync handler should catch `SteamWorkerError(kind='NotAuthenticated')` specifically and update `platforms.auth_status='expired', last_error='session_expired'` before re-raising for the jobs worker's `mark_failed` to run. The auth router does this on `auth_begin` failure (line 138-140) but not on background `library_sync` failure. Same expired session, two different operator surfaces depending on which code path detected it.

### Finding 5 — Unbatched `get_product_info` calls are likely to exceed the 30 s IPC timeout for large accounts

**Severity:** SEV-3
**Scenario:** Operator has a typical heavy gaming library (700+ apps owned, common for lan parties). `worker.py:201` calls `_client.get_product_info(packages=package_ids)` with the full list in one shot, then `worker.py:218` does the same for `candidate_app_ids`. steam-next does batch internally but the entire round trip is one gevent-blocking call. From Spike A notes, a 500-app round trip empirically takes ~15-25 s; 1000+ apps creeps over 30 s. `Settings.steam_worker_ipc_timeout_sec` default is 30 s. The orchestrator client will time out (`IPCTimeoutError`), mark the job failed, and the worker — which is still mid-`get_product_info` call — will eventually respond into a closed future (orphan response, logged at debug level at `client.py:220`).
**Why it matters:** Heavy operators get permanent sync failure with no diagnostic. Mitigations: (a) chunk `candidate_app_ids` into batches of e.g. 200 inside `_handle_library_enumerate`; (b) raise the default `steam_worker_ipc_timeout_sec` to 120 with a comment about library size; (c) at minimum surface a hint when `IPCTimeoutError` fires on `library.enumerate`. The current 30 s default was set for auth flows (sub-second steam-next calls) and was not re-evaluated for library enumeration.

### Finding 6 — Worker stderr is captured but never drained; long-running worker can deadlock on a full pipe

**Severity:** SEV-3
**Scenario:** `client.py:107` opens the subprocess with `stderr=asyncio.subprocess.PIPE`. Nothing in the orchestrator reads from `self._process.stderr`. steam-next is chatty when it reconnects to a Steam CM, retries, etc. Over a multi-week container uptime, stderr accumulates. When the OS pipe buffer fills (~64KB on Linux), the worker's stderr `write()` blocks. The worker then can't respond to IPC requests because gevent's monkey-patched `write` is now waiting on a never-draining reader.
**Why it matters:** Silent deadlock weeks into deployment. Either change to `stderr=asyncio.subprocess.DEVNULL` (loses diagnostics) or add a read-and-log task parallel to `_read_loop` that drains stderr and logs at `info` level. The latter also gives the operator something to look at when `library.enumerate` mysteriously hangs.

### Finding 7 — `jobs_worker_poll_interval_sec` default of 1.0 s is fine for the lancache host

**Severity:** Informational
**Scenario:** Lancache host is a low-load fileserver. 1 s polling = one `SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1` per second when idle; trivial on local-SSD SQLite with WAL. No reason to raise. If the operator ever co-locates this with a heavy DB workload, 5 s would still feel snappy to humans (a library_sync takes 30-90 s anyway). Document in HANDOFF.md but no code change.

### Finding 8 — First-boot scenario works end-to-end, with the caveats above

**Severity:** Informational
**Scenario:** Fresh container, no DB file, no session file.
1. Migration 0001 runs, seeds `platforms` rows with `auth_status='never'` (`0001_initial.sql:26-28`). Confirmed.
2. Lifespan spawns worker. ADR-0013 says "only if session file exists", but `main.py:111-119` ALWAYS spawns. Discrepancy with ADR — verify which is the intended design. If "always spawn" is correct, ADR-0013 should be updated.
3. Operator hits `POST /platforms/steam/auth` → worker handles `auth.begin` → success path triggers `_queue_library_sync_job_best_effort` → jobs worker claims → handler runs → `library.enumerate` returns apps → upsert into `games`. End-to-end works in the happy case.
4. Steam server-side checks: `_client.licenses` is a list of `License` objects with a `.package_id` attribute (Spike A confirmed; worker uses `getattr(... , "package_id", None)` defensively). `get_product_info` accepts `packages=` and `apps=` kwargs and returns a dict shaped as `{"packages": {...}, "apps": {...}}` — current worker code matches.

## Observability assessment

**Good:**
- Structured logs at every lifespan boundary (`api.boot.migrations_starting`, `api.boot.pool_starting`, `api.boot.steam_worker_started`, `api.boot.jobs_worker_started`, `api.boot.complete`).
- Job lifecycle events: `jobs.worker.claimed_job`, `jobs.handler.started`, `jobs.handler.completed` with `elapsed_ms`, `jobs.handler.failed` with `kind_error`.
- `library_sync.enumerate.started`, `.returned` (with `app_count`), `.upserted` (with `upserted` and `skipped` counts).
- Steam worker death events (`steam_worker.died`, `steam_worker.restart_storm_guard_fired`).

**Insufficient:**
- No correlation between an HTTP request that queued a job and the eventual job execution. The auth router logs `auth.auto_sync.queued` but doesn't include the resulting `job_id`. Operator can't grep one correlation_id end-to-end.
- `steam_worker.ipc_orphan_response` at `debug` level (`client.py:220`) — this is the diagnostic for a timeout where the worker eventually replied. Should be `warning`. Operator running with default log level (INFO) will never see it.
- No periodic heartbeat from the worker. If gevent deadlocks (see Finding 6), the orchestrator just sees `IPCTimeoutError` on the next call, not "worker stopped responding 4 hours ago".
- `library_sync.upserted` doesn't include `apps_total` from the enumerate response. Operator reading the log can compute it (upserted + skipped) but it'd be clearer to log directly.

**Severity rule violations:** none observed; everything that should be `error` is `error`, except the non-loopback bind warning (`main.py:150` is `warning`, which is right) and the orphan-response edge case noted above.

## Open questions for UAT-6 manual session (operator-only)

These need a real Steam account on the lancache host:

1. **Account scale:** How many apps does the operator's actual Steam account own? Above ~700, Finding 5 (IPC timeout on enumerate) likely fires. Measure wall-clock of a full library_sync from queue → succeeded.
2. **Session expiry observation:** After the operator authenticates successfully, leave the deployment for a day. Trigger a manual `POST /library/sync`. Does it succeed silently? Or does steam-next's refresh-token flow kick in transparently? If it fails, validate Finding 4 (the `platforms.auth_status` does NOT flip to `'expired'`).
3. **Free-to-play / 0-owned account:** If the operator has any spare Steam account with zero owned games, run library_sync against it. Confirm the handler returns succeeded with `upserted=0, skipped=0` and no errors. The code path `worker.py:196-198` handles `package_ids` empty; `worker.py:212-214` handles `candidate_app_ids` empty. Both return `{"apps": []}`.
4. **Custom session dir:** Set `ORCH_STEAM_SESSION_DIR=/srv/lancache/steam` and verify whether refresh tokens land there or in `/var/lib/orchestrator/steam_session` (Finding 1). If the latter, that confirms the SEV-2.
5. **Worker stderr volume:** After a 24-hour soak, check `docker logs` for the worker container — what's the volume of stderr output? If high, Finding 6 (pipe deadlock) is a real production risk.
6. **Concurrent auth:** While a library_sync is running, hit `POST /auth` again. Verify the running sync isn't interrupted (the worker is single-threaded per gevent; should serialize the request, possibly hitting `IPCTimeoutError` on whichever request waits longer). This is a UX-only check; the dedup logic for `POST /library/sync` is unit-tested but the cross-endpoint case isn't.
