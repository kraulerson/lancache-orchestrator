# UAT-11 — Integration / Chaos / Deployment-Shape Leg

**Date:** 2026-06-13
**Branch:** main
**Persona:** SRE — end-to-end flows, self-recovery, deployment robustness
**Scope:** jobs worker + handlers, self-recovery (job timeout, pool heal, reaper, scheduler lifecycle, lancache heartbeat, validator self-test), deployment shape (Dockerfile / settings / docs). A code-level defect audit was just completed; this leg targets SYSTEM-LEVEL and DEPLOYMENT concerns.

## Test baseline

```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/jobs/ tests/scheduler/ tests/db/ tests/lancache/ tests/validator/ -p no:randomly -q
365 passed, 3 deselected in 11.89s
```

All in-scope subsystem tests are green. The findings below are gaps the existing tests do not cover, not regressions.

---

## Summary by severity

| Severity | Count |
|---|---|
| SEV-2 | 0 |
| SEV-3 | 3 |
| SEV-4 | 3 |

No SEV-1/SEV-2. The end-to-end job flow, dedup, and reaper are sound. The three SEV-3s are self-recovery / deployment gaps where the system reaches a stuck or surprising state with no automatic recovery path, but in each case an operator-visible signal or a process restart resolves it.

---

## Findings

### F-INT-1 (SEV-3) — A job timeout leaves the game stuck in `downloading` forever (no game-status recovery path)

**Area:** self-recovery — jobs/worker.py `job_max_runtime_sec` × prefill/validate handlers × reaper

**Why it matters (system level):** The newly added per-job max-runtime backstop (`worker.py:161-162`) cancels a wedged handler via `asyncio.wait_for(handler(row, deps), timeout=...)`. On expiry, `wait_for` **cancels the handler coroutine**, raising `CancelledError` *inside* the handler. Both prefill handlers (`_steam_prefill`, `_epic_prefill`) and `validate_one_game` set `games.status='downloading'` first, then do their long work inside a `try/except Exception` guard whose stated purpose is "Never leave the game stuck in 'downloading'" (`prefill.py:99-109`, `216-225`). But `CancelledError` is a `BaseException`, **not** an `Exception` — so on a timeout cancellation the guard does NOT run, the re-raise (`raise`) does not fire that cleanup, and the game is left `downloading`. The worker, one level up, correctly marks the *job* `failed` (it catches `TimeoutError` outside the cancelled handler), so the two states diverge: job=`failed`, game=`downloading`.

There is **no recovery path** for a stuck `downloading` game. The startup reaper (`reaper.py`) reaps orphaned *jobs* only — nothing resets `games.status`. The only code that resolves a stuck `downloading` is `validate_one_game` (`validate.py:74-79`), which runs only when a *validate job executes for that game* — and a timed-out prefill never enqueued one (the validate enqueue happens after prefill success). So the game shows `downloading` indefinitely until an operator manually re-triggers prefill (and even then, a fresh prefill timing out again repeats the leak).

This is also reachable on a hard crash mid-prefill (container OOM/kill while `downloading`): the reaper fixes the job, the game stays `downloading`.

**Repro / evidence:**
```python
# Confirmed offline — CancelledError from wait_for bypasses `except Exception`:
async def handler():
    status = "downloading"
    try:
        await asyncio.Event().wait()      # the long prefill
    except Exception:                     # prefill handler's guard
        status = "failed"; print("cleanup ran")
        raise
async def wrapper(): await asyncio.wait_for(handler(), timeout=0.05)
# -> "cleanup ran" NEVER prints; worker sees TimeoutError, job->failed, game stuck.
```
`tests/jobs/test_worker.py::test_hung_handler_times_out_and_marks_failed` asserts the *job* goes `failed` but uses a trivial handler with no game-status side effect, so it does not catch this.

**Suggested direction:** Add a startup game-status reaper alongside the job reaper: on boot, `UPDATE games SET status='failed', last_error='orchestrator restarted/timed out mid-prefill' WHERE status='downloading'` (mirrors `reap_running_jobs`). The job reaper already establishes that any in-flight state at boot is orphaned; extend the same invariant to `games.status='downloading'`, which is only ever a transient in-flight state. Optionally also have the worker's timeout branch best-effort clear the game status, but the boot reaper is the robust fix (it also covers the crash case).

---

### F-INT-2 (SEV-3) — Writer connection has no self-heal after a replacement storm; only a process restart recovers (asymmetric with readers)

