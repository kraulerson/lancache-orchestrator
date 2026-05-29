# F7 Cache Validator (disk-stat) — Design Spec

**Date:** 2026-05-28
**Feature:** F7 — the orchestrator's core value proposition. Determine
whether a Steam game's depot-manifest chunks are present in the lancache
on-disk cache, by computing each chunk's nginx cache path and `os.stat()`-ing
it. Records a `validation_history` row and updates `games.status`.

**Spike:** `spikes/spike_a4_lancache_cache_key.md` — cache-key formula
empirically verified against the live lancache. **This spec follows the
verified formula, which corrects two FRD errors (10 MiB slice not 1 MiB;
levels path ordering `<H[30:32]>/<H[28:30]>/<H>`).**

---

## 1. Scope

### In (MVP cutline)

- A `validate` job kind handler (`jobs.kind='validate'` already exists in
  migration 0001) that validates one game's current manifest set.
- Manual trigger: `POST /api/v1/games/{game_id}/validate` (bearer-gated,
  in-flight dedup, returns 202 + job_id).
- Cache-key derivation: pure, offline, deterministic
  (`steam` + uri + `bytes=0-{slice-1}` → md5 → `levels` path).
- Manifest BLOB expansion in the worker venv (`manifest.expand` IPC op),
  returning `{depot_id, chunk_shas}` — **no Steam session required**.
- Batched `os.stat()` over the read-only cache mount via
  `loop.run_in_executor`.
- `validation_history` row per run; `games.status` + `last_validated_at`
  update.
- Startup self-test gating `health.validator_healthy`.
- A `depot_id` column on `manifests` (migration 0003) + BL12 handler update
  to populate it (F7 needs depot_id to build chunk URLs and pick the
  latest manifest per depot).

### Out (deferred)

- Auto-trigger on prefill completion (ID5) — lands with prefill (F5/F6).
- Full-library validation sweep (F13/BL13).
- HEAD-probe / mixed validation methods (`validation_history.method`
  enum reserves them; F7 only writes `disk_stat`).
- Deep byte-level collision check (read first KiB, confirm embedded
  `KEY:`); off by default, post-MVP.
- Epic validation (cacheidentifier = `$http_host`); F7 is Steam-only for
  the F1 cutline. Non-steam games → 400 at the endpoint, error at handler.

---

## 2. Architecture & components

Five units, each independently testable:

1. **`src/orchestrator/validator/cache_key.py`** — pure functions:
   - `steam_chunk_uri(depot_id: int, sha_hex: str) -> str`
   - `cache_key(identifier: str, uri: str, slice_range: str) -> str` (md5 hex)
   - `slice_range_zero(slice_size: int) -> str` → `bytes=0-{slice-1}`
   - `cache_path(cache_root: Path, h: str, levels: str) -> Path` — general
     nginx `levels` algorithm (consume hex from the end).
   - Input validation: `depot_id >= 0`, `sha_hex` matches `^[0-9a-f]{40}$`;
     raise `ValueError` otherwise (path-traversal guard).
   No I/O, no settings import — caller passes config in. Trivial to unit-test.

2. **`src/orchestrator/validator/disk_stat.py`** — the validator engine:
   - `async def validate_chunks(paths: list[Path], *, batch_size=256) ->
     tuple[int, int]` → `(cached, missing)`. Stats in batches via
     `run_in_executor`; "cached" = exists AND `st_size > 0`.
   - `async def validate_game(pool, deps, game_id, settings) ->
     ValidationResult` — orchestrates: load latest manifests per depot,
     expand via worker, dedup SHAs, derive paths, stat, tally, classify.
   - `ValidationResult` dataclass: `chunks_total, chunks_cached,
     chunks_missing, outcome, manifest_version, error`.
   - Raises a mount error if `lancache_nginx_cache_path` is not an
     accessible directory → outcome `error`.

3. **Worker IPC `manifest.expand`** — `platform/steam/worker.py`
   `_handle_manifest_expand(msg_id, params)`:
   - params: `{raw_b64}` (the stored `base64(zstd(protobuf))` BLOB).
   - decompress zstd → `DepotManifest(data)` → iterate
     `payload.mappings[*].chunks[*].sha` → hex; collect unique.
   - returns `{depot_id: int, chunk_shas: [hex, ...]}`.
   - Offline (no CDNClient, no auth). Registered in `_HANDLERS`.
   - `client.manifest_expand(raw_bytes)` on `SteamWorkerClient` +
     `manifest.expand` per-op timeout (default 120 s).

