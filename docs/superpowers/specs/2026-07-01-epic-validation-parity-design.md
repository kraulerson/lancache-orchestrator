# Epic Cache-Validation Parity — Design

**Date:** 2026-07-01
**Status:** Approved (design)
**Goal:** Give Epic games the same per-chunk, on-disk validation Steam's F7 validator provides — real "is the whole game actually cached?" answers, `validate-all` sweep coverage, and true `Partial · N%` badges — with the mechanism adapted to Epic's CDN/manifest model.

<!-- Last Updated: 2026-07-01 -->

## Problem (traced in the code, 2026-07-01)

Steam has a real disk-stat validator (F7): it reads the manifest, computes each chunk's lancache cache-key → on-disk path, `stat`s them, and classifies `cached / partial / missing`. Epic has **none of this**:

- `jobs/handlers/validate.py::validate_handler` **hard-raises `ValueError` for any non-steam platform**; `jobs/handlers/sweep.py` selects `WHERE platform='steam'`. So Epic games never validate and never enter a sweep.
- Epic's only check is `prefill/epic_downloader.py::verify_cached` — a **sample-based network re-request** counting `X-Upstream-Cache-Status: HIT`. It re-hits the network, samples rather than checks every chunk, and doesn't touch disk. It answers "does a sample seem cached?", not "is the whole game on disk?".

