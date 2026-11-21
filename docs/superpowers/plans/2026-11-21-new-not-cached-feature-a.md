# New - Not Cached (Feature A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A newly bought game shows "New - Not Cached" in Game_shelf until a check
measures it cached, and a Kuma monitor goes DOWN when a new game stays uncached for
7 days, or stays `unknown` for 48 h.

**Architecture:**
- **Orchestrator.** Library sync stamps `games.first_seen_at` once, when it inserts
  a game.
  - A new `new_games_check` job (cron every 6 h, plus a poller that watches
    SteamPrefill's downloaded record) syncs Steam, then re-measures the new games
    that aren't cached yet with the existing `validate_one_game`, then pushes two
    Kuma monitors.
  - The games API exposes `first_seen_at` and `is_new`.
- **Game_shelf.** It derives the badge, the filter value and the tile from
  `is_new`.

**Tech Stack:** Python 3.12, FastAPI, APScheduler 3.x, SQLite (STRICT tables,
versioned SQL migrations), pytest, click. Game_shelf: Node/Express 5,
better-sqlite3, React, Vite, vitest, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-20-new-not-cached-design.md` (commit
`7d99856`). Feature A covers §§1–3a, 4, 5 (monitors only) and the Game_shelf
section. Feature B (§3b, eviction re-check) is a separate plan.

## Global Constraints

- **Settings.**
  - `new_game_window_days` = 7; `new_game_unknown_alert_hours` = 48.
  - `new_games_check_cron` = `30 2,8,14,20 * * *` (UTC); `steam_download_poll_sec`
    = 600, where `0` disables the poller.
  - `new_games_check_enabled` defaults to `true`.
  - `kuma_push_new_games` and `kuma_push_new_games_overdue` are `str | None`,
    default `None`, read from env as `ORCH_KUMA_PUSH_NEW_GAMES` and
    `ORCH_KUMA_PUSH_NEW_GAMES_OVERDUE`.
- **Copy.** The Game_shelf label is exactly `New - Not Cached`.
- **Single-writer rule.** Only `orchestrator.jobs.measurement.record_measurement()`
  writes `games.status` and `status_measured_at`. `tests/test_measurement_writer_guard.py`
  must stay green.
- **Existing rows.** Rows that exist before migration 0019 keep `first_seen_at`
  NULL and are never new and never overdue.
- **Heartbeats.**
  - The liveness monitor is pushed only when `source == 'scheduler'`.
  - The overdue monitor is pushed when `source` is `scheduler` or `trigger`.
  - CLI and API runs push neither.
- **Exclusions.** Blocked games (`block_list`) and excluded games
  (`prefill_exclusions.mode='exclude'`) are never in the check set, never overdue,
  and never `is_new`.
- **Tests.** Run them as `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`.
- **Commits.** Every commit goes through the recorded-approval flow (pending-approval
  question, then a lone `git commit -m`). Stage files individually.
- **Build Loop.** Run `scripts/process-checklist.sh --start-feature "new-not-cached"`
  before Task 1, and mark its steps as the tasks complete them.

**Corrections to the spec, found while planning:**
- **Hand-run source.** Spec §3 says a manual run uses `source='cli'`. Every existing
  CLI trigger goes through the HTTP API with `source='api'` (for example
  `cli/commands/cache.py` → `POST /api/v1/sweep`). This plan uses the same pattern
  (`POST /api/v1/new-games-check`, `source='api'`). The heartbeat rules are
  unaffected.
- **API docs.** Spec §4 cites `docs/api and interfaces/`, which does not exist. The
  API fields are documented in `PROJECT_BIBLE.md`, which is where `status_measured_at`
  lives.

## Review Focus

1. **A clock or timezone mismatch in the window maths.** `first_seen_at` is stored
   as `strftime('%Y-%m-%dT%H:%M:%SZ','now')`. Both the window comparisons in SQL and
   `is_new` in Python must use UTC. A game first seen 6 d 23 h ago is new; one first
   seen 7 d 1 h ago is not. Test owner: Task 4 and Task 7.
2. **The agent is down or returns garbage while the T1 poller runs.**
   `downloaded_state()` raises, or returns `{}` after an agent restart. That must not
   enqueue a check, and must not reset the baseline so that the next good poll
   triggers on the whole library. Test owner: Task 6.
3. **Steam sync fails inside the check.** `_steam_library_sync` raises (the agent is
   unreachable). The check must still measure the existing new Steam games and all
   Epic ones, and log `new_games.steam_sync_failed`. A sync failure must not abort the
   job. Test owner: Task 5.
4. **A long overdue list.** 40 overdue titles must still produce a Kuma message within
   `_KUMA_MSG_MAX` = 400 characters, ending in `…(+N more)`. Test owner: Task 4.
5. **An old orchestrator behind Game_shelf.** A response without `is_new` must render
   today's labels, never "New - Not Cached" and never a crash. Test owner: Task 9.

---

## Part 1 — orchestrator (`lancache_orchestrator`, continue on `design/new-not-cached`)

That branch carries the spec (`7d99856`) and this plan, and sits directly on `main`
`8fe0883`; it has never been opened as a PR. Before Task 1, run `git fetch` and merge
any newer `main` into it.

### Task 1: Migration 0019 (first_seen_at, jobs kind/source/index)

**Files:**
- Create: `src/orchestrator/db/migrations/0019_new_games_check.sql`
- Modify: `src/orchestrator/db/migrations/CHECKSUMS` (append the 0019 line)
- Test: `tests/db/test_migration_0019_new_games_check.py`

**Interfaces:**
- Produces:
  - column `games.first_seen_at TEXT NULL`;
  - `jobs.kind` CHECK adds `'new_games_check'`;
  - `jobs.source` CHECK adds `'trigger'`;
  - index `idx_jobs_new_games_check_inflight ON jobs(kind) WHERE kind='new_games_check' AND state IN ('queued','running')`.

- [ ] **Step 1: Write the failing tests.** Model them on `tests/db/test_migration_0014_purge_kind.py`.
  - `test_first_seen_at_column_exists_and_existing_rows_are_null`: insert a game before migrating, then assert `first_seen_at IS NULL`.
  - `test_jobs_accepts_new_games_check_kind_and_trigger_source`: an INSERT with `kind='new_games_check', source='trigger'` succeeds.
  - `test_jobs_rejects_unknown_kind_still`: `kind='bogus'` raises `sqlite3.IntegrityError`.
  - `test_new_games_check_inflight_dedup`: a second queued `new_games_check` row hits `ON CONFLICT DO NOTHING`, and the rowcount is 0.
  - `test_existing_jobs_rows_and_all_prior_indexes_survive`: compare the `jobs` row count and `PRAGMA index_list(jobs)` before and after. Every 0014 index is still present.
- [ ] **Step 2: Run them.** Command: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/db/test_migration_0019_new_games_check.py -v`. Expect them to FAIL (the migration is missing).
- [ ] **Step 3: Write `0019_new_games_check.sql`.**
  - Start with `ALTER TABLE games ADD COLUMN first_seen_at TEXT;`.
  - Then copy the 0014 rebuild recipe verbatim (`PRAGMA foreign_keys=OFF`, then the backup, drop, recreate, restore, and all of the 0014 indexes), with the kind list extended by `'new_games_check'` and the source list by `'trigger'`.
  - Then add the new inflight index.
  - Append the CHECKSUMS line in the same format: id, sha256, filename.
- [ ] **Step 4: Run** the test file, then `tests/db/` as a whole. Expect PASS.
- [ ] **Step 5: Commit** `feat(db): migration 0019 — first_seen_at and new_games_check job kind`.

### Task 2: Stamp first_seen_at on insert only

**Files:**
- Modify: `src/orchestrator/jobs/handlers/library_sync.py:46-70` (`_NAMED_UPSERT_SQL`, `_UPSERT_SQL`)
- Test: `tests/jobs/test_library_sync_first_seen.py`

**Interfaces:**
- Consumes: Task 1's column.
- Produces: every row library sync inserts has `first_seen_at` = the insert time (UTC ISO `...Z`). The conflict path never changes it.

- [ ] **Step 1: Write the failing tests.**
  - `test_new_steam_row_gets_first_seen_at`.
  - `test_new_epic_row_gets_first_seen_at`.
  - `test_resync_does_not_change_first_seen_at`: sync, set the value to `'2026-01-01T00:00:00Z'`, sync again, and assert it is unchanged.
  - `test_existing_null_row_stays_null_on_resync`: the pre-0019 library must never become new.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Change both upserts.**
  - Add `first_seen_at` to each INSERT column list, with the value `strftime('%Y-%m-%dT%H:%M:%SZ','now')`.
  - Do not add it to `DO UPDATE SET`.
- [ ] **Step 4: Run** the test file and `tests/jobs/test_library_sync_handler.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(library-sync): record first_seen_at for newly seen games`.

### Task 3: Settings

**Files:**
- Modify: `src/orchestrator/core/settings.py`. Put the new fields beside `validation_sweep_cron` (:258) and the Kuma block (:290-295), and add a validator beside `_validate_fetch_manifests_cron` (:495).
- Test: `tests/core/test_settings_new_games.py`

**Interfaces:**
- Produces:
  - `Settings.new_game_window_days: int = Field(7, ge=1, le=60)`;
  - `new_game_unknown_alert_hours: int = Field(48, ge=6, le=24*60)`;
  - `new_games_check_enabled: bool = True`;
  - `new_games_check_cron: str = "30 2,8,14,20 * * *"`, validated by `_checked_cron`;
  - `steam_download_poll_sec: int = Field(600, ge=0, le=86400)`;
  - `kuma_push_new_games: str | None = None`;
  - `kuma_push_new_games_overdue: str | None = None`.

- [ ] **Step 1: Write the failing tests.**
  - Each default equals the Global Constraints value.
  - `ORCH_NEW_GAMES_CHECK_CRON="bad"` raises at load.
  - `ORCH_STEAM_DOWNLOAD_POLL_SEC=0` is accepted.
  - `ORCH_NEW_GAME_WINDOW_DAYS=0` is rejected.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Add the fields and the validator.**
- [ ] **Step 4: Run** the test file and `tests/core/`. Expect PASS.
- [ ] **Step 5: Commit** `feat(settings): new-games check window, cron, poller and monitors`.

### Task 4: The check-set and overdue-set queries

**Files:**
- Create: `src/orchestrator/jobs/new_games.py`
- Test: `tests/jobs/test_new_games_sets.py`

**Interfaces:**
- Consumes: Task 1 and Task 3.
- Produces:
  - `async def select_check_set(pool: Pool, *, window_days: int) -> list[int]`. It returns game ids, ordered by `first_seen_at`, per spec §3 "The check set".
  - `@dataclass(frozen=True) class OverdueGame: game_id: int; title: str; reason: Literal["not cached", "unmeasured"]`.
  - `async def select_overdue(pool: Pool, *, window_days: int, unknown_hours: int) -> list[OverdueGame]`. It follows spec §3 "The overdue set". A game matching both reasons is reported once, as `"not cached"`.
  - `def overdue_message(overdue: list[OverdueGame]) -> str`, with `_KUMA_MSG_MAX = 400`. It returns `"overdue K: Title (not cached), Title (unmeasured)…(+N more)"`, or `"no new games overdue"` when the list is empty.

- [ ] **Step 1: Write the failing tests.** Seed rows directly. The seed SQL may set `status` because it is test setup, not production code; the writer guard scans only `src/`.
  - `test_check_set_includes_unknown_not_downloaded_partial_within_window`.
  - `test_check_set_excludes_up_to_date_pending_update_outside_window_null_first_seen`.
  - `test_check_set_excludes_blocked_and_excluded`: a `prefill_exclusions` row with `mode='exclude'` excludes the game. Any other mode does not.
  - `test_check_set_excludes_unowned`.
  - `test_window_boundary`: first seen 6 d 23 h ago → in the set; 7 d 1 h ago → out (Review Focus 1).
  - `test_overdue_not_cached_after_window`: a `not_downloaded` game seen 8 d ago is overdue with reason `"not cached"`, and a `pending_update` game seen 8 d ago is not.
  - `test_overdue_unknown_after_48h`: an `unknown` game seen 49 h ago gives `"unmeasured"`, and one seen 47 h ago gives nothing.
  - `test_overdue_both_reasons_reported_once`.
  - `test_overdue_never_includes_null_first_seen_blocked_or_excluded`.
  - `test_overdue_message_truncates_to_400_with_more_count`: 40 titles give a message of at most 400 characters that ends `(+N more)` with the correct N (Review Focus 4).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement the three functions.** Do the window maths in SQL against `strftime('%Y-%m-%dT%H:%M:%SZ','now', '-<n> days')`, binding the numbers as parameters (`'-' || ? || ' days'`), so the comparison happens in UTC string order.
- [ ] **Step 4: Run them.** Expect PASS.
- [ ] **Step 5: Commit** `feat(jobs): new-game check and overdue selection`.

### Task 5: The `new_games_check` handler, heartbeats and registry

**Files:**
- Create: `src/orchestrator/jobs/handlers/new_games_check.py`
- Modify:
  - `src/orchestrator/jobs/handlers/__init__.py` (register under `"new_games_check"` in `_register_builtin_handlers`);
  - `src/orchestrator/jobs/worker.py:57-62` (add `"new_games_check": settings.kuma_push_new_games` to the `monitor_url_for` map).
- Test: `tests/jobs/test_new_games_check_handler.py`, plus one case in `tests/jobs/test_worker_heartbeats.py`.

**Interfaces:**
- Consumes:
  - `select_check_set`, `select_overdue` and `overdue_message` (Task 4);
  - `validate_one_game(pool, deps, game_id, settings)` (`jobs/handlers/validate.py:35`);
  - `_steam_library_sync(job, deps)` (`jobs/handlers/library_sync.py:84`);
  - `record_measurement(pool, game_id, "error")` and `CircuitBreakerTripped` (`jobs/measurement.py`);
  - `heartbeat.push(url, *, status, msg)` (`clients/heartbeat.py:32`);
  - `JobSummary(ok, msg)` (`jobs/summary.py`).
- Produces: `async def new_games_check_handler(job: dict[str, Any], deps: Deps) -> JobSummary`. Its msg is `"checked N new, C cached; overdue K"`, and it always returns `ok=True`.

- [ ] **Step 1: Write the failing tests.** Use a fake agent and a pool fixture, as `tests/jobs/test_sweep_size_gate.py` does.
  - `test_runs_steam_sync_before_reading_check_set`: the sync inserts a new row, and that row is measured in the same run.
  - `test_steam_sync_failure_is_logged_and_check_continues` (Review Focus 3).
  - `test_measures_each_game_in_check_set_once`.
  - `test_per_game_error_records_attempt_and_continues`.
  - `test_breaker_trip_fails_the_job`: it re-raises `CircuitBreakerTripped`.
  - `test_overdue_push_down_with_message_on_scheduler_and_trigger`: `heartbeat.push` is called with `kuma_push_new_games_overdue`, `status="down"` and the `overdue_message`.
  - `test_overdue_push_up_when_none_overdue`.
  - `test_no_overdue_push_for_api_source`.
  - `test_summary_message_counts`.
  - `test_new_epic_game_becomes_prefill_eligible`. This is the spec's intended
    consequence. A new Epic game with `status='unknown'` and
    `status_measured_at IS NULL` is measured `not_downloaded` by the check, and
    `enqueue_scheduled_prefill` then queues it. Use a fake validator returning
    `missing` (`scheduler/jobs.py:176-183`).
  - In `test_worker_heartbeats.py`, add `test_new_games_check_liveness_is_scheduler_only`: `monitor_url_for("new_games_check", "scheduler", s)` returns the liveness URL, and `"trigger"` and `"api"` return `None`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement the handler.** The handler runs in this order:
  1. `try: await _steam_library_sync(job, deps)`, then `except Exception: log new_games.steam_sync_failed`.
  2. `select_check_set`, then a sequential loop calling `validate_one_game`. Per-game exceptions are handled as in `sweep.py:281-320`. Let `CircuitBreakerTripped` propagate.
  3. `select_overdue`.
  4. If `job["source"] in ("scheduler","trigger")`, call `heartbeat.push(settings.kuma_push_new_games_overdue, status=…, msg=overdue_message(...))`.
  5. Log one `new_games.overdue` line per overdue game.
  6. Return the `JobSummary`.
- [ ] **Step 4: Run** both test files and `tests/test_measurement_writer_guard.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(jobs): new_games_check handler with liveness and overdue monitors`.

### Task 6: Scheduling: the cron, the T1 poller and the API trigger

**Files:**
- Modify:
  - `src/orchestrator/scheduler/jobs.py` (add `enqueue_new_games_check`, modelled on `enqueue_validation_sweep` at :65);
  - `src/orchestrator/scheduler/manager.py` (add constructor args, a cron job, and an interval job for the poller);
  - `src/orchestrator/api/main.py:233` (pass the new settings).
- Create:
  - `src/orchestrator/scheduler/steam_download_watch.py`;
  - `src/orchestrator/api/routers/new_games_trigger.py`, included next to `sweep_trigger` in `api/main.py`.
- Test: `tests/scheduler/test_new_games_scheduling.py`, `tests/scheduler/test_steam_download_watch.py`, `tests/api/test_new_games_trigger_router.py`.

**Interfaces:**
- Consumes: `AgentClient.downloaded_state() -> dict[str, list[int]]` (`clients/agent_client.py:374`).
- Produces:
  - `async def enqueue_new_games_check(pool: Pool, *, source: str = "scheduler") -> int`. It returns the rowcount, never raises, and deduplicates through Task 1's index.
  - `class SteamDownloadWatch:`
    - `__init__(self, pool: Pool, agent_client: AgentClient)`;
    - `async def poll(self) -> bool`. It returns True when it enqueued a check.
    - The baseline is held in memory as `frozenset[tuple[str, int]]`. On the first successful poll it sets the baseline and returns False.
    - When it sees added entries, it enqueues with `source="trigger"` and moves the baseline.
    - On an exception or an empty `{}` it logs and returns False, and leaves the baseline unchanged.
  - `POST /api/v1/new-games-check` returns 202 with `{"job_id": int | None, "queued": bool}`, calling `source="api"`.
  - `SchedulerManager.__init__` gains `new_games_check_enabled: bool = True`, `new_games_check_cron: str = "30 2,8,14,20 * * *"` and `steam_download_poll_sec: int = 600`.
  - It registers job ids `NEW_GAMES_CHECK_JOB_ID` (the cron) and `STEAM_DOWNLOAD_WATCH_JOB_ID` (`IntervalTrigger(seconds=steam_download_poll_sec)`). The watch job is registered only when the agent client is set and `steam_download_poll_sec > 0`.

- [ ] **Step 1: Write the failing tests.**
  - `test_enqueue_dedups`.
  - `test_manager_registers_cron_and_watch_jobs`.
  - `test_manager_skips_watch_when_poll_zero_or_no_agent`.
  - `test_watch_first_poll_sets_baseline_without_enqueue`.
  - `test_watch_added_entry_enqueues_trigger`.
  - `test_watch_unchanged_or_removed_only_does_not_enqueue`.
  - `test_watch_agent_error_or_empty_keeps_baseline`: after an error, then an empty response, then the original set, nothing is enqueued (Review Focus 2).
  - `test_trigger_endpoint_queues_with_api_source_and_requires_bearer`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** to the interfaces above, and wire `api/main.py`.
- [ ] **Step 4: Run** the three test files and `tests/scheduler/test_manager.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(scheduler): new_games_check cron, SteamPrefill download watch, API trigger`.

### Task 7: API fields `first_seen_at` and `is_new`

**Files:**
- Modify: `src/orchestrator/api/routers/games.py`:
  - `_GAMES_COLUMNS` adds `first_seen_at`;
  - `_GAME_ROW_SELECT` (:92) adds `EXISTS(SELECT 1 FROM prefill_exclusions e WHERE e.platform=games.platform AND e.app_id=games.app_id AND e.mode='exclude') AS excluded`;
  - `GameResponse` (:111) gains the fields;
  - `_row_to_game_response` (:177) computes them.
- Test: `tests/api/test_games_is_new.py`

**Interfaces:**
- Produces:
  - `GameResponse.first_seen_at: str | None`.
  - `GameResponse.is_new: bool`. It is True iff `first_seen_at` is not None, `now_utc - parse(first_seen_at) < timedelta(days=settings.new_game_window_days)`, `not blocked` and `not excluded`.
  - `excluded` is NOT added to the response, because `extra="forbid"` keeps the surface explicit.

- [ ] **Step 1: Write the failing tests.**
  - `test_is_new_true_within_window`.
  - `test_is_new_false_at_7d_plus`.
  - `test_is_new_false_when_first_seen_null`.
  - `test_is_new_false_when_blocked_or_excluded`.
  - `test_first_seen_at_in_list_and_detail` (list and detail share `_GAME_ROW_SELECT`).
  - `test_malformed_first_seen_at_renders_is_new_false_not_500`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the test file and `tests/api/test_games_router.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(api): expose first_seen_at and is_new on games`.

### Task 8: CLI, documentation, Build Loop close-out

**Files:**
- Modify:
  - `src/orchestrator/cli/commands/cache.py` (a new `cache check-new` command → `POST /api/v1/new-games-check`);
  - `CHANGELOG.md` (`[Unreleased]`: Added, Data Model, Infrastructure, and Changed for "new Epic purchases now download within hours");
  - `FEATURES.md` (a new feature entry);
  - `PROJECT_BIBLE.md` (the API fields, the job kind, migration 0019);
  - `docs/deploy/live-configuration.md` (the settings, the two monitors, the env names);
  - the follow-up issue for the S1 gap (`gh issue create`, body via `--body-file`).
- Create: `docs/ADR documentation/0020-first-seen-defines-new.md`, `docs/ADR documentation/0021-overdue-alert-never-times-out.md`.
- Test: `tests/cli/test_cache_check_new.py`

- [ ] **Step 1: Write the failing test.** `test_check_new_posts_and_reports_queued_or_inflight`. Before writing any CLI help text, confirm the existing `cache` group with `orchestrator-cli cache --help`.
- [ ] **Step 2: Run it.** Expect FAIL.
- [ ] **Step 3: Implement the command, then write the documents.** Use the ADR template already used by `0019-steam-manifest-helper.md`.
- [ ] **Step 4: Run** the full suite. Expect every test to pass, plus the new ones. Run `ruff check`, `ruff format --check` and `mypy src`, all clean.
- [ ] **Step 5: Run the closing checks.**
  - Run `/self-review-diff`, then one adversarial review (opus, max effort, a fresh agent).
  - Fix the reachable findings.
  - Commit, push, and open the PR (body via `--body-file`, "Refs" the follow-up issue).

## Part 2 — Game_shelf (`Game_shelf`, branch `feat/new-not-cached` from fresh `master`)

**Before Task 9:** the working copy is on `feat/cache-measured-age` (`47c980d`).
Confirm with `git log master..feat/cache-measured-age` whether that branch is
merged. If it is not, stop and ask Karl. Never branch from an unmerged PR.

### Task 9: Badge, per-store line and tiles

**Files:**
- Modify:
  - `frontend/src/utils/cacheBadge.js` (`cacheBadgeFor`, `cacheCounts`);
  - `frontend/src/utils/perStoreStatus.js` (pass `isNew`; add `Sparkles` to `LANCACHE_ICON_KIND` and `STORE_SYMBOLS`);
  - `frontend/src/components/GameCard.jsx:110-120` (pass `is_new`);
  - `frontend/src/components/cache/CacheBadge.jsx` (map the `Sparkles` lucide icon, and add `sky: 'bg-sky-600 text-sky-50'` to `TONE`);
  - `frontend/src/components/cache/CacheStats.jsx` (a "New - Not Cached" tile);
  - `frontend/src/pages/Library.jsx` (pass `is_new` wherever it passes `not_cached` or status today).
- Test: `frontend/src/utils/cacheBadge.test.js`, `frontend/src/utils/perStoreStatus.test.js`

**Interfaces:**
- Produces:
  - `export function isNewNotCached({ status, blocked, isNew }) -> boolean`, true iff `isNew === true && !blocked && (status === 'unknown' || status === 'not_downloaded')`. It is the one badge rule, and Task 10 mirrors it.
  - `cacheBadgeFor({..., isNew})` returns `{ icon: 'Sparkles', tone: 'sky', label: 'New - Not Cached' }` when that rule holds. Precedence is offline, then not-tracked, then blocked, then the new rule, then status.
  - `cacheCounts` gains a `new_not_cached` bucket. Those games are not counted in `not_cached` and never in an unknown tally.

- [ ] **Step 1: Write the failing tests.**
  - `isNew unknown → New - Not Cached`.
  - `isNew not_downloaded → New - Not Cached`.
  - `isNew validation_failed → Partly cached · N%`.
  - `isNew up_to_date → Cached`.
  - `isNew blocked → Blocked`.
  - `isNew undefined → today's label` (Review Focus 5).
  - `cacheCounts puts new games only in new_not_cached`.
  - The per-store line shows the Sparkles kind.
- [ ] **Step 2: Run them.** Command: `cd frontend && npx vitest run src/utils`. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd frontend && npx vitest run`. Expect PASS.
- [ ] **Step 5: Commit** `feat(cache): New - Not Cached badge and tile`.

### Task 10: The filter follows the badge

**Files:**
- Modify:
  - `backend/src/services/cacheSnapshot.js:26` (store `'new'` under the Task 9 rule);
  - `frontend/src/components/FilterPanel.jsx:10-17` (add `{ key: 'new', label: 'New - Not Cached' }`);
  - `backend/src/routes/games.js` (the `cache_status` filter accepts `new`).
- Test: `backend/tests/services/cacheSnapshot.test.js`, `backend/tests/routes/` (the cache_status filter test file beside it), `frontend/src/components/FilterPanel.test.jsx`, and a new `backend/tests/services/newRuleAgreement.test.js`.

**Interfaces:**
- Consumes: the Task 9 rule.
- Produces: the snapshot value is `g.blocked ? 'blocked' : (isNewNotCached({status: g.status, blocked: false, isNew: g.is_new === true}) ? 'new' : g.status)`. The backend gets a CommonJS twin of `isNewNotCached` in `backend/src/services/cacheRules.js`.

- [ ] **Step 1: Write the failing tests.**
  - The snapshot maps a new unknown or new not_downloaded game to `'new'`, and a new partial game to `'validation_failed'`.
  - The filter `cache_status=new` returns exactly the new games, and `cache_status=not_downloaded,unknown` excludes them.
  - The FilterPanel toggle sets `cache_status=new`.
  - **Agreement test:** a table of 12 (status, blocked, isNew) combinations is run through both the frontend rule and the backend twin, and the results are identical. This is the 2026-09-10 lesson.
- [ ] **Step 2: Run them.** Commands: `cd backend && npm test`, and `cd frontend && npx vitest run src/components/FilterPanel.test.jsx`. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both suites in full. Expect PASS.
- [ ] **Step 5: Close out.** Run `/self-review-diff`, then commit `feat(cache): New - Not Cached filter follows the badge`, push, and open the PR, which notes that it needs orchestrator PR N deployed first.

## Deploy (after both PRs merge; each step needs a change card)

1. **Create the Kuma monitors.** Create the two push monitors (spec §5) in the group
   with 179 and 180, notification 3, ~7 h interval. Put their URLs into
   `/root/orch-lxc.env` as `ORCH_KUMA_PUSH_NEW_GAMES` and
   `ORCH_KUMA_PUSH_NEW_GAMES_OVERDUE`. Never print the URLs.
2. **Recreate the control plane** in an empty-queue gap, not on a Wednesday sweep:
   tag `dpa-pre-<PR>`, build, then `deploy-orchestrator-lxc.sh` plus `qjobs.py`.
   - Verify that migration 0019 applied.
   - Verify that `first_seen_at` is NULL on every existing row (count = total).
   - Verify that health is ok and both scheduler jobs are listed.
3. **Recreate Game_shelf.** Verify that one new game shows "New - Not Cached" and the
   filter returns it.
4. **Prove it live.** After the next SteamPrefill run that downloads something, the
   poller enqueues `source='trigger'` within 10 min. The badge turns Cached, and
   monitor `orch:new-games-overdue` is UP.
