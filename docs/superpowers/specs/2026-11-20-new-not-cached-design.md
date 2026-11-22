# "New - Not Cached" for new purchases, plus a re-check after cache eviction

Date: 2026-11-20. Status: draft, awaiting Karl's approval. Spans two repos:
`lancache_orchestrator` (this repo) and `Game_shelf`.

## Why

A game Karl has just bought shows **Unknown** in Game_shelf until the weekly sweep
(Wednesdays only) reaches it, which can take up to a week, even after SteamPrefill
has downloaded it. Live case 2026-11-20: Graveyard Orbit and Sol One were both
downloaded on 11-19 yet showed Unknown; one-off validates measured both
`up_to_date` in 17 s and 65 s.

Steam prefill runs from a host cron on the NAS, outside the orchestrator, so the
orchestrator never measures a Steam game straight after it downloads.

A Steam row only enters `games` once SteamPrefill has written a manifest.
`_steam_library_sync` enumerates `agent_client.prefilled_apps()`
(`jobs/handlers/library_sync.py:101`), which is the only Steam insert path. So the
orchestrator sees a Steam purchase only after its download has started.

Epic is worse than "slow to show". Nothing measures a new Epic game except the
weekly sweep, and scheduled prefill only downloads measured games
(`status_measured_at IS NOT NULL`, `scheduler/jobs.py:181`). So a new Epic
purchase waits up to a week to be **downloaded**, not just to be shown. #388 only
changed what a validation returns for a manifestless game.

## What Karl decided

- **Label.** A recently bought game that is not cached shows **"New - Not Cached"**
  in Game_shelf, not "Unknown".
- **Window.** A game counts as new for **7 days**.
- **Cadence.** A check runs **every 6 hours** and measures only new games.
- **Alert.** A new game still not cached after 7 days almost always means its
  automatic download failed, so it **raises an Uptime Kuma alert**. It does not
  quietly become "Not cached".

**Rulings of 2026-11-21:**
- **T1.** A SteamPrefill run finishing triggers an immediate new-games check. The
  6-hourly cron stays as a backstop.
- **S1.** A Steam purchase SteamPrefill never starts is a recorded, accepted gap.
- **U1.** A game still `unknown` 48 h after it was first seen also counts as
  overdue.
- **P2.** A cache-eviction alarm triggers a full re-check of the library, built in
  this feature.
- **N1.** NAS events reach the control plane by the control plane polling the
  agent. Nothing on the NAS calls the orchestrator. The bearer token stays where
  it is today, and the NAS host gets no new credential and is not added to
  `ORCH_ALLOWED_SOURCE_IPS`.

**Delivery (my call).** One design, two Build Loop features, each with its own PR
and deploy:
- **Feature A, "New - Not Cached":** §§1–3a, 4 and 5 (monitors), plus Game_shelf.
- **Feature B, "Eviction re-check":** §3b, plus the NAS compose change.

That uses the 2 features the test gate allows before the next UAT session.

## Goals

1. **Fast badge after a download.** Within about 10 minutes of SteamPrefill
   finishing a new purchase (T1 poll, plus the check run), Game_shelf shows it as
   **Cached** (or "Partly cached · N%"). The 6-hourly cron bounds the worst case
   for a missed trigger, and covers Epic.
2. **No "Unknown" for an orchestrator-known purchase.** Until then, a new game the
   orchestrator knows about shows "New - Not Cached", never "Unknown". Apart from
   that, the S1 gap below applies.
3. **The overdue alert.** A Kuma monitor turns DOWN, naming the game, for any new
   purchase that is still not cached after 7 days, or still `unknown` 48 h after
   it was first seen.
4. **Re-check after a loss.** After cache-catcher detects an uncommanded eviction,
   every owned game is measured again, in a pass that starts at the eviction time
   and runs to completion without waiting for Wednesday.

**Intended consequence.** Measuring new Epic games every 6 hours makes them
eligible for the next scheduled Epic prefill within hours, not up to a week. That
is intended: Karl's 2026-11-12 complaint was that purchases should be cached
within about 12 hours. It gets its own test and its own CHANGELOG line.

**Accepted gap (S1).** A Steam purchase that SteamPrefill never starts has no
orchestrator row. So it never shows "New - Not Cached", never alerts, and shows
"Unknown" in Game_shelf. The most likely cause, SteamPrefill not running at all as
on 2026-11-10, already turns Kuma 176 DOWN. Closing the gap needs Game_shelf as an
ownership source (ADR-0016 and ADR-0018, both still Proposed). That is filed as a
follow-up issue, not built here.

## Non-goals

