# F8 — Block List + Scheduled Prefill Driver (version-diff) — Design

**Status:** Approved (design) — 2026-06-17
**Feature:** F8 (MVP must-have) + completion of F12's "diff enqueues prefills" step
**Phase:** 2 (Construction)
**Author:** Orchestrator + AI agent (brainstorming session 2026-06-17)

---

## 1. Goal

Make the orchestrator the **automatic Steam+Epic prefill driver** (End-state 1): on a recurring 6-hour cycle it detects which owned games are new or patched and prefills only those, validates the result, and lets the operator **block** specific games from that automatic prefill. This is the last orchestrator-repo MVP feature before the Game_shelf integration (F14–F17).

The orchestrator's prefill (F5/F6) is intended to **supersede the host's standalone SteamPrefill/EpicPrefill cron jobs** for Steam and Epic (GOG is out of scope — that cron stays; so does the `chmod_fix.sh` heal cron). The recommended rollout is parallel-run-then-retire: prove the orchestrator fills+validates correctly live, then disable the Steam/Epic cron.

## 2. Background & key discoveries

Established during the design session by reading the codebase:

- **`block_list` table already exists** (migration `0001_initial.sql`) exactly as needed: `id`, `platform CHECK IN ('steam','epic')`, `app_id`, `reason` (nullable, ≤500), `source CHECK IN ('cli','gameshelf','api','config')`, `blocked_at`, `UNIQUE(platform, app_id)`, no FK to `games` (so an unknown app_id can be pre-blocked). **No migration is required.**
- **`block_list` is referenced nowhere in code** — it is a dormant table.
- **There is no scheduled-prefill loop today.** Prefill is enqueued only by the manual `POST /api/v1/games/{id}/prefill` trigger. The scheduler runs `library_sync` (interval) and `sweep` (cron) only. The Manifesto user-journey (§94, "F12 diff enqueues prefills") describes the missing step.
- **`current_version` / `cached_version` columns exist on `games` but nothing writes them** — they are vestigial today. They must be populated for the version-diff model.
- **Steam library enumeration already fetches version data.** The batched product-info call carries each depot's manifest GID (`manifest_gids_for_app` / `extract_manifest_gid` in `platform/steam/enumerate.py` already parse them) and the public-branch `buildid`. So Steam `current_version` is obtainable for free during the existing library sync.
- **Epic library enumeration carries `buildVersion`** per asset in the library/assets response.
- **Prefill respects the cache** — no `nocache`/bypass anywhere in the orchestrator code. Re-running prefill on an already-cached game pulls **0 bytes over WAN** (all lancache HITs); only changed chunks (a patch's delta) are fetched from the CDN. (The `BYPASS`/`?nocache=1` entries seen in the lancache access log come from a LAN Steam client or the standalone cron tool, not the orchestrator.)
- **F7 validation is disk-stat only** — it `stat()`s cache files (existence + size) against a manifest's computed cache keys; it does not download or read full chunk bytes. This makes it the cheap way to confirm "already cached at the latest version."

## 3. Design decisions (resolved forks)

1. **Orchestrator role = prefill driver** (not just verifier). Build the scheduled diff-enqueue.
2. **Version-diff model** (not blanket re-prefill). Track `current_version` + `cached_version`; each cycle enqueues prefill only for games that changed / were never cached / failed validation. Cost is O(changed games) per cycle, not O(library).
3. **`block_list` table is the single source of truth** for "blocked". Do **not** overwrite `games.status='blocked'` (it would clobber lifecycle state and need a fragile restore on unblock). "Blocked" is orthogonal to lifecycle status, surfaced as a computed `blocked: bool`.
4. **Existing cache is adopted, not rebuilt.** Validation success (not just prefill success) writes `cached_version = current_version`. Cold-start = run a validation sweep once: every already-cached game gets `cached_version` seeded via stat-only validation (no prefill, no download), so the scheduled diff then skips it.
5. **Manual triggers bypass the block-list.** `POST /games/{id}/prefill` and `/validate` are unchanged (operator override).
6. **The F13 validation sweep is not filtered by the block-list.** Per the F8 spec, validation still runs on blocked games; blocking only suppresses *scheduled prefill*.

## 4. Components

### 4.1 Version tracking

**`current_version` — written during `library_sync`** (the upsert in `jobs/handlers/library_sync.py`):

- **Steam:** the public-branch `buildid` (as a string) is the primary token — it changes on every app update. If `buildid` is absent, fall back to a deterministic composite: the SHA-256 (hex) of the sorted `"<depot_id>:<manifest_gid>"` pairs from `manifest_gids_for_app(depots, "public")`. `platform/steam/enumerate.py` must propagate the per-app version token (it currently returns only `{app_id, name, depots}` — add the version token), and the `_UPSERT_SQL` must set `current_version = excluded.current_version`.
- **Epic:** the `buildVersion` string from the library/assets `EpicLibraryItem`. The Epic library-sync upsert sets `current_version` from it.
- `current_version` is data-only — writing it does **not** change `status`, `cached_version`, or other lifecycle columns (preserves the BL11 "library_sync touches title/owned/metadata only" invariant, extended to add `current_version`).

**`cached_version` — written on confirmed-cached:**

- **Prefill handler**, on **full success** only, sets `cached_version = <version it prefilled>` (= the game's `current_version` at prefill time) and `last_prefilled_at`. Partial/failed prefill leaves `cached_version` unchanged.
- **Validate handler**, on a **clean validation** (all chunks present), sets `cached_version = <version it validated>` (= `current_version`). A failed validation leaves `cached_version` unchanged and flips `status='validation_failed'` (existing behavior).

### 4.2 Scheduled prefill driver (completes F12)

- New `enqueue_scheduled_prefill(pool)` in `scheduler/jobs.py`, mirroring `enqueue_library_sync` / `enqueue_validation_sweep`.
- Registered in `scheduler/manager.py` on the 6-hour interval (the `SCHEDULE_CRON`/library-sync cadence), fired **after** the library sync so newly-enumerated versions are current. `replace_existing=True`, `coalesce=True` (consistent with existing jobs).
- Single bulk diff insert:
  ```sql
  INSERT INTO jobs (kind, game_id, platform, state, source)
  SELECT 'prefill', g.id, g.platform, 'queued', 'scheduler'
  FROM games g
  WHERE g.owned = 1
    AND (g.cached_version IS NULL
         OR g.cached_version <> g.current_version
         OR g.status = 'validation_failed')
    AND NOT EXISTS (
      SELECT 1 FROM block_list b
      WHERE b.platform = g.platform AND b.app_id = g.app_id)
  ON CONFLICT DO NOTHING
  ```
- `ON CONFLICT DO NOTHING` + the existing migration-0006 in-flight partial-unique index dedups against a prefill already queued/running for that game. Completed prefills are eligible again only when their game next diverges (version change or `validation_failed`).
- Rows with `current_version IS NULL` (never enumerated/version-resolved) are covered by the `cached_version IS NULL` clause and will be prefilled, which resolves their version.

### 4.3 Block-list API (`api/routers/block_list.py`, new)

Mirrors F9 conventions (bearer auth via middleware, `ConfigDict(extra="forbid")` on all models, `_query_helpers` pagination, `PoolError → 503`):

- **`GET /api/v1/block-list`** — paginated wrapped envelope `{"block_list": [BlockEntry...], "meta": {...}}`. Filters: `platform` (eq, in), `source` (eq, in). Sort allow-list: `blocked_at`, `platform`, `app_id`, `id` (default `blocked_at:desc`, tie-break `id:asc`). `limit`/`offset` pagination (NOT `per_page`).
- **`POST /api/v1/block-list`** — body `{platform, app_id, reason?, source?}` (`source` defaults to `'api'`). Idempotent: `INSERT ... ON CONFLICT(platform, app_id) DO NOTHING`, then SELECT and return the row. Accepts **unknown** `(platform, app_id)` (pre-block). `201` on new insert, `200` on existing. `422` on schema violation (extra fields / bad `platform` / `app_id` length / `reason` >500), `503` on PoolError.
- **`DELETE /api/v1/block-list/{platform}/{app_id}`** — idempotent: `DELETE ... WHERE platform=? AND app_id=?`. Returns `200 {"removed": <0|1>}` (succeeds even when absent). `503` on PoolError.

`BlockEntry` response model fields: `id, platform, app_id, reason, source, blocked_at` (`extra="forbid"`).

### 4.4 Games API — `blocked` field

- Add `blocked: bool` to `GameResponse` (`api/routers/games.py`), computed via `LEFT JOIN block_list b ON b.platform = g.platform AND b.app_id = g.app_id` and selecting `b.id IS NOT NULL AS blocked`. `block_list` is the source of truth; `games.status` is not consulted for blocked-ness. The new field is additive (allowed under `extra="forbid"` since it is declared).

### 4.5 CLI (`cli/commands/game.py`)

- **`game block <game_id> [--reason TEXT]`** — resolve `game_id → (platform, app_id)` client-side (reuse the `game show` list-filter approach, since there is no detail endpoint), then `POST /api/v1/block-list {platform, app_id, reason, source:'cli'}`. Print confirmation. Exit 1 if the game id is not found.
- **`game unblock <game_id>`** — resolve `game_id → (platform, app_id)`, then `DELETE /api/v1/block-list/{platform}/{app_id}`. Print confirmation (idempotent — reports removed/not-blocked).
- **`game list`** — add a `blocked` column (a glyph or `yes`/`-`) sourced from the new `GameResponse.blocked` field.
- Pre-blocking *unknown* games stays API-only (Game_shelf/config use `source='gameshelf'|'config'`), matching the Manifesto's `game block <id>` CLI shape.

### 4.6 Cold-start cache adoption

- Operational procedure, not new code beyond §4.1's "validation writes `cached_version`": after deploy, run a validation sweep across owned games (the existing F13 sweep mechanism / a one-shot). Already-cached games validate clean (stat-only) and get `cached_version = current_version` seeded → the scheduled diff skips them. Genuinely missing/outdated games remain `cached_version IS NULL` / divergent → prefilled.
- **Default mechanism:** reuse the existing F13 sweep, widening its candidate query so the adoption pass also includes never-validated owned games (currently it targets `status IN ('up_to_date','validation_failed')`; add `'unknown'`/`'not_downloaded'` for the adoption pass). The operator triggers one sweep after deploy; thereafter the weekly sweep keeps `cached_version` honest.

## 5. Data flow (steady state)

```
scheduler (every 6h)
  ├─ enqueue_library_sync ──► worker: library_sync_handler
  │       └─ enumerate owned apps + versions ─► UPSERT games (title, owned, metadata, current_version)
  └─ enqueue_scheduled_prefill (after sync)
          └─ diff query ─► enqueue 'prefill' jobs (source='scheduler') for changed/never-cached/validation_failed, non-blocked, owned
                 └─ worker: prefill_handler (F5/F6)
                        ├─ fetch latest manifest, request chunks via lancache (HIT=local, MISS=CDN delta)
                        ├─ on full success: cached_version = current_version, last_prefilled_at
                        └─ enqueue 'validate' job
                               └─ worker: validate_handler (F7 disk-stat)
                                      ├─ clean: cached_version = current_version, status=up_to_date
                                      └─ missing chunks: status=validation_failed (→ re-prefilled next cycle)

manual POST /games/{id}/prefill | /validate  ─► bypass block-list (operator override)
F13 weekly sweep ─► validates cached games (NOT block-filtered); keeps cached_version honest, catches eviction
block-list API/CLI ─► add/remove rows in block_list (source of truth)
```

## 6. Block semantics

- A game is "blocked" iff a `block_list` row exists for its `(platform, app_id)`.
- Blocking only suppresses **scheduled** prefill enqueue (§4.2 filter). It does not delete cache, change `games.status`, or stop manual prefill/validate or the F13 sweep.
- Version-diff catches upstream **patches**; the F13 sweep's `validation_failed` catches local **eviction**. The `enqueue_scheduled_prefill` condition combines both for complete "needs prefill" coverage.

## 7. Error handling & status codes

- Block-list `POST`: `201` new / `200` existing / `422` invalid body / `503` PoolError. Never `5xx` on a duplicate (idempotent).
- Block-list `DELETE`: `200 {"removed": 0|1}` / `503` PoolError.
- Block-list `GET`: `200` envelope / `400` bad query params (via `_query_helpers`) / `503` PoolError.
- All non-health endpoints require bearer auth (middleware) → uniform `401`.
- `enqueue_scheduled_prefill` swallows nothing silently: it logs the enqueued count (structured log) and lets pool errors propagate to the scheduler's existing error handling (consistent with `enqueue_library_sync`).

## 8. Testing strategy (TDD)

- **`tests/api/test_block_list_router.py`** (new): GET empty/populated + envelope shape + filters/sort/pagination; POST new (`201`) / duplicate (`200`, idempotent) / pre-block unknown app_id / invalid body (`422`, extra=forbid) / PoolError (`503`); DELETE present (`removed:1`) / absent (`removed:0`) / PoolError.
- **`tests/cli/test_cmd_game.py`** (extend): `game block <id>` resolves and POSTs (assert path/body/source); `--reason` carried; unknown id → exit 1; `game unblock <id>` resolves and DELETEs; idempotent unblock message; `game list` renders the blocked column.
- **`tests/api/test_games_router.py`** (extend): `GameResponse.blocked` true/false via LEFT JOIN.
- **`tests/scheduler/` (new or extend)**: `enqueue_scheduled_prefill` selects changed / `cached_version IS NULL` / `validation_failed`; excludes blocked and unowned; dedups via ON CONFLICT; sets `source='scheduler'`.
- **`tests/platform/steam/` + library_sync tests**: `current_version` populated from buildid (and composite fallback); Epic `buildVersion` populated.
- **`tests/jobs/test_prefill_handler.py`** (extend): `cached_version` set on full success only, untouched on partial/failure.
- **`tests/jobs/test_validate_handler.py`** (extend): `cached_version` set on clean validation; untouched + `validation_failed` on missing chunks.

## 9. Known limitations

- **Scheduled Steam prefill needs a valid session.** With an expired session, the cycle's prefill jobs fail auth (and flip `platforms.auth_status='expired'`) until the operator re-authenticates. Documented; not auto-recovered.
- **Steam version token format** is `buildid`-primary with a depot-GID composite fallback; final selection pinned in the plan against live product-info output.
- **Epic `buildVersion` wiring** depends on the library/assets response carrying it on `EpicLibraryItem`; confirmed present in the model path during planning.
- **Cold-start adoption** requires one validation sweep over owned games; until it runs (or the weekly F13 sweep covers a game), a game with `cached_version IS NULL` will be prefilled once (WAN-free if already cached, but a full prefill pass rather than stat-only) — running the adoption sweep first avoids this.

## 10. Out of scope (future / not this feature)

- Retiring the host Steam/Epic prefill cron (operational step after a live parallel-run proves the driver).
- GOG prefill (orchestrator is Steam+Epic only).
- `GET /api/v1/games/{id}` detail endpoint (#141) — CLI continues to resolve via the list.
- Per-game prefill scheduling/priority, bandwidth throttling, partial-library selection.
- Game_shelf block-list management UI (F16) — consumes this API later.

## 11. File-level change map (for planning)

- `src/orchestrator/platform/steam/enumerate.py` — emit per-app version token (buildid / depot-GID composite).
- `src/orchestrator/jobs/handlers/library_sync.py` — Steam + Epic upserts set `current_version`.
- `src/orchestrator/platform/epic/{library,models}.py` — carry `buildVersion` on `EpicLibraryItem` (verify/extend).
- `src/orchestrator/jobs/handlers/prefill.py` — set `cached_version` + `last_prefilled_at` on full success.
- `src/orchestrator/jobs/handlers/validate.py` — set `cached_version` on clean validation.
- `src/orchestrator/scheduler/jobs.py` — new `enqueue_scheduled_prefill(pool)`.
- `src/orchestrator/scheduler/manager.py` — register the 6h scheduled-prefill job.
- `src/orchestrator/api/routers/block_list.py` — new router (GET/POST/DELETE).
- `src/orchestrator/api/app.py` (or router registration) — mount the block-list router.
- `src/orchestrator/api/routers/games.py` — `blocked` field + LEFT JOIN.
- `src/orchestrator/cli/commands/game.py` — `block` / `unblock` subcommands + `list` column.
- Tests per §8. No DB migration. CHANGELOG/FEATURES updated at build time.