**Area:** self-recovery — db/pool.py writer replacement vs reader heal-on-acquire

**Why it matters (system level):** The reader path was deliberately hardened to self-heal: when a reader replacement gives up (open failure or `>3 replacements/60s` storm guard), the slot is recorded in `_lost_reader_slots` and a later `_acquire_reader` re-opens it once the fault clears (`pool.py:851-868`, `972-981`, `989-1002`). The **writer has no equivalent.** When a writer replacement hits the storm guard, `_replace_connection` returns early (`pool.py:972-981`) leaving `self._writer` pointing at the **old broken connection** and `_writer_healthy=False`. `_checkout_writer` (`pool.py:923-936`) never reads `_writer_healthy` and never attempts a heal — it just yields the dead `self._writer`, the write fails with a disk-I/O error, which spawns another replacement, which immediately re-trips the storm guard. The writer is wedged permanently; **every write (job status updates, library upserts, manifest writes, trigger enqueues) fails until the process is restarted.**

Mitigating factor that keeps this SEV-3 not SEV-2: `health_check` live-probes the actual writer connection (`pool.py:1271-1305`), so `/health` reports `writer.healthy=False` → 503, and the Docker `HEALTHCHECK` (every 30s) will mark the container unhealthy. So the failure is *visible* and an orchestrator-style restart policy would recover it. But "recover only via restart" is a weaker guarantee than the reader path's automatic heal, and a single transient writer-side disk hiccup that trips the storm guard converts into a permanent outage until something restarts the container.

**Repro / evidence:** Static analysis of `pool.py`. `grep _writer_healthy` shows it is written at lines 592/685/960/1014 but the only *read* is in `health_check` (1305) — never in the write checkout path. No `_lost_writer` counter exists.

**Suggested direction:** Give the writer a heal-on-checkout: in `_checkout_writer`, if `self._writer is None` or `not self._writer_healthy`, attempt one bounded re-open (under `_writer_lock`) before yielding, mirroring the reader's heal-on-acquire. Failing that, at minimum document explicitly (operations runbook + known-limitations) that a wedged writer requires a container restart and rely on the HEALTHCHECK + restart policy — and make sure the deployment actually sets `restart: unless-stopped`.

---

### F-INT-3 (SEV-3) — Dockerfile hardcodes `--host 0.0.0.0`, overriding the secure `api_host` default and tripping the non-loopback warning on every boot

**Area:** deployment shape — Dockerfile ENTRYPOINT vs settings/OQ2 posture

**Why it matters (system level):** `Settings.api_host` defaults to `127.0.0.1` and the whole security posture (boot warning `api.boot.non_loopback_bind_warning`, the `config.api_bound_non_loopback` config warning, OQ2 loopback enforcement) is built around loopback binding. But the Dockerfile ENTRYPOINT (`Dockerfile:66`) hardcodes `--host 0.0.0.0`, which uvicorn honours directly — `ORCH_API_HOST` is ignored entirely (uvicorn binds the socket, the app never reads `api_host` for binding). Consequences:

1. `_detect_non_loopback_bind` inspects argv for `--host`, so it sees `0.0.0.0` and **fires `api.boot.non_loopback_bind_warning` on every single container start** — a permanent false-alarm that trains operators to ignore the warning that is supposed to flag a genuine misconfiguration.
2. In the documented deployment (per project memory: "dockerized on the lancache host", host networking required for the DNS-bypass cache access), `--host 0.0.0.0` under `--network host` means the API binds every LAN interface of the lancache host. OQ2 (`dependencies.py:45-71`) only loopback-restricts credential-intake + schema endpoints; **all other endpoints (sync/prefill/manifest/validate triggers, games/jobs/manifests reads) are then reachable from the LAN protected by bearer token only.** That may be an acceptable design, but it is the opposite of the loopback-by-default posture the rest of the codebase advertises, and nothing documents the intended publish/network model.

**Repro / evidence:** `Dockerfile:66` `ENTRYPOINT ["uvicorn", ..., "--host", "0.0.0.0", ...]`. `main.py:66-85` `_detect_non_loopback_bind` matches `--host` in argv. `dependencies.py:61-71` `LOOPBACK_ONLY_PATTERNS` covers only auth + schema routes.