- Using store purchase dates. "New" is measured from the moment the orchestrator
  first sees the game.
- Changing the download code. Downloads stay with SteamPrefill and the existing
  Epic scheduled prefill. Only the Epic timing changes, as a consequence of
  measuring sooner.
- Re-checking the existing library more often in normal running. The weekly sweep
  keeps that job. The only exception is the forced pass after an eviction (§3b).

## Anti-goals (failure even if it "works")

- **A wrong label that sticks.** A new game measured before its download must not
  drop out of the 6-hourly set. That is the failure a plain "re-check Unknown
  games" filter would create.
- **The whole library turning "New" on deploy.** About 2,600 existing rows must
  not suddenly show "New - Not Cached".
- **A permanently red monitor that people learn to ignore.** For example, the 4
  Epic games excluded as "covered on a higher-priority launcher" must never count
  as overdue.
- **A silent failure.** A new game that never caches must be visible
  (CLAUDE.md: no silent fallbacks).
- **A badge that disagrees with the filter.** Karl ruled on 2026-09-10 that the
  Game_shelf filter follows the displayed badge. A game showing "New - Not Cached"
  must match a "New - Not Cached" filter and must not match "Not cached" or
  "Unknown".

## Design — orchestrator

### 1. Record when a game is first seen (migration 0019)

- Add `games.first_seen_at TEXT NULL` (ISO-8601 UTC, the same format as the other
  timestamps).
- Existing rows keep **NULL**, so they are never new. This meets the deploy
  anti-goal, and the migration needs no backfill.
- Both library-sync upserts set it only when a row is inserted. Today these are
  `_NAMED_UPSERT_SQL` and `_UPSERT_SQL` in `jobs/handlers/library_sync.py`, both
  `INSERT … ON CONFLICT(platform, app_id) DO UPDATE`.
  - Add `first_seen_at` to the INSERT's column list.
  - Leave it out of `DO UPDATE SET`, so the conflict path never rewrites it.
- **My call:** a game that was un-owned and then owned again is not new, because
  its row already exists.
- **My call:** other insert paths (manual coverage, tests) leave the field NULL.
  Only library sync marks a game new.
- **The same migration also rebuilds `jobs`.** `jobs.kind` has a CHECK list
  (`0014_jobs_kind_purge.sql:34-36`) that would reject the new kind, and each kind
  has its own in-flight dedup index (`idx_jobs_<kind>_inflight`, `:67-94`). So
  migration 0019 rebuilds `jobs` with `'new_games_check'` added to the CHECK, and
  adds `idx_jobs_new_games_check_inflight`. Follow the 0014 rebuild pattern
  exactly.
- Migrations are versioned, and `CHECKSUMS` in the migrations directory must be
  updated, following the project's existing pattern.

### 2. Settings

| Setting | Default | Notes |
|---|---|---|
| `new_game_window_days` | `7` | Karl's 7 days |
| `new_game_unknown_alert_hours` | `48` | Karl's U1 |
| `new_games_check_enabled` | `true` | the same on/off pattern as `validation_sweep_enabled` |
| `new_games_check_cron` | `30 2,8,14,20 * * *` | UTC, validated by `_checked_cron` |
| `steam_download_poll_sec` | `600` | T1 poll (§3a); `0` disables it |
| `eviction_poll_sec` | `300` | P2 poll (§3b, Feature B); `0` disables it |
| `kuma_push_new_games` | `None` | env `ORCH_KUMA_PUSH_NEW_GAMES`; liveness; unset disables it |
| `kuma_push_new_games_overdue` | `None` | env `ORCH_KUMA_PUSH_NEW_GAMES_OVERDUE`; overdue; unset disables it |

**Why that cron (my call).** The SteamPrefill host cron runs `0 */6 * * *` in the
NAS's local time, which is MST (UTC-7) in winter. That is 01/07/13/19 UTC, and a
run usually ends within about 60 minutes. 02:30, 08:30, 14:30 and 20:30 UTC lands
about 1.5 h after each start in winter, and about 2.5 h after in summer (MDT).

It also avoids the scheduled-prefill (`45 3,9,15,21`) and sweep (`0 3,9,15,21`)
minutes. On Wednesdays it queues behind a running sweep, which is acceptable.

### 3. New job kind `new_games_check`

- Registered in `scheduler/manager.py` the same way as the sweep: a `CronTrigger`,
  UTC, only when enabled.
- `scheduler/jobs.py` gets an `enqueue_new_games_check`, with `source='scheduler'`
  and dedup through the new `idx_jobs_new_games_check_inflight`.
