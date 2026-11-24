# Eviction Re-check (Feature B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When cache-catcher detects an uncommanded cache eviction, every owned game
is measured again, in a pass that starts at the eviction time and runs to
completion without waiting for Wednesday.

**Architecture:**
- **cache-catcher** appends one JSON line per eviction alert it actually sends. The
  file lives on a new shared volume, mounted read-write into cache-catcher and
  read-only into the agent.
- **The agent** serves the file at `GET /v1/cache/eviction-events?since=`.
- **The control plane** polls that endpoint (Karl's N1: nothing on the NAS calls the
  orchestrator). On a new event it restarts the sweep pass at the event's
  timestamp, marks the pass `forced`, and keeps a sweep queued until the pass
  completes.

**Tech Stack:** Python 3.12, FastAPI (agent and control plane), APScheduler 3.x,
SQLite migrations, pytest. cache-catcher is stdlib-only Python under
`tools/cache_catcher/`.

**Spec:** `docs/superpowers/specs/2026-11-20-new-not-cached-design.md` §3b, plus
"Deploy order, Feature B" in §5. Karl's rulings P2 and N1.

## Global Constraints

- **Events file.** `eviction-events.jsonl` holds one JSON object per line:
  `{"ts": "YYYY-MM-DDTHH:MM:SSZ", "deletes": int, "window_s": int, "actor": str}`.
  `ts` is UTC with second precision, the same format as `games.first_seen_at`.
- **When an event is written.** Only for alert kind `eviction`, and only when
  `alert()` actually sends (never when the cooldown suppresses it). `mode000` and
  commanded purges never write an event.
- **Paths.**
  - cache-catcher writes to `/events/eviction-events.jsonl`.
  - The agent reads `settings.eviction_events_path`, default
    `/cache-events/eviction-events.jsonl`.
  - The volume is `cache-catcher-events`, mounted read-only in the agent.
- **Settings.**
  - `eviction_poll_sec`: default 300. `0` disables the poller; otherwise at least 60.
  - The poller is registered only when an agent client is set,
    `validation_sweep_enabled` is true, and `eviction_poll_sec > 0`.
- **Exactly once.** The newest event `ts` acted on is stored in the DB, so a restart
  never re-triggers an event already acted on.
- **Secrets.** Nothing new is secret. Never log the SMTP or Kuma values from
  cache-catcher's `/log`.
- **Failure isolation.**
  - In cache-catcher, an error writing the events file must never stop the alert's
    email or Kuma push.
  - In the control plane, an agent error or a 404 from an older agent is logged and
    retried, never crashes, and never triggers anything.
- **Tests.** Run them with `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`.
- **Commits.** The index is the reviewed baseline: implementers never stage or
  commit, and there is one approval at the end (the same as Feature A, ruling 1).
- **Build Loop.** Start it with `scripts/process-checklist.sh --start-feature
  "eviction-recheck"` before Task 1.

**Corrections to the spec, found while planning (rulings):**
- **Spec §3b's chaining cannot work as written.** It says a partially finished
  sweep "enqueues the next sweep before it returns". But the job's own row is
  still `running` at that point, so `idx_jobs_sweep_inflight` drops the insert
  (`ON CONFLICT DO NOTHING`). Instead, the eviction poller does the chaining: on
  every poll, while `sweep_pass.forced = 1` and no sweep is queued or running, it
  enqueues one. That chains within at most `eviction_poll_sec` of a partial sweep
  ending, and any queued jobs run in the gap.
- **Where the newest acted-on timestamp lives.** Spec §3b asks for a one-row table
  for it. The plan stores it in the existing one-row `sweep_pass` table as
  `last_eviction_ts`, alongside the new `forced` flag. That is the same semantics
  with one migration and no new table.