**Suggested direction:** Either (a) bind the container to `127.0.0.1` in the ENTRYPOINT and require operators to front it with an explicit published-port/proxy (cleanest given the loopback posture), or (b) keep `0.0.0.0` but make it an explicit, documented choice: drop `--host` from the ENTRYPOINT and let `ORCH_API_HOST` (or a `UVICORN_HOST` env) drive it via `uvicorn ... --host ${...}`, default `127.0.0.1`; ship the promised compose bundle (see F-INT-4) pinning the network/publish model; and suppress the boot warning when the bind is an intentional configured value rather than always-on. At minimum, document the host-network exposure and the bearer-only protection of non-OQ2 endpoints in the LAN-reachable deployment.

---

### F-INT-4 (SEV-4) — No compose bundle / `docker run` recipe despite Dockerfile + README promising one; README env table omits shipped settings

**Area:** deployment shape — documentation / deployability

**Why it matters:** A UAT cares whether the system is deployable *as documented*. The Dockerfile comment (`Dockerfile:53-54`) says the `VOLUME` exists so "we can safely add `--read-only` to the compose bundle later" — but no compose file exists anywhere in the repo (`find` for `compose*.y*ml` / `docker-compose*` returns nothing). README has no `docker run`/`docker compose` recipe at all: no statement of the required cache mount (`/data/cache/cache/` read-only), the persistent volume (`/var/lib/orchestrator`), the secret mount (`/run/secrets/orchestrator_token`), the network model (host-net for DNS bypass), or `--read-only`. So the documented happy-path deploy is reconstructed from memory/SSH history, not the repo — a new maintainer (Phase 4 handoff persona) could not stand it up from docs.

Separately, the README env-var table (`README.md:38-64`) is stale: it omits **`ORCH_JOB_MAX_RUNTIME_SEC`** (the 6h backstop — a key tunable for this leg), `ORCH_POOL_READER_ACQUIRE_TIMEOUT_SEC`, `ORCH_VALIDATION_SWEEP_ENABLED` / `ORCH_VALIDATION_SWEEP_CRON` / `ORCH_SWEEP_BATCH_SIZE`, the Epic `ORCH_EPIC_*` block, the `ORCH_LANCACHE_BASE_URL` / `ORCH_STEAM_CDN_HOST` / `ORCH_PREFILL_*` prefill knobs, and the steam-worker timeout settings. An operator tuning the timeout for a slow link can't discover the var from the docs.

**Repro / evidence:** `find` for compose files: none. `README.md` 136 lines, no docker run section; env table ends at `ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC`. `settings.py` defines ~20 more `ORCH_*` fields.

**Suggested direction:** Ship the promised `docker-compose.yml` (cache mount read-only, `/var/lib/orchestrator` named volume, secret mount, network model, `--read-only` + `tmpfs` as needed, `restart: unless-stopped`) and a matching `docker run` block in README; regenerate the env-var table from `settings.py` so it is exhaustive. This is partly a Phase-3/4 doc deliverable, flagged here because it directly blocks "deployable as documented."

---

### F-INT-5 (SEV-4) — `manifest_fetch` in-flight dedup relies on app-level SELECT-then-INSERT (no UNIQUE index), unlike every other job kind

**Area:** integration — job dedup consistency

**Why it matters:** library_sync (migration 0004), sweep (0005), and prefill+validate (0006) all have partial UNIQUE in-flight indexes and use `INSERT ... ON CONFLICT DO NOTHING`, making their dedup race-safe at the DB layer. **`manifest_fetch` is the lone exception:** `manifest_trigger.py:67-71` does a plain `SELECT ... state IN ('queued','running')` then a bare `INSERT` with no `ON CONFLICT` and no backing index. That SELECT-then-INSERT straddles an `await`, so two concurrent manifest-fetch triggers for the same game (operator double-click, or CLI racing the API) can both pass the SELECT and insert two `queued` rows. Both then run serially on the single steam worker — wasted work and a redundant manifest re-fetch, not a correctness corruption (the UPSERT on `(game_id, version)` converges). Low blast radius because manifest_fetch is usually triggered as a side-effect of prefill (which IS deduped), and the steam worker is serial — but it is an inconsistency with the hardened pattern the 0004/0005/0006 audits established for every other kind.

**Repro / evidence:** `grep manifest_fetch src/orchestrator/db/migrations/*.sql` shows only the kind-CHECK extension (0002), no in-flight index. `manifest_trigger.py:67-71` lacks `ON CONFLICT`.

**Suggested direction:** Add migration `0007_jobs_manifest_fetch_unique.sql` mirroring 0006 (cancel pre-existing dupes, `CREATE UNIQUE INDEX idx_jobs_manifest_fetch_inflight ON jobs(game_id) WHERE kind='manifest_fetch' AND state IN ('queued','running')`), and switch `manifest_trigger.py` to `INSERT ... ON CONFLICT DO NOTHING`. Closes the last gap in the "DB-enforced in-flight dedup for every job kind" invariant.