**Epic is NOT missing manifest data** (unlike Steam's gap). Epic fetches the manifest fresh at prefill and stores it in the `manifests` table (`raw` bytes + `chunk_count` + `total_bytes`, per `game_id`+`version`; see `jobs/handlers/prefill.py::_EPIC_MANIFEST_UPSERT`). Every prefilled Epic game already has its complete chunk list persisted — no DepotDownloader-equivalent fetcher is needed. The gap is purely the **absence of a disk-stat validator** plus one missing piece of data (below).

## Spike-proven facts (live, against real cached Epic chunks on 2026-07-01)

The lancache access log (`/lancache/lancache/logs/access.log`) shows Epic content cached under **two identifiers**: `epicgames` (host `epicgames-download1.akamaized.net`) and `egs-cloudfront-chunks.epicgamescdn.com` (host `egs-cloudfront-chunks.epicgamescdn.com`) — the same chunk is often cached under **both**. Taking one **HIT** chunk and computing md5 candidates against `/data/cache`:

- **`md5(identifier + uri + "bytes=0-10485759")` EXISTS on disk** for identifier `= "egs-cloudfront-chunks.epicgamescdn.com"` AND for `= "epicgames"`.
- Every candidate without the slice, or without the identifier, missed.

**Therefore the Epic cache-key is `md5(identifier + uri + slice)` — the exact same structure as Steam** (`validator/cache_key.py::cache_key`), same 10 MiB `slice_range_zero`, same `H[-2:]/H[-4:-2]/H` disk layout (`cache_path`). Three consequences:

1. **`uri` = `cdn_base` + `chunk_path`** (`epic_chunk_uri(chunk_path, cdn_base)` already computes this). `chunk_path` derives from the stored manifest (`platform/epic/manifest.py::chunk_path(chunk, version)`). `cdn_base` (`/Builds/Org/{catalogId}/{buildId}/default`) is **stable per game version** — only the signed *query string* is short-lived, and lancache strips it — but it is **not persisted today** (set on the `EpicManifest` at fetch time, dropped after prefill).
2. **`identifier` varies by CDN host** (`epicgames` or the hostname). Content is often cached under multiple identifiers, so validation checks a small known set and counts a chunk **present if it exists under ANY identifier**.
3. **No Epic auth and no manifest re-fetch are needed at validate time** — everything derives from the stored manifest plus a stored `cdn_base`.

## Goal / Non-goals

**Goal:** an Epic disk-stat validator that produces the *same* `cached / partial / missing` outcome, `validation_history` rows, and `games.status` + `chunks_cached/chunks_total` updates as Steam; Epic joins the `validate-all` sweep; Game_shelf badges/Repair light up for Epic automatically.

**Non-goals:**
- Replacing Epic prefill or its manifest storage (unchanged — it already stores what we need).
- Keeping `verify_cached` as the validation source (it stays as a prefill-time smoke check, not the validator).
- An Epic manifest *fetcher* (unneeded — the manifest is already stored).
- Multi-slice chunk handling: Epic chunks are ≤ one 10 MiB slice (window-sized, ~1 MB observed), so a chunk lives entirely in slice 0 — same assumption Steam's `slice_range_zero` documents. (Guarded: a chunk whose `window_size > slice_size` is a spike-flagged edge case, see Phase 0.)

## Architecture

Validation must disk-`stat` `/data/cache`, which only the **agent** (on the UGREEN host) can reach — the control plane (LXC) has no cache mount. But unlike Steam (whose manifest lives on the agent's SteamPrefill cache), the **Epic manifest lives in the control-plane DB**. So the control plane reads the stored manifest and hands it to the agent, which parses + computes keys + stats — mirroring `steam_validate`'s agent-side compute, with the manifest passed in rather than read locally.

### Component A — agent Epic validator (`agent/routers/epic.py`, new)

`POST /v1/epic/validate` (bearer-gated), body `{app_id, version, cdn_base, raw_manifest_b64}`:
- `parse_manifest(base64-decode(raw_manifest_b64))` → chunk list + version.
- For each chunk: `uri = epic_chunk_uri(chunk_path(chunk, version), cdn_base)`; for each identifier in `settings.epic_cache_identifiers`, `h = cache_key(identifier, uri, slice_range_zero(cache_slice_size_bytes))`, `p = cache_path(lancache_nginx_cache_path, h, cache_levels)`; the chunk is **present if any identifier's `p` exists** (short-circuit on first hit).
- Return `{chunks_total, chunks_cached, chunks_missing, outcome, versions, error}` — the **same shape** `steam_validate` returns, so the control side is symmetric. `outcome` via a shared `_classify(total, cached)`.
- Disk stats run through the existing bounded cache-stat executor (`validator/disk_stat`), not inline.

### Component B — persist `cdn_base` (control plane)

Add `cdn_base TEXT` to the `manifests` table (migration `0010_manifests_cdn_base.sql`). `jobs/handlers/prefill.py`'s Epic branch stores `manifest.cdn_base` in the upsert. `cdn_base` is stable per version, so the stored value is valid for that version's chunks.

**Backfill:** Epic manifests stored *before* this migration have `cdn_base = NULL`. The Epic validator returns `outcome="error", error="no_cdn_base"` for them (unvalidatable until re-prefilled) — the exact analog of Steam's `no_manifest_in_cache`, which the sweep leaves status-unchanged. The nightly Epic prefill re-populates `cdn_base` on its next run, so the gap self-heals; no separate backfill job.

### Component C — un-scope validate + sweep to Epic (control plane)

- `jobs/handlers/validate.py::validate_handler`: dispatch by platform — `steam` → existing path; `epic` → new `validate_one_epic_game(pool, deps, game_id, settings)` which reads the latest manifest row (`raw`, `version`, `cdn_base`) from `manifests`, calls `deps.agent_client.epic_validate(...)`, and records `validation_history` + `games.status` + `chunks_cached/total` via the **same** recording helper Steam uses. A `NULL cdn_base` or missing manifest → `error`, status unchanged (mirrors Steam).
- `jobs/handlers/sweep.py`: the candidate SQL drops the `platform='steam'` restriction (both status-gated and `full` modes) so `epic` games are swept; the per-game validate is already platform-dispatched by (C).
- `clients/agent_client.py`: add `async def epic_validate(self, *, app_id, version, cdn_base, raw_manifest_b64) -> dict[str, Any]` (single POST, no poll — validate is synchronous like `steam_validate`).

### Component D — Epic identifier set (setting)

`settings.epic_cache_identifiers: list[str] = ["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]` (env `ORCH_EPIC_CACHE_IDENTIFIERS`, comma-separated). Configurable so a new Epic CDN host can be added without a code change; Phase 0 confirms the complete set.

### Component E — Game_shelf / API (no change)

`GET /api/v1/games` already surfaces `chunks_cached/chunks_total` + `status` platform-agnostically; Game_shelf's `Partial · N%` badge, Validate poll, and Repair button key off those fields, not the platform. Once the Epic validator writes them, Epic parity in the UI is automatic. (The Repair/force-prefill path already supports Epic via F6.)

## Data flow (steady state)

1. Nightly Epic prefill downloads chunks through lancache + stores the manifest **with `cdn_base`**.
2. The 6h/weekly sweep (now platform-agnostic) enqueues `validate` for each Epic game.
3. `validate_handler` (epic) reads the stored manifest + `cdn_base`, calls the agent's `/v1/epic/validate`.
4. The agent computes each chunk's cache-key across the identifier set, disk-stats `/data/cache`, returns `cached/total` → recorded as `up_to_date` / `validation_failed` (+ `chunks_cached/total`).
5. Game_shelf shows true `Partial · N%` for Epic.

## Phase 0 spike (gates the build; small — the formula is already proven on one chunk)

- **E1 — identifier set + manifest-scale cross-check.** For one real prefilled Epic game: read its stored manifest + `cdn_base` (re-derive `cdn_base` from a fresh prefill or the log if the stored one is NULL pre-migration), compute `uri`+cache-key for every chunk across `epic_cache_identifiers`, disk-stat, and confirm a sensible cached ratio that matches the game's real state (the Epic analog of the Steam 400/400 check). Confirm the **complete** identifier set (grep the access log for all `[identifier]` tags on `ChunksV*`/`.chunk` lines). Flag any chunk with `window_size > cache_slice_size_bytes` (would need multi-slice keys — expected: none).

## Settings (new)

| Setting | Default | Purpose |
|---|---|---|
| `epic_cache_identifiers: list[str]` | `["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]` | lancache cache identifiers Epic content is stored under; a chunk counts present if cached under any |

Migration: `0010_manifests_cdn_base.sql` adds `cdn_base TEXT` (nullable) to `manifests` (+ regenerated CHECKSUMS).

## Error handling

- **No manifest / NULL `cdn_base`** → `outcome="error"`, status unchanged (mirrors Steam `no_manifest_in_cache`); self-heals on re-prefill.
- **Malformed stored manifest** → `parse_manifest` raises `EpicManifestError`; the validator returns `error`, never 500s the sweep (per-game isolation, as Steam).
- **Agent unreachable** → the existing `agent_client` connect-retry + the sweep's per-game isolation apply.
- **Import isolation:** the agent Epic router uses only agent-safe modules (`platform/epic/manifest.py`, `validator/*`); it must not import `orchestrator.api.main` / `orchestrator.db.pool`.
- **Security:** `cdn_base` and CDN host from the manifest are already SSRF/traversal-validated at fetch time (`platform/epic/manifest.py::_HOSTNAME_RE`, `".." in cdn_base` guard). The validator does no network I/O and no auth — it only reads local manifest bytes + stats local files.

## Testing (TDD)

- **Agent Epic validator:** a crafted Epic manifest (reuse the F6 test fixtures) + a temp `/data/cache` with some chunk files present → asserts `cached/partial/missing` counts; present-under-any-identifier (a chunk cached under only the 2nd identifier still counts); NULL/absent → `error`; malformed manifest → `error` not raise; the slice/levels/`cache_path` match Steam's.
- **`cache_key.py`:** `epic_chunk_uri` already tested; add the end-to-end key equivalence for a known chunk (the spike's proven value as a regression fixture).
- **Control:** `validate_handler` dispatches epic → `validate_one_epic_game`; records the same fields; NULL `cdn_base` → error+unchanged. Sweep candidate SQL includes epic (both modes). `agent_client.epic_validate` posts correctly.
- **Settings:** `epic_cache_identifiers` default + env override (comma-split).
- **Migration** applies + checksum verifies; the `manifests` schema gains `cdn_base` with no data loss.
- Agent import-isolation guard stays green.

## Scope

Ships as **1 PR** (`feat/epic-validation-parity`): Phase 0 spike (E1) → agent validator (A) → `cdn_base` persistence + migration (B) → validate/sweep un-scoping (C) → identifier setting (D). Game_shelf needs no change (E). Operator go-live: deploy agent + control, run `cache validate-all` (now platform-agnostic), report the Epic before/after.