4. **`src/orchestrator/jobs/handlers/validate.py`** —
   `async def validate_handler(job, deps)`:
   - non-steam platform / missing game → `ValueError`.
   - calls `validate_game(...)`, writes `validation_history`, updates
     `games.status` + `last_validated_at`. Registered in
     `handlers/__init__.py` as `register("validate", validate_handler)`.

5. **`src/orchestrator/api/routers/validate_trigger.py`** —
   `POST /api/v1/games/{game_id}/validate`: 404 unknown, 400 non-steam,
   in-flight dedup (queued/running `validate` for game → existing job_id),
   202 + job_id, 503 on `PoolError`. Mirrors the BL12 manifest trigger.

Plus: **migration 0003** (manifests.depot_id), **BL12 handler update**,
**health/lifespan wiring** for the self-test.

---

## 3. Data flow (a validate run)

```
POST /api/v1/games/{id}/validate
  → INSERT jobs(kind='validate', game_id, platform='steam', state='queued', source='api')
  → 202 {job_id}

worker_loop claims job → validate_handler(job, deps)
  → validate_game(pool, deps, game_id, settings):
      latest manifest row per depot_id for game  (SELECT … GROUP BY depot_id, max fetched_at)
      for each manifest row:
        deps.steam_client.manifest_expand(row.raw)  → {depot_id, chunk_shas}
        uri = /depot/{depot_id}/chunk/{sha}     (per unique sha)
        h = md5("steam" + uri + "bytes=0-10485759")
        path = cache_root/h[-2:]/h[-4:-2]/h
      dedup all (depot_id, sha) pairs → path list
      (cached, missing) = validate_chunks(paths)
      outcome = cached==total→'cached'; 0<cached<total→'partial'; cached==0→'missing'
  → INSERT validation_history(game_id, manifest_version, started_at, finished_at,
        method='disk_stat', chunks_total, chunks_cached, chunks_missing, outcome, error)
  → UPDATE games SET status=…, last_validated_at=CURRENT_TIMESTAMP WHERE id=game_id
  → mark job succeeded
```

`manifest_version` recorded on the run: the comma-joined sorted set of
depot manifest gids validated (e.g. `"529345:123…,529346:456…"` →
simplified to a stable representative string). One `validation_history`
row per job ("one row per F7 run").

### games.status transitions (enum: unknown/not_downloaded/up_to_date/
pending_update/downloading/validation_failed/blocked/failed)

| outcome | games.status |
|---|---|
| cached  | `up_to_date` |
| partial | `validation_failed` |
| missing | `validation_failed` |
| error   | unchanged (infra failure must not clobber real state) |

`blocked` games are never validated (skip + 400/handler-skip); `error`
leaves status as-is.

### No manifests for the game

If the game has zero manifest rows, the run is outcome `error` with
`error="no manifests; run manifest fetch first"`, `chunks_total=0`. Job
still succeeds (it ran correctly); status unchanged.

---

## 4. Startup self-test → `health.validator_healthy`

Goal: catch deployment misconfiguration (cache not mounted / wrong path /
unreadable) before accepting validations.

- At lifespan startup, `validator_self_test(settings)`:
  1. `lancache_nginx_cache_path` exists, is a directory, and is listable
     (read access). Fail → `validator_healthy=False`.
  2. Derive a sample path from a synthetic key (exercise `cache_key.py`
     end-to-end without I/O); any exception → `False`.
  3. **Best-effort deep check (logged, non-gating):** if a manifest BLOB
     exists, expand one and stat its first chunk; log hit/miss. A miss
     does NOT flip healthy false (the chunk may legitimately be uncached)
     — only an exception/mount error does.
- Result stored on `app.state.validator_healthy` (bool). `/health` reads
  it via `getattr(app.state, "validator_healthy", False)` (test fixtures
  without lifespan → False, matching the existing pattern).
- `validator_healthy=False` ⇒ `/health` 503 (joins the existing
  `all_healthy` conjunction alongside pool/scheduler/lancache/cache_volume).

This is distinct from `cache_volume_mounted` (which is a pure
`Path.is_dir()` snapshot evaluated per request); `validator_healthy`
reflects the one-time startup self-test of the validator subsystem.

---

## 5. Locked decisions

- **D1 — verified cache-key formula** (spike A4), not the FRD. 10 MiB
  slice; path `<H[-2:]>/<H[-4:-2]>/<H>`; identifier `steam`; present =
  exists AND size>0.