---

### F-INT-6 (SEV-4) — Timeout-cancelled steam handler leaves an in-flight IPC op running in the serial worker; next job's response can be delayed

**Area:** self-recovery — job timeout × serial steam worker IPC

**Why it matters:** The steam worker is a single serial subprocess (one stdin/stdout IPC channel). When `job_max_runtime_sec` cancels a handler that is blocked in `_await_response` (`client.py:250-258`), the `finally` pops the pending future, but the **worker subprocess is still executing the cancelled op** (e.g. a multi-minute `manifest.fetch`). The orchestrator does not signal the worker to abort. The next job's `_send` writes a new request onto the same stdin; the worker processes serially, so the new op queues behind the still-running cancelled one, and the cancelled op's eventual response arrives as an orphan (correctly dropped by `_on_response_line`, `client.py:304-307`). Net effect: after a timeout, the *next* steam job can stall for up to the remainder of the abandoned op before it even starts — a single timeout can cascade into a second apparent timeout. Not a corruption (orphan responses are handled, futures are cleaned), but the per-job budget does not actually free the worker, so the "self-heals without a process restart" claim is weaker than it reads for steam jobs specifically.

**Repro / evidence:** `client.py` IPC is single-channel serial; no abort/cancel op is sent on handler cancellation. `_await_response.finally` pops the future but cannot interrupt the subprocess.

**Suggested direction:** Document the limitation, or have the worker-loop timeout path recycle the steam worker subprocess (restart it) when it cancels a steam-bound handler, so the next job starts against a clean worker. Given the restart-storm guard already exists, a bounded "restart worker after a timeout-cancel" is feasible. Lower priority — only bites back-to-back steam jobs where the first times out.

---

## What works well (verified)

- **End-to-end job flow is sound.** `claim_next_job` claims atomically under `write_transaction()` with `state='queued'` guard (`worker.py:53-65`); the loop catches every handler exception so one bad job can't kill the dispatcher; `mark_succeeded`/`mark_failed` both guard on `state='running'` and retry transient pool errors (`worker.py:76-103`). No path silently double-runs a job.
- **In-flight dedup ON CONFLICT call sites verified at every trigger except manifest_fetch (F-INT-5).** prefill (`prefill_trigger.py:75-79` + handler `prefill.py:279-283`), validate (`validate_trigger.py:71`), library_sync (`sync.py:43`, `auth.py:158`, `epic_sync.py:40`, `epic_auth.py:76`), sweep (`scheduler/jobs.py:71-73`) all use `ON CONFLICT DO NOTHING` backed by the 0004/0005/0006 indexes.
- **Startup reaper** correctly marks all `running` jobs `failed` before the worker spawns (`main.py:118-131` ordering verified: reaper at step 2b, worker at step 4), with a defensive try/except so a failed reap can't abort boot.
- **Scheduler lifecycle** is robust: lock-serialized idempotent start/shutdown, stale-instance disposal (`manager.py:90-97`, `150-169`), `max_instances=1` + `coalesce` so a missed fire never bursts, callbacks that never raise (`jobs.py`), and shutdown-before-worker-stop ordering in lifespan.
- **Lancache heartbeat** requires the `X-LanCache-Processed-By` header (not just any 2xx) so a misconfigured DNS bypass pointing at a different 2xx service is correctly reported unreachable; concurrency-collapse + one-shot `invalidate` semantics are correct; never raises.
- **Validator self-test** detects the empty-AND-unmounted bind-mount case (`self_test.py:46-48`) — the exact deploy footgun where Docker auto-creates an empty target and the validator would otherwise report every game missing. A mounted-but-fresh cache (still a mountpoint) correctly passes.
- **Pool reader heal + teardown double-close fix** are correct: heal-on-acquire under `_heal_lock` bounds heals by the deficit (`pool.py:851-868`); teardown dedups by `id()` across `_reader_pool` + queue to avoid the aiosqlite double-close deadlock (`pool.py:781-809`); release-surplus and replacement-surplus paths avoid background-closing `_reader_pool` members.
- **`job_max_runtime_sec=21600` (6h) default is coherent** — well above the inner steam-worker IPC budgets (≤5min each), so it is a true outer backstop for a legitimately long prefill that streams a large game through lancache and resumes from cache on retry. `0` disables it.