- **Monitor 180 does not report the forced pass.** Spec §3b says the forced pass
  reports through sweep monitor 180. It cannot, because forced sweeps are enqueued
  with `source='trigger'` and `monitor_url_for` pushes 180 only for scheduler runs
  (that is how a dead scheduler stays visible). What exists instead:
  - Kuma 220 already alerts on the eviction itself.
  - The forced pass is visible through the `eviction_watch.*` and `sweep.*` logs and
    `sweep_pass.forced`.
  - Task 5 documents this.

  No new monitor is added.

## Review Focus

1. **The agent is the old image (no endpoint, so 404) or is unreachable during the
   control-plane poll.** Nothing is triggered, a log line is written, and the next
   poll retries. Test owner: Task 4.
2. **A malformed or partial last line in the events file**, for example while
   cache-catcher is writing it. The line is skipped, the other events are still
   returned, and the endpoint never returns 500. Test owner: Task 3.
3. **A restart replays the file.** Events with `ts` at or before
   `last_eviction_ts` are ignored, so a restart never restarts the pass a second
   time. Test owner: Task 4.
4. **The events volume is missing or read-only in cache-catcher.** The email and the
   Kuma DOWN push still happen, and the write failure is logged. Test owner: Task 2.
5. **A forced pass meets a scheduled Wednesday sweep.** Never two sweeps at once
   (the in-flight index). The poller does not enqueue while a sweep is queued or
   running, and the scheduled sweep advances the same forced pass. Test owner:
   Task 4.

---

All tasks are in `lancache_orchestrator`, on branch `feat/eviction-recheck`
(from main `bab0f88`).

### Task 1: Migration 0020 and the sweep_pass API

**Files:**
- Create: `src/orchestrator/db/migrations/0020_sweep_pass_forced.sql`
- Modify:
  - `src/orchestrator/db/migrations/CHECKSUMS`
  - `src/orchestrator/jobs/sweep_pass.py` (`SweepPass`, `read_pass`, `complete_pass`, plus the new functions below)
- Test: `tests/db/test_migration_0020_sweep_pass_forced.py`, `tests/jobs/test_sweep_pass_forced.py`

**Interfaces:**
- Produces:
  - `sweep_pass.forced INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0,1))` and
    `sweep_pass.last_eviction_ts TEXT` (nullable).
  - `SweepPass` gains `forced: bool` and `last_eviction_ts: str | None`.
  - `async def restart_pass(pool: Pool, *, started_at: str, eviction_ts: str) -> SweepPass`.
    In one UPDATE it does `pass_number = pass_number + 1`,
    `pass_started_at = started_at`, `forced = 1` and
    `last_eviction_ts = eviction_ts`, then returns the new state.
  - `complete_pass` also sets `forced = 0`, keeping its existing pass-number guard.

