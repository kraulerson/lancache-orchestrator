# Durable Steam Manifest Store + Validate-All Backfill — Design

**Date:** 2026-06-24
**Status:** Approved (design)
**Re-arch follow-up to:** ③ steam worker deletion ([[project_steam_worker_deletion]]), ④ LXC move ([[project_lxc_move]])
**Root-cause memo:** [[project_validation_manifest_gap]]

## Problem (proven live 2026-06-24)

The data-plane agent validates a Steam game by reading that app's manifest (chunk
list) from SteamPrefill's `.bin` cache only:
`POST /v1/steam/validate` → `locate_manifest_bins(app_id, cache_root=/steamprefill-cache)`
→ `/steamprefill-cache/v1/{app}_{app}_{depot}_{gid}.bin`. If no `.bin` exists it
returns `outcome="error", error="no_manifest_in_cache"`, and the control plane
deliberately leaves the game's status unchanged (`error` is excluded from
`_STATUS_FOR` in `jobs/handlers/validate.py`).

Live facts on the NAS (192.168.1.40):

- Karl's root cron (`/SteamPrefill/prefill_cronjob.sh`) prefills **1077 selected
  apps** (`selectedAppsToPrefill.json`); SteamPrefill has downloaded **2248
  depots** (`successfullyDownloadedDepots.json`) ≈ **13 TB** on the NFS lancache
  (`192.168.1.30:/volume1/cache` → `/lancache/lancache/cache` → agent
  `/data/cache`; real chunks at `/data/cache/cache/H[-2:]/H[-4:-2]/H`).
- The agent's manifest cache (`/root/.cache/SteamPrefill/v1`, mounted RO at
  `/steamprefill-cache/v1`) holds manifests for only **330 distinct apps**.
- Cause: SteamPrefill only *writes* a depot manifest `.bin` when it actually
  downloads that depot's content. An app that is already up to date is skipped
  (it compares manifest GIDs from cheap product-info, never fetching the full
  manifest), so its `.bin` is never (re)written. SteamPrefill v3.4.2 has **no
  manifest-only mode**; `prefill -f|--force` ("always run, overrides only-if-newer")
  is the only lever to regenerate a manifest, and it re-requests the app's chunks.
- Live proof: app `105600` (has manifest) → `partial 1670/15820`; app `1517970`
  (selected, no manifest) → `error / no_manifest_in_cache`.

Net: ~747 of Karl's selected/prefilled apps are **structurally unvalidatable** —
their chunks are on disk but the orchestrator can't get a manifest to check them.
The F13 sweep only re-checks `status IN ('up_to_date','validation_failed')`, so
these games (sitting at `unknown`/`not_downloaded`) are never revisited.

## Goal

Let the orchestrator validate the **entire** prefilled Steam library — durably —
**without** adding an independent Steam manifest fetcher (i.e. keep the
SteamPrefill-only architecture from re-arch ③; do not revive SteamKit/ValvePython
or add DepotDownloader).

## Non-goals

- Epic. There is no disk-stat validator for Epic at all (validate route 400s for
  non-steam; sweep is steam-only). Epic is **deferred** to a separate feature.
- Pre-reverting the 2156 wrongly-`not_downloaded` rows. The backfill pass
  (Component C) corrects them in one shot; no separate revert.
- Pruning owned-but-not-selected clutter rows from `games`. Out of scope.

## Architecture

One feature, two code halves (agent + control plane) plus an operator runbook.

### Component A — durable manifest archive + union read (agent)

A new permanent docker volume `orchestrator-manifests` mounted **rw** at
`/manifest-archive`, mirroring SteamPrefill's layout
(`/manifest-archive/v1/{app}_{app}_{depot}_{gid}.bin`).

New setting `steam_manifest_archive_dir: Path = Path("/manifest-archive")`
(`src/orchestrator/core/settings.py`). The archive is treated as **append-only**
and is never touched by SteamPrefill's `clear-temp`/pruning.

`src/orchestrator/agent/manifest_locator.py` changes so both functions read the
**union** of `[live cache, archive]`:

