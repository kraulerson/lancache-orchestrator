# Design — auto-coverage for newly-purchased Steam games

<!-- 2026-07-07 -->

**Repo:** lancache_orchestrator (orchestrator-only — NO host-cron edits, NO 2FA). Branch `feat/steam-new-purchase-auto-coverage`.

## Problem

A newly-purchased Steam game is downloaded by the **host** SteamPrefill `prefill --recently-purchased` midnight cron (confirmed live: Raft, app 648800 / depot 648801, 2.09 GiB pulled through the lancache at 12:47 AM). But the orchestrator shows `games.status='unknown'` indefinitely, so Game_shelf shows "Unknown". The host downloader and the orchestrator (status/coverage) are separate systems, and the orchestrator's automatic pipeline never catches a game that arrives outside the SteamPrefill *selection*.

### Root causes (due-diligence workflow, adversarially verified SOUND)

1. **Load-bearing bug — `unknown` games are never auto-validated.** A new steam row is inserted at the schema default `status='unknown'` (`library_sync.py::_NAMED_UPSERT_SQL` writes `title`+`owned` only). The scheduled *gated* validation sweep (`sweep.py::_CANDIDATE_SQL`) enumerates only `status IN ('up_to_date','validation_failed')`, and there is **no scheduled full sweep** (`manager.py` registers `enqueue_validation_sweep` with `full=False`; `full=True` is on-demand API/CLI only). So no cron ever enqueues a validate for an `unknown` game → it stays `unknown` forever.
   - `games.metadata`/`current_version` being NULL is a **red herring**: the validator (`disk_stat.validate_game` → agent `locate_manifest_bins` over the manifest cache roots) reads on-disk `.bin`/`.shas` only, never `games.metadata` (the ~2485 rows that have depot metadata are legacy pre-③ ValvePython data; no current code writes steam `games.metadata`/`current_version`). **Raft's `.bin` is already on the agent-visible cache root**, so a validate resolves it to cached immediately.
   - A validate with no manifest returns `outcome='error'`, which is **non-clobbering** (`validate.py` leaves status unchanged) — so enumerating `unknown` rows is safe even for genuinely-uncovered ones.

2. **Durability gap — recent purchases get no durable `.shas`.** `manifest_fetcher.py::_enumerate_app_ids` reads **only** `selectedAppsToPrefill.json`. Recently-purchased games are downloaded outside the selection, so they never get a durable `.shas` sidecar. The `.bin` cache is prunable; the `.shas` in the archive volume is the durable coverage record `PR #213` introduced.

3. **Operator enhancement (Karl).** A recently-purchased download should persist into `selectedAppsToPrefill.json` so it shows checked in `SteamPrefill --select-apps` and joins the durable prefill set (survives future `prefill` passes).

## Locked decisions

- **Immediacy = within 6h** (sweep only). Do NOT add a near-immediate `library_sync` validate-enqueue.
- **Durable `.shas` cadence = leave weekly.** Do NOT change the `fetch_manifests` cron.
- **Do NOT populate `games.metadata`/`current_version` for steam** (vestigial).

## Architecture — three independent pieces

### Piece 1 — sweep validates `unknown` games (control plane; the load-bearing fix)
`src/orchestrator/jobs/handlers/sweep.py::_CANDIDATE_SQL`: extend the gated predicate to
`WHERE status IN ('unknown','up_to_date','validation_failed') AND owned = 1`
(the `owned = 1` guard bounds churn to owned games). The existing every-6h gated sweep (`manager.py` cron `0 3,9,15,21`) then enqueues a `validate` for any newly-discovered game. `validate.py` reads the on-disk `.bin` and flips status via `_STATUS_FOR` (cached→`up_to_date`, partial/missing→`validation_failed`); a no-manifest app returns `outcome='error'` and is left `unknown` (non-clobbering). **Zero agent change.** Raft → `up_to_date` within one sweep cycle post-deploy.

### Piece 2 — bounded fetcher widening (agent; durability)
`src/orchestrator/platform/steam/manifest_fetcher.py`: add a `manifest_cache_dir` param to `DepotDownloaderManifestFetcher.__init__`, and widen `_enumerate_app_ids` from selection-only to the **union of the selection with the BOUNDED subset** of app_ids that have a `.bin` in the manifest cache roots **but no matching-gid `.shas`**. Enumeration reference: `manifest_locator.list_prefilled_app_ids` glob over the cache roots — **inline the glob**; do NOT `import orchestrator.db.*` / `orchestrator.api.*` (keeps `tests/agent/test_import_isolation.py` green). Keep the existing `delay_sec` throttle + `_TRANSIENT_RE` backoff — the **first run** is the rate-limit spike to guard (#228 hazard); the bounded "has `.bin`, no matching `.shas`" subset is precisely what prevents a naive ~330–1077-app logon burst. `_write_shas` is already idempotent (already-covered depots skip). Wiring: pass `manifest_cache_dir=settings.steam_manifest_cache_dir` into **both** `DepotDownloaderManifestFetcher` constructions in `src/orchestrator/agent/app.py`.

### Piece 3 — recently-purchased persist into the selection (control plane)
`src/orchestrator/scheduler/jobs.py::auto_classify_block` already reconciles `selectedAppsToPrefill.json` via `agent_client.prune_steam_selection(exclude_ids, restore_ids)`. Verified: the agent's `reconcile_selection` (`selection_file.py`) treats `restore_ids` as **"ensure present"** — `to_add = restore - cur; new = (cur - to_remove) | restore` — so `restore_ids` **adds** app_ids even if never previously selected. Therefore Piece 3 is **control-plane-only, no agent/RPC change**: expand the `restore_ids` the reconcile sends to include the set of **prefilled** steam app_ids (from `agent_client.prefilled_apps()`, the `.bin` cache — the same source `library_sync` already uses) that are **not** in `prefill_exclusions(mode='exclude')`. The DB exclude set stays the source of truth, so an operator deselect (→ DB `exclude`) is honored and not re-added (`exclude - restore` still removes it because it won't be in the prefilled-restore set once excluded). Best-effort, never raises, converges each 6h tick. Net: a game the host `--recently-purchased` cron downloads appears in the `.bin` cache → next reconcile tick adds it to the selection → it shows checked in `--select-apps` and stays in the durable prefill set.