- **Sync Steam first (my call).** The handler's first step runs the Steam library
  sync inline (the same code as `_steam_library_sync`), so a game SteamPrefill
  started since the last sync gets its row before the check set is read. Without
  this, `library_sync` runs on a 6 h interval counted from process start
  (`scheduler/manager.py:134-141`), and the worst case from download to badge would
  be about 12 h, not about 6 h.
- **The check set.** These are the games the job measures. All of the following
  must hold:
  - `owned = 1`
  - platform is `steam` or `epic`
  - `first_seen_at` is within the last `new_game_window_days`
  - `status IN ('unknown','not_downloaded','validation_failed')`
  - there is no `block_list` row for (platform, app_id)
  - there is no `prefill_exclusions` row with `mode='exclude'` for (platform, app_id)
- **Why validation_failed is included (my call).** A "Partly cached" new game is
  usually still downloading, so it stays in the check set until it is
  `up_to_date`.
- **Why not `pending_update`.** Such a game is cached already, so it is out of
  scope.
- **Per game,** the job calls the existing `validate_one_game(pool, deps, game_id,
  settings)` in `jobs/handlers/validate.py`. That runs the platform validator,
  writes a `validation_history` row, and calls `record_measurement` in one
  transaction. Cache status is therefore still written only by
  `record_measurement`, and the build-breaking writer-guard test keeps passing.
- **Errors.** A per-game error is handled as the sweep handles it:
  `record_measurement(pool, game_id, "error")`, log, and continue.
  `CircuitBreakerTripped` aborts the job as failed, as in the sweep.