- [ ] **Step 1: Write the failing tests.**
  - The migration adds both columns. The existing row keeps `forced = 0` and
    `last_eviction_ts IS NULL`.
  - `restart_pass` increments the number, sets `pass_started_at` to the given ts,
    `forced = 1` and `last_eviction_ts`.
  - Games whose `last_measure_attempt_at` is before the new `pass_started_at`
    become sweep candidates again (use the sweep's `_CANDIDATE_SQL`).
  - `complete_pass` clears `forced`.
  - A `complete_pass` with a stale `current` (the pass was restarted meanwhile)
    does not clear `forced`.
  - The migration checksum is valid. Model this on the existing tests for migration
    0017.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the migration (`ALTER TABLE … ADD COLUMN` twice), the
  CHECKSUMS line, and the sweep_pass changes.
- [ ] **Step 4: Run** the new tests plus `tests/db/` and `tests/jobs/test_sweep*`.
  Expect PASS.

### Task 2: cache-catcher writes eviction events

**Files:**
- Modify: `tools/cache_catcher/fanotify_guard.py` (`alert()` at about :174, plus the
  new `EVENTS_PATH` and `_append_event`)
- Modify: `tools/cache_catcher/README.md` (the events file, the volume, and that
  writes are best-effort)
- Test: `tests/tools/test_fanotify_guard_events.py`

**Interfaces:**
- Produces:
  - `EVENTS_PATH = "/events/eviction-events.jsonl"`.
  - `def _append_event(deletes: int, window_s: int, actor: str) -> None` appends one
    line in the Global Constraints format. It never raises: any exception is logged
    via `log()` as `EVENT-WRITE FAILED (<type>)`.
  - `alert()` gains keyword-only `deletes: int | None = None` and
    `actor: str | None = None`. The eviction call site in `handle_delete` passes
    them.
  - `_append_event` runs only when `kind == "eviction"` and the alert was not
    cooldown-suppressed. It runs after `send_email` and `kuma.push`, so a write
    failure cannot pre-empt them.

- [ ] **Step 1: Write the failing tests.** Patch `EVENTS_PATH` to a tmp file, and
  patch `send_email`, `kuma.push` and `load_cfg` to fakes.
  - A sent eviction alert appends exactly one valid JSON line with `ts` matching
    `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$`.
  - A cooldown-suppressed eviction writes nothing.
  - A `mode000` alert writes nothing.
  - With an unwritable `EVENTS_PATH` (a directory, or a missing parent), the email
    and the Kuma DOWN push are still called and `EVENT-WRITE FAILED` is logged
    (Review Focus 4).
  - If `fanotify_guard` cannot import on macOS because of Linux-only calls at import
    time, report it rather than restructuring. Check `tests/tools/` for how sibling
    modules are imported.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new test file plus `tests/tools/`. Expect PASS.

### Task 3: Agent endpoint and client

**Files:**
- Create: `src/orchestrator/agent/routers/cache_events.py`, included in the agent
  app the same way the existing routers are.
- Modify:
  - `src/orchestrator/core/settings.py` (`eviction_events_path: str = "/cache-events/eviction-events.jsonl"`)
  - `src/orchestrator/clients/agent_client.py` (the new method below)
- Test: `tests/agent/test_cache_events.py`, plus a client test in `tests/clients/test_agent_client.py`

**Interfaces:**
- Produces:
  - `GET /v1/cache/eviction-events?since=<ts>` returns
    `{"events": [{"ts", "deletes", "window_s", "actor"}…]}`. It holds only events
    with `ts > since`, in file order. `since` is optional; when it is absent, every
    event is returned.
    - A missing file gives `{"events": []}`.
    - Malformed lines are skipped and logged.
    - The response holds at most 1,000 events (the newest).
    - The route sits behind the existing bearer and source-IP middleware.
  - `AgentClient.eviction_events(since: str | None) -> list[dict[str, Any]]`.

- [ ] **Step 1: Write the failing tests.**
  - The endpoint returns events after `since`.
  - A missing file gives an empty list.
  - A malformed line in the middle, and a partial last line, are both skipped, the
    valid lines are returned, and the status is 200 (Review Focus 2).
  - The cap keeps the newest 1,000.
  - The endpoint returns 401 without the bearer.
  - The client issues one GET with `since` and returns the list.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.** Do the file read with `asyncio.to_thread`, as the steam
  router does.
- [ ] **Step 4: Run** the new tests plus `tests/agent/` and
  `tests/clients/test_agent_client.py`. Expect PASS.

### Task 4: Control-plane eviction watch and pass chaining

**Files:**
- Create: `src/orchestrator/scheduler/eviction_watch.py`
- Modify:
  - `src/orchestrator/core/settings.py`: add `eviction_poll_sec: int = Field(300, ge=0, le=86400)`
    with a validator requiring 0 or at least 60, matching `steam_download_poll_sec`.
  - `src/orchestrator/scheduler/manager.py` and `src/orchestrator/api/main.py`:
    register an IntervalTrigger job, `EVICTION_WATCH_JOB_ID`, under the condition
    in Global Constraints.
- Test: `tests/scheduler/test_eviction_watch.py`, `tests/scheduler/test_manager.py`

**Interfaces:**
- Consumes:
  - `restart_pass` and `read_pass` (Task 1).
  - `AgentClient.eviction_events` (Task 3).
  - `enqueue_validation_sweep(pool, full=False, source=...)` (scheduler/jobs.py).
- Produces:
  - `class EvictionWatch`, with `__init__(self, pool: Pool, agent_client: AgentClient)`
    and `async def poll(self) -> str`. The return value is one of
    `"triggered"`, `"chained"`, `"idle"` or `"error"`.
  - `poll` runs in this order:
    1. `state = read_pass()`.
    2. Fetch `eviction_events(since=state.last_eviction_ts)`. On an exception, log
       `eviction_watch.agent_error` and return `"error"`.
    3. If new events exist, let `newest = max(ts)`, call
       `restart_pass(started_at=newest, eviction_ts=newest)`, enqueue a sweep with
       `source="trigger"`, log `eviction_watch.triggered` (with deletes, actor and
       ts), and return `"triggered"`.
    4. Otherwise, if `state.forced` is set and no `sweep` job is queued or running,
       enqueue a sweep with `source="trigger"`, log `eviction_watch.chained`, and
       return `"chained"`.
    5. Otherwise return `"idle"`.

- [ ] **Step 1: Write the failing tests.**
  - A new event restarts the pass at its ts, sets `forced` and `last_eviction_ts`,
    enqueues one sweep, and returns `triggered`.
  - Events at or before `last_eviction_ts` do nothing (Review Focus 3).
  - Two new events act once, at the newest ts.
  - An agent error or a 404 returns `error` and changes nothing (Review Focus 1).
  - `forced` with no sweep in flight returns `chained` and enqueues.
  - `forced` with a sweep queued or running returns `idle`, so there is no double
    sweep (Review Focus 5).
  - After `complete_pass`, `forced` is cleared and the result is `idle`.
  - The manager registers the job only under the conditions.
  - `eviction_poll_sec = 30` is rejected.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement**, and wire up main.py.
- [ ] **Step 4: Run** the new tests, `tests/scheduler/`, `tests/core/` and
  `tests/jobs/test_sweep*`. Expect PASS.

### Task 5: Docs, security audit, Build Loop close-out

**Files:**
- Modify:
  - `CHANGELOG.md`
  - `FEATURES.md` ("Feature 32: Eviction Re-check")
  - `PROJECT_BIBLE.md`: the sweep_pass columns, migration 0020, the agent endpoint,
    and the poller.
  - `docs/deploy/live-configuration.md`. Add:
    - the new volume;
    - both compose mounts, giving the exact YAML for the NAS compose file, which is
      not in the repo;
    - the setting;
    - the deploy order.
  - `tools/cache_catcher/README.md`, if Task 2 did not already cover it.
- Create: `docs/security-audits/eviction-recheck-security-audit.md`, in the same
  format as `new-not-cached-security-audit.md`.

- [ ] **Step 1: Write the docs.** The deploy order is:
  1. `docker volume create cache-catcher-events` on the NAS, then add it to the
     compose file's `volumes:` as `external: true`.
  2. Add `cache-catcher-events:/events` to cache-catcher and
     `cache-catcher-events:/cache-events:ro` to orchestrator-agent.
  3. Recreate the agent on the new image. Verify uid 0 and 256/256 buckets.
  4. Copy the cache-catcher code into the `/log` volume, then recreate
     cache-catcher. Verify Kuma 219 and 220 resume.
  5. Run the control-plane deploy (migration 0020).

  An events file appears only after the first eviction, so a missing file is
  normal.
- [ ] **Step 2: Run** the full suite, `ruff check src tests tools`,
  `ruff format --check`, and `mypy --strict src`. Expect everything clean, and
  paste the tallies.