- **D2 — manifests.depot_id (migration 0003)** + BL12 handler populates
  it. Nullable INTEGER (no backfill; STRICT-safe add). New index
  `idx_manifests_game_depot ON manifests(game_id, depot_id, fetched_at DESC)`.
- **D3 — latest manifest per depot:** validate the row with max
  `fetched_at` (tie-break max `id`) for each `depot_id` of the game.
  Historical rows ignored.
- **D4 — deserialize in worker** (`manifest.expand`), offline, ADR-0013
  D14. Orchestrator never parses protobuf.
- **D5 — dedup chunk SHAs** per depot; `chunks_total` = count of unique
  (depot_id, sha) pairs.
- **D6 — batched stat** (256) via `run_in_executor`; never block the loop.
- **D7 — status mapping** per §3; `error` never clobbers status.
- **D8 — path-traversal guard:** validate `depot_id`/`sha_hex` shape;
  confirm resolved path stays under cache root.
- **D9 — config from Settings:** `lancache_nginx_cache_path`,
  `cache_slice_size_bytes`, `cache_levels` (all already exist with correct
  defaults). Add `steam_cache_identifier: str = "steam"` and
  `steam_worker_manifest_expand_timeout_sec: int = 120` (30..600).
- **D10 — Steam-only**; non-steam → 400 / handler ValueError. Epic
  deferred.
- **D11 — in-flight dedup** at the trigger (race-tolerant; handler is
  safe to run twice — it only reads cache + writes a history row).

---

## 6. Error handling

- Cache mount missing/unreadable mid-run → outcome `error`, error string
  truncated `[:200]`, job succeeds, status unchanged, WARN log.
- Worker `manifest.expand` failure (IPCTimeout/WorkerDied) → propagates;
  worker loop marks job `failed`. (Not auth-related — expand needs no
  session.)
- Malformed manifest BLOB (decompress/parse fails in worker) → worker
  returns an error kind; handler records outcome `error`.
- Individual `os.stat` raising (e.g. EACCES on one file) → counted as
  missing for that path, WARN once per run with the count (don't fail the
  whole run on a single unreadable file).
- All structured logs: `validate.started/expanded/stat_done/recorded`,
  `validate.mount_error`, `validate.no_manifests`. No secrets, no chunk
  bytes.

---

## 7. Testing strategy (test-first)

- **cache_key.py (pure):** golden vectors from spike A4 — the three real
  (uri → md5 → path) triples MUST reproduce exactly. Levels generalization
  (1:2, 2:2, 1:1:1). Input-validation rejections (bad sha, negative depot).
- **disk_stat.py:** tmp_path cache tree; create files (some empty, some
  non-empty, some absent); assert (cached, missing) and outcome
  classification; batch boundary (>256 paths); mount-missing → error.
- **manifest.expand worker op:** unit test with a stub
  decompress/DepotManifest (the worker IPC handler in isolation, like
  BL12's worker tests) — round-trips a fake serialized payload to
  `{depot_id, chunk_shas}`; dedup verified.
- **validate_handler:** stub `steam_client.manifest_expand`; seed manifest
  rows + a tmp cache tree; assert validation_history row + games.status
  transition for cached/partial/missing/error/no-manifests.
- **validate_trigger router:** 202 queue, dedup, 404, 400 non-steam, auth
  401, 503 PoolError (mirror BL12 trigger tests).
- **self-test + health:** validator_healthy true/false wiring; /health 503
  when false; lifespan integration.
- **settings:** new fields' defaults + bounds.
- **migration 0003:** runner applies it; depot_id present + nullable;
  CHECKSUMS regenerated; index exists.

Target: full suite green (currently 874 + ~50 new ≈ 920+).

---

## 8. Security review focus (Phase 2.4)

- **Path traversal** via depot_id/sha into a filesystem path — primary
  threat; mitigated by shape validation + under-root resolution (D8).
- **Untrusted protobuf deserialization** confined to the worker venv
  (ADR-0013); orchestrator only handles ints/hex strings returned over IPC.
- **No new auth surface** beyond the bearer-gated trigger (reuses
  middleware). Validation needs no Steam session.
- **Resource use:** batched stat bounded; chunk count bounded by the
  manifest (operator's own owned game). DoS not attacker-reachable.

---

## 9. Out-of-scope confirmations

No CLI command (that's a separate cutline item). No prefill auto-trigger.
No sweep. No Epic. No HEAD-probe. No deep byte check. These keep F7 to a
single, reviewable build loop.