- **The overdue set.** These are the games the alert reports: `owned = 1`, the
  same block and exclusion filters as the check set, and **either** reason below.
  1. **Not cached after the window (the download failed).** `first_seen_at` is
     older than `new_game_window_days`, and `status` is not `up_to_date`. A
     `pending_update` game counts as cached.
  2. **Still unknown after 48 h (measuring is failing; Karl's U1).**
     `status = 'unknown'` and `first_seen_at` is older than
     `new_game_unknown_alert_hours` (default `48`). By then the game has been
     through about 8 checks, so the cause is measurement, not download.
  - The overdue message tags each title with its reason (`not cached` or
    `unmeasured`), because the two need different fixes.
  - Rows with NULL `first_seen_at` (the pre-existing library) are never overdue
    under either reason. The weekly sweep keeps covering them.
- **How long it stays DOWN (my call).** Overdue games keep the monitor DOWN until
  each one is cached, blocked or excluded. There is no time-out. A failed
  download needs a decision, and a time-out would be a silent fallback. The
  bounded fix for "a red monitor people ignore" is that blocking or excluding a
  game clears it.
- **Automatic blocks clear it too (my call).** Examples are #388's auto-block
  after 10 manifest 4xx, and Game_shelf's automatic exclusions. Neither is
  silent: a blocked game shows **Blocked** in Game_shelf, and an excluded one is
  covered on another launcher. So the overdue SQL does not need to tell sources
  apart, and no schema change is needed.
- **Two monitors, not one (my call; the keys_zone 219/220 lesson).** One monitor
  cannot tell "the check stopped running" from "a game is overdue". That is the
  #326/#330/#337 defect class.
  - **`job:orch-new-games-check`** (setting `kuma_push_new_games`) is liveness
    only. It goes through the normal `monitor_url_for` path and is UP whenever a
    scheduled run finishes.
  - **`orch:new-games-overdue`** (setting `kuma_push_new_games_overdue`) is
    pushed explicitly by the handler on every scheduled **and** trigger run
    (`source` in `scheduler`, `trigger`): DOWN while any game is overdue, UP
    otherwise. CLI and API runs never push it.
- **The result.** The job returns a `JobSummary` with `ok = True` (the check ran)
  and `msg` = `"checked N new, C cached; overdue K"`. The overdue push's message
  lists every overdue title, truncated to fit Kuma's message field.
- **Known limit.** Kuma notifies only when status changes. A second game going
  overdue while the overdue monitor is already DOWN sends no new notification.
  The message still lists it, and an `orch.new_games.overdue` log line is written
  for each game on each run.
- **Heartbeat wiring.** `jobs/worker.py::monitor_url_for` maps `new_games_check`
  to `kuma_push_new_games`. It is scheduler-only as today, so neither a trigger run
  nor a manual run can mask a dead scheduler. The overdue monitor follows the rule
  above.
- **The `jobs.source` value `'trigger'`.** `jobs.source` has a CHECK list,
  `('scheduler','cli','gameshelf','api')` (`0014_jobs_kind_purge.sql:41-42`), so
  the 0019 rebuild adds `'trigger'` to it as well.
- **Manual trigger.** `orchestrator-cli` gains a command that enqueues
  `new_games_check` with `source='cli'`, matching how the other job kinds are
  hand-run.

### 3a. Trigger when SteamPrefill finishes (Karl's T1)

- **How the signal is picked up.** The control plane polls the agent's existing
  `GET /v1/steam/downloaded-state` every `steam_download_poll_sec` (default `600`).
  That endpoint returns SteamPrefill's own record, `successfullyDownloadedDepots.json`.
- **When it fires.** When the set of (app, depot) entries differs from the
  previous poll and any entry was added, the control plane enqueues
  `new_games_check` with `source='trigger'`. The in-flight index deduplicates
  bursts while SteamPrefill is still running.
- **What it is not.** The record is never treated as cache truth. It only decides
  when to measure. Cache status still comes only from `record_measurement`.
- **The backstop.** The 6-hourly cron stays, for Epic and for any missed trigger.
- **How it reports.** A trigger-run check pushes the overdue monitor, which must
  reflect every check. It does not push the liveness monitor, which stays
  scheduler-only so a dead scheduler can't be masked.
- **Restart behaviour.** The previous set is held in memory. After a restart the
  first poll only records the set and does not trigger, so a deploy never causes a
  spurious check.

### 3b. Full re-check after a cache eviction (Karl's P2)

- **The signal today.** cache-catcher (`tools/cache_catcher/fanotify_guard.py`
  `handle_delete`, :275-329) already raises an `eviction` alert when 10 or more
  md5-named cache files are deleted within 60 s by any process other than the
  orchestrator agent (`delete_actor.classify_actor`, #337). Commanded purges never
  count. Today the alert goes only to email and to Kuma 220.
- **cache-catcher change.** On each `eviction` alert that is actually sent (not
  cooldown-suppressed), it also appends one JSON line `{ts, deletes, window_s,
  actor}` to an events file on a new small shared volume.
- **Agent change.** A new endpoint, `GET /v1/cache/eviction-events?since=<ts>`,
  reads that file. The volume is mounted **read-only** into the agent. It is a
  dedicated volume, not cache-catcher's `/log`, because `/log` holds the SMTP
  credentials and the Kuma push URLs.
- **Control-plane change.** It polls the endpoint every `eviction_poll_sec`
  (default `300`). On a new event, it **restarts the sweep pass at the event's
  timestamp** and enqueues a sweep.
  - Restarting the pass sets `sweep_pass.pass_started_at` to the event time
    through `jobs/sweep_pass.py`, so every game measured before the eviction is
    measured again.
  - The sweep goes through `enqueue_validation_sweep`, non-full.
- **Running until the pass completes.** Today a sweep that stops partway
  (`deadline_hit`) does not continue on its own. Add a `forced` flag to the
  `sweep_pass` row:
  - While it is set, a sweep that ends partway enqueues the next sweep before it
    returns.
  - `complete_pass` clears the flag.
  - Other queued jobs (new-game checks, Epic prefill, one-off validates) run in
    the gap between chained sweeps, because the queue is FIFO. That is how they
    share the ~16 h pass.
  - Scheduled Wednesday sweeps simply join the same pass.
- **Exactly once.** The control plane remembers the newest event `ts` it acted on,
  in a one-row table, so a restart doesn't re-trigger it.
- **Feature B's own migration, 0020.** It adds that one-row table and the
  `sweep_pass.forced` column. Feature A's 0019 does not include them, so each PR
  deploys alone.
- **Monitoring.** No new monitor is needed. Kuma 220 already alerts on the
  eviction, and the forced pass reports through the existing sweep monitor (180).
- **Out of scope.** `mode000` alerts. They are a permissions fault, not data loss,
  and they keep their existing alert path.

### 4. API

- `GameResponse` in `api/routers/games.py` gains two fields:
  - `first_seen_at: str | None`
  - `is_new: bool`, computed in `_row_to_game_response` as `first_seen_at` within
    `new_game_window_days` of now, **and** neither blocked nor excluded. An
    excluded new duplicate would otherwise show "New - Not Cached" for 7 days on a
    launcher that is deliberately not used.
- The orchestrator owns the window, so Game_shelf never duplicates the 7.
- `GameResponse` is `extra="forbid"`, so the model and the mapping change together.
- The API docs under `docs/api and interfaces/` are updated.

### 5. Monitoring setup (deploy step, needs a change card)

- **Two new Kuma push monitors**, in the group that holds 179 and 180, both bound
  to notification 3:
  - **`job:orch-new-games-check`**, with a heartbeat interval of about 7 h (the
    6 h cadence plus slack, matching 179 and 180).
  - **`orch:new-games-overdue`**, with the same interval.
- **Wiring.** Their push URLs go into `/root/orch-lxc.env` as
  `ORCH_KUMA_PUSH_NEW_GAMES` and `ORCH_KUMA_PUSH_NEW_GAMES_OVERDUE`.
- **NAS compose (P2).** A new named volume `cache-catcher-events`, mounted
  read-write into `cache-catcher` and **read-only** into `orchestrator-agent`.
  - Recreating the agent re-checks `user: "0:0"` and 256/256 buckets.
  - Recreating cache-catcher re-checks that monitors 219 and 220 resume.
- **Deploy order, Feature A.**
  1. The control plane: migration 0019, the job, the T1 poller and the API fields.
     The agent is unchanged, because `downloaded-state` already exists.
  2. Game_shelf.
- **Deploy order, Feature B.**
  1. The NAS compose volume, then the agent (the new endpoint).
  2. cache-catcher (the events file).
  3. The control plane (migration 0020 and the eviction poller).
  - A control plane polling an agent that lacks the endpoint must log and retry.
    It must not crash and must not trigger anything.

## Design — Game_shelf

1. **Badge.** In `frontend/src/utils/cacheBadge.js`, `cacheBadgeFor` gains an
   `isNew` input. When `isNew && status in (unknown, not_downloaded)` and the game
   is not blocked, it returns the label **"New - Not Cached"**.
   - It needs its own icon as well as its colour (accessibility rule: never colour
     alone). **My call:** a sparkle or "new" glyph from the icon set already used.
   - "Blocked" still overrides everything.
   - `validation_failed` still shows "Partly cached · N%" during the window, which
     tells the user more.
2. **Wiring.** `perStoreStatus.js` and `GameCard.jsx` pass `is_new` through from
   the orchestrator response, like `status` and the chunk counts.
   - The new icon also goes into `perStoreStatus.js` `LANCACHE_ICON_KIND` and
     `STORE_SYMBOLS`.
   - `cacheCounts()` (`cacheBadge.js:61-80`) gets a "New - Not Cached" tile. New
     games must not be counted in the Not cached or Unknown tiles. That is the
     tile-versus-badge disagreement #230 fixed.
   - After day 7, an overdue game shows plain "Not cached" (my call). The overdue
     monitor is how it stays loud.
3. **Filter.** `backend/src/services/cacheSnapshot.js` currently stores
   `blocked ? 'blocked' : status`. It must store a derived value of `'new'` under
   exactly the badge rule above, and the `cache_status` filter in
   `backend/src/routes/games.js` gains a "New - Not Cached" option.
   - The badge rule and the filter rule must come from one shared definition, or
     be covered by a test asserting they agree. This is the 2026-09-10 lesson.
4. **Freshness.** The existing caches (60 s backend snapshot, 30 s react-query
   `staleTime`) are fine. Nothing new is needed.

## Constraints

- **Deploy order.** Orchestrator first: the migration plus the job plus the API
  field. Game_shelf second. A Game_shelf build against an old orchestrator gets
  no `is_new` and must treat that as false, so it falls back to today's labels.
- **Container recreates** happen only in an empty-queue gap, and never during a
  Wednesday sweep.
- **Project rules.**
  - The orchestrator is in Phase 2: one feature, the full Build Loop
    (`process-checklist.sh`), test-first, CHANGELOG, FEATURES.md, Bible.
  - The UAT test gate is clear, with 2 features until the next UAT session.
  - Commits go through the recorded-approval flow.
- **ADR.** Write one for "first-seen time, not purchase date, defines new" and for
  "the overdue alert never times out".

## Left to the builder

- Exact SQL and indexing. The check set is tiny, so no index is expected to be
  needed, but measure it first.
- The CLI command's name, and the log event names, following the existing
  `sweep.*` style.
- How the title list in the Kuma message is truncated.
- Test structure, provided it covers:
  - the insert-only `first_seen_at`, including the conflict path leaving it alone;
  - every filter in the check set and the overdue set;
  - the no-time-out behaviour;
  - the scheduler-only heartbeat;
  - Game_shelf badge and filter agreement;
  - the old-orchestrator fallback (`is_new` missing → false);
  - the T1 poller: a new entry triggers, an unchanged set does not, and the first
    poll after a restart does not;
  - the P2 path: a new event restarts the pass at its `ts` and chains sweeps until
    the pass completes, an already-acted event does not re-trigger, a
    cooldown-suppressed alert writes no event, and an orchestrator-actor purge
    writes no event.