## Data flow (buy a game → fully covered, no manual steps)
```
Purchase → host 'SteamPrefill prefill --recently-purchased' (midnight) caches it through lancache + writes its .bin
  → library_sync (6h): prefilled_apps() sees the new .bin → upserts games row (title, status defaults 'unknown')
  → auto_classify_block reconcile (6h) [PIECE 3]: adds the prefilled-non-excluded app_id to restore_ids
     → agent reconcile_selection adds it to selectedAppsToPrefill.json (now checked in --select-apps)
  → gated validation_sweep (6h) [PIECE 1]: enumerates the 'unknown' owned row → enqueues validate
     → validate reads the on-disk .bin → status flips to up_to_date (or validation_failed if partial)
  → fetch_manifests (weekly) [PIECE 2]: app now has a .bin but no matching .shas → fetches → durable .shas written
```

## Error handling
- Piece 1: uncovered `unknown` rows → `validate` `outcome='error'`, status untouched (bounded to owned rows; ~a handful today). No cascade — `enqueue_scheduled_prefill` is Epic-only, so a steam `validation_failed` never triggers a prefill.
- Piece 2: DepotDownloader logon reuses the persisted IsolatedStorage session (no 2FA); transient failures ride the existing `_TRANSIENT_RE` backoff; first-run burst bounded by the "no matching `.shas`" subset + `delay_sec`.
- Piece 3: best-effort inside `auto_classify_block` (already `try/except`, never raises); a failed prune retries next tick; `prefilled_apps()` unreachable → skip, no selection change.

## Testing (TDD, test-first per piece)
- **Piece 1** (`tests/jobs/test_sweep_handler.py`): the gated sweep enumerates a `status='unknown', owned=1` steam game (and does NOT enumerate a not-owned `unknown` row); an uncovered `unknown` game's `outcome='error'` does not clobber its status. Existing gated/full sweep tests stay green.
- **Piece 2** (`tests/…/test_manifest_fetcher.py`): app_ids present in the manifest cache but absent from `selectedAppsToPrefill.json` (e.g. Raft) ARE enumerated; app_ids already covered by a matching-gid `.shas` are skipped; selection-only apps still enumerated. `tests/agent/test_import_isolation.py` stays green (glob inlined). Fetcher-construction test in `agent/app.py` wiring.
- **Piece 3** (`tests/scheduler/test_jobs.py` or the auto_classify_block test): `restore_ids` sent to `prune_steam_selection` includes a prefilled-non-excluded app_id and EXCLUDES a `prefill_exclusions(mode='exclude')` app_id; never raises when `prefilled_apps()` errors.
- Full suite `pytest -q --ignore=tests/scripts`; mypy + ruff clean.

## Non-goals
- No `games.metadata`/`current_version` population for steam (vestigial).
- No near-immediate `library_sync` validate-enqueue (Karl chose ≤6h).
- No `fetch_manifests` cadence change.
- No host-cron / `selectedAppsToPrefill.json` host-side edits, no new agent selection RPC (`restore_ids` already adds).
- No schema/migration change (no new columns).

## Deploy + live verification (no 2FA)
- Control plane (Pieces 1+3) → LXC 1105: `git reset --hard origin/main; docker build -t orchestrator:dpa .; bash /root/deploy-orchestrator-lxc.sh` (build first — the script only recreates). Tag a rollback image first.
- Agent (Piece 2) → UGREEN: rebuild + **recreate** the agent container (not restart — env reload).
- Verify: within one 6h gated sweep, `games.status` for 648800 → `up_to_date`; after the reconcile tick, `648800` appears in `selectedAppsToPrefill.json`; the next weekly `fetch_manifests` writes a `648800_*_648801_*.shas` into the archive volume. Confirm no regression on the ~8 legacy NULL-metadata `unknown` rows (validate or stay `unknown` via non-clobbering error). Confirm `validation_sweep` + `fetch_manifests` scheduled jobs are enabled live (defaults True).