- `locate_manifest_bins(app_id, *, cache_roots: list[Path]) -> list[Path]` —
  glob `{app}_{app}_*.bin` under each root's `v1/`, keep the **newest `.bin`
  per depot by mtime across all roots**.
- `list_prefilled_app_ids(*, cache_roots: list[Path]) -> list[int]` — distinct
  union of app_ids across all roots' `v1/*.bin`.

Each root is independently guarded by the existing `if not v1.is_dir(): continue`
check, so an **absent/unmounted archive contributes nothing → byte-identical to
today** (the archive dir's mere existence is the on/off signal; no separate flag).

The two callers in `src/orchestrator/agent/routers/steam.py`
(`steam_validate`, `prefilled_apps`) pass
`cache_roots=[settings.steam_manifest_cache_dir, settings.steam_manifest_archive_dir]`.

**Why union, newest-per-depot:** it fixes version drift for free. When an app
updates, SteamPrefill writes a newer-GID `.bin` to the *live* cache (newer mtime
→ wins over the archived old one); stable apps live only in the archive and are
still found. The archive copies preserve mtime (see Component B) so this ordering
holds.

### Component B — archive sync (agent)

`src/orchestrator/agent/manifest_archive.py` (new):
`sync_manifests_to_archive(live_root: Path, archive_root: Path, *, settle_seconds: float) -> int`

- Copy any `.bin` present under `live_root/v1` but **not** under `archive_root/v1`
  (compared by filename), using `shutil.copy2` to **preserve mtime**.
- **Append-only:** never deletes from the archive.
- **Settle guard:** skip files whose mtime is within `settle_seconds` of now
  (default 10s) so a half-written manifest is never copied (it is picked up on a
  later cycle once settled).
- Fault-isolated: per-file `try/except (OSError)` → skip + log; a missing or
  unwritable archive dir → log once + return 0 (never crashes the agent).
- Returns the count copied (for logging/metrics).

Runs as a **periodic agent background task** (new setting
`manifest_archive_sync_interval_sec: int = 1800`, i.e. every 30 min), wired into
the agent lifespan alongside the existing `agent_bg_tasks` (see
`src/orchestrator/agent/app.py`), and also once at startup. This is what pins the
manifests Karl's **root cron** writes — permanently, before any future
`clear-temp` can drop them. Interval `0` disables it.

### Component C — validate-all backfill (control plane)

Extend the existing F13 sweep with a `full` mode carried on the job's `payload`
(the `jobs.payload` TEXT/JSON column already exists; **no migration**):

- `src/orchestrator/jobs/handlers/sweep.py`: parse
  `full = json.loads(job.get("payload") or "{}").get("full", False)`. Select with
  `_CANDIDATE_SQL_FULL = "SELECT id, status FROM games WHERE platform='steam' ORDER BY id"`
  when `full`, else the existing status-gated `_CANDIDATE_SQL`. Everything else is
  reused unchanged: the agent-health pre-flight gate, the
  `asyncio.Semaphore(settings.sweep_batch_size)` bounded concurrency (already
  present — the NAS is CPU-steal-bound, so this matters), per-game error
  isolation, `validate_one_game` (records `validation_history`, maps
  `outcome → games.status`). No-manifest apps return `error` and correctly leave
  status unchanged.
- `src/orchestrator/scheduler/jobs.py`:
  `enqueue_validation_sweep(pool, *, full: bool = False)` sets
  `payload='{"full": true}'` when `full`. The weekly cron keeps `full=False`.
  Reuses the `idx_jobs_sweep_inflight` dedup (at most one sweep queued/running),
  so the one-off backfill is enqueued only when no sweep is in flight.
- Operator trigger: a new orchestrator-cli command
  `orchestrator-cli cache validate-all` (under `src/orchestrator/cli/commands/`)
  that enqueues a `sweep` job with `payload={"full": true}` via the loopback jobs
  API. (The jobs API/`POST /api/v1/jobs` already accepts `kind="sweep"`; the
  command passes the `full` payload.)

**Side benefit:** once the archive is seeded, `list_prefilled_app_ids` returns
~1077 apps, so steam `library_sync` enumeration self-heals toward the real
selected library instead of 330 (still budget-throttled per run by
`steam_store_fetch_budget=150`).

## Data flow (steady state, after rollout)

1. Karl's nightly root cron prefills new content → SteamPrefill writes manifests
   to the live cache.
2. Agent background sync (every 30 min) copies new manifests → durable archive
   (mtime preserved, settle-guarded, append-only).
3. `validate` reads `union(live, archive)`, newest-per-depot → can validate any
   app once its manifest has been archived.
4. Weekly F13 sweep re-checks `up_to_date`/`validation_failed` for drift (unchanged).

## Operator runbook (post-merge gates; Claude runs these on the boxes)

**R1 — mount the archive.** Add `-v orchestrator-manifests:/manifest-archive` to
`/home/karl/deploy-agent.sh`; recreate the agent. Set
`ORCH_STEAM_MANIFEST_ARCHIVE_DIR=/manifest-archive` (or rely on the default).
The named volume survives agent recreation.

**R2 — seed.** Compute the missing set = `selectedAppsToPrefill ∖ archived apps`.
Run `SteamPrefill prefill --force <batch>` for those app_ids in **throttled
off-hours batches** (LAN re-read from the warm lancache → HITs, no WAN), sized to
spare the NAS, coordinated so as not to overlap the root cron. The background
sync archives each batch's manifests as they appear. Repeat until the missing set
is empty. (Forcing only the missing apps avoids re-reading the already-archived
330.)

**R3 — backfill.** Once the archive is populated, run
`orchestrator-cli cache validate-all` once. The full sweep validates every steam
game: genuinely-cached → `up_to_date`, partial/missing → `validation_failed`,
no-manifest → unchanged. This corrects the wrongly-`not_downloaded` rows in the
same pass. Consider a lower `sweep_batch_size` env for this run given NAS load.

## Settings (new)

| Setting | Default | Purpose |
|---|---|---|
| `steam_manifest_archive_dir: Path` | `/manifest-archive` | durable manifest store (absent dir ⇒ off / parity) |
| `manifest_archive_sync_interval_sec: int` | `1800` | agent sync cadence; `0` disables |

## Error handling & rollout

- **Feature-flagged by presence of the archive dir.** Until the volume is mounted,
  `union` reads live-only and the sync no-ops — byte-identical to today. Safe to
  merge before any box changes.
- Sync and backfill are per-item fault-isolated; the archive is append-only and
  survives container recreation.
- The agent must still import neither `orchestrator.api.main` nor
  `orchestrator.db.pool` — the new agent module (`manifest_archive.py`) uses only
  stdlib (`shutil`, `pathlib`, `time`); the import-isolation guard
  (`tests/agent/test_import_isolation.py`) must stay green.

## Testing (TDD)

- `manifest_locator` union: newest-per-depot across two roots by mtime; archive-only
  app found; live-only app found; archive root absent → live-only (parity); both
  empty → `[]`; `list_prefilled_app_ids` returns the union set.
- `manifest_archive.sync`: copies new `.bin`; skips ones already in archive;
  preserves mtime; settle-guard skips too-fresh files; tolerates an unreadable
  file (skip+log); no-op when archive dir absent/unwritable; never deletes from
  archive; returns the copied count.
- `sweep` full mode: payload `{"full": true}` selects all steam games (quote the
  full SQL); `full=False`/absent payload keeps the status-gated SQL; reuses the
  semaphore, agent-health gate, and `validate_one_game`; a no-manifest game leaves
  status unchanged.
- `enqueue_validation_sweep(full=True)` writes the `{"full": true}` payload;
  `full=False` writes none and dedups as today.
- CLI `cache validate-all` enqueues a `sweep` job with the `full` payload.
- `settings`: new fields default + env override.
- Agent import-isolation guard still passes.

## Scope

Ships as **1 PR** (`feat/durable-manifest-store`): the agent half (A + B + the
two `manifest_locator` call-site updates) and the control-plane half (C) are
small and cohesive. The operator runbook (R1–R3) is executed post-merge and
monitored — not code.
