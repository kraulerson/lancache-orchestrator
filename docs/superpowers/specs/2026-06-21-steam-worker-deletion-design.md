# Roadmap ③ — Delete the Legacy ValvePython Steam Worker — Design

**Date:** 2026-06-21
**Status:** Approved (design)
**Repo:** lancache_orchestrator. **Branch:** `feat/steam-worker-deletion`
**Parent:** the re-architecture north-star (`docs/superpowers/specs/2026-06-19-re-architecture-design.md`). This is roadmap **step ③**.
**Predecessors:** ① (Steam prefill+auth → SteamPrefill, PR #175), ② (data-plane agent, PR #177/#178 — full flip live: steam prefill + epic prefill + validate all route through the agent).

> Scope: retire the entire legacy `ValvePython/steam 1.4.4` gevent-subprocess worker. The orchestrator becomes a pure consumer of SteamPrefill: Steam validate is sourced from SteamPrefill's manifest cache (parsed in dependency-free Python by the agent), library enumeration from SteamPrefill, and Steam auth is already SteamPrefill's. **Invariant: validate (F7 / live Game_shelf cache badges) never breaks** — each capability's replacement is proven behind a flag before the worker use is removed; the worker is deleted only after its last use is gone.

---

## 0. Gating spike — PASSED (2026-06-21)

The whole direction hinged on parsing SteamPrefill's manifest cache in Python. **Proven live:** a ~40-line dependency-free protobuf walk over SteamPrefill's `.bin` files yields **byte-identical chunk-SHA sets** to the ValvePython worker. Tested on 4 depots (52 / 51,192 / 92,000 / 163,174 chunks) — all `SETS EQUAL: True`, zero diff either way. Format documented in `[[reference_steamprefill_manifest_format]]`:

- File: `~/.cache/SteamPrefill/v1/{appId}_{appId}_{depotId}_{manifestGid}.bin` (root cron → `/root/.cache/...`).
- protobuf-net of SteamPrefill's `Manifest`: `[1]` repeated `FileData`; `FileData [1]` repeated `ChunkData`; `ChunkData [1]` = lowercase-hex chunk SHA1 string, `[2]` compressed length. Top-level `[4]` = depot_id, `[2]` = manifest gid.
- Chunk SHAs = walk field-1 → field-1 → field-1 (the hex string), dedup. The `.bin` is **not** zstd-compressed (raw protobuf-net); the legacy DB `manifests.raw` IS zstd(ValvePython-protobuf) — different formats.

## 1. Architecture — worker capabilities re-homed

The worker owns FOUR things today; ③ re-homes all four, then deletes the worker:

| Capability | Today (worker) | After ③ |
|---|---|---|
| `manifest_expand` | gevent subprocess: zstd(DB blob) → `DepotManifest` → chunk SHAs (gates every validate/sweep) | **Agent** parses SteamPrefill `.bin` (pure-Python) |
| `manifest_fetch` | worker `CDNClient` online fetch → DB `manifests.raw` | **DELETED** — manifests exist as a side effect of SteamPrefill prefilling |
| `library_enumerate` | worker owned-apps (scheduler auto-enqueues) | **`SteamPrefillDriver.list_owned`** |
| auth flow | worker `auth_begin/complete/status` (OQ2 + 2FA) | **DELETED** — SteamPrefill owns Steam auth (`account.config`, already on `/health.steam_auth_ok`) |

**The core: `POST /v1/steam/validate {app_id}` on the agent.** SteamPrefill's manifests AND the cache files both live on the lancache host (agent side), so the agent owns Steam validate end-to-end. Given `app_id`:
1. Find the app's current per-depot manifests: cross-reference `successfullyDownloadedDepots.json[app]` (the gids SteamPrefill actually prefilled) against the `.bin` filenames (`{app}_{app}_{depot}_{gid}.bin`).
2. Parse each `.bin` → dedup'd chunk SHAs (the proven walk).
3. Compute cache keys (`identifier="steam"`, slice_range from `cache_slice_size_bytes`, `cache_levels`, `cache_root` — all agent settings) and disk-stat the cache (reuse `validate_chunks`).
4. Return `{chunks_total, chunks_cached, chunks_missing, outcome, versions}` (the existing `ValidationResult` shape).

The control plane's `validate_game` (Steam path) collapses to: call `agent_client.steam_validate(app_id)`, record the `ValidationResult`. No worker, no DB manifest read, no control-side cache-key compute for Steam. Epic validate is unchanged (its deferred `verify_cached` path).

This is the natural endpoint of the control/data split: the data plane owns everything touching the manifest cache + disk; the control plane decides *which* games to validate and records outcomes.

## 2. The other three capabilities + the DB

**`library_enumerate` → `SteamPrefillDriver.list_owned`.** SteamPrefill knows the owned-app set. Exact source is a **plan-time recon** — candidates: `select-apps` query output, an owned-apps cache file, or SteamPrefill's app-info cache. The driver gains `list_owned() -> list[OwnedApp{app_id, name}]`; the Steam `library_sync` handler calls it instead of the worker and upserts `games` exactly as today. **Fallback (flagged):** if SteamPrefill can only enumerate prefilled apps (not all owned), enumerate from `successfullyDownloadedDepots.json` — which covers Karl's cron-prefilled library.

**`manifest_fetch` → deleted.** The handler, its API route (`POST /api/v1/games/{id}/manifest/fetch`), and the CLI `manifest/fetch` command are removed. Manifests exist as a side effect of SteamPrefill prefilling — no separate fetch op.

**Steam auth flow → deleted.** `auth_begin/auth_complete/auth_status` in `api/routers/auth.py` (the worker-backed OQ2 credential-intake + 2FA) are removed — SteamPrefill owns Steam auth. Epic auth (separate router) is untouched. Plan confirms nothing else depends on the worker's session metadata.

**`manifests` table → slimmed.** Validate no longer reads `manifests.raw`. A migration drops the `raw` BLOB column; the table keeps `game_id, depot_id, version, chunk_count, total_bytes` as lightweight metadata, repopulated from the agent's validate parse (for the games/Game_shelf display). Dropping the whole table is possible but Game_shelf may surface chunk counts, so slim-metadata is the safer choice.

## 3. Phasing — three dependent phases, validate live throughout

The worker can only be deleted once all four uses are gone. Each phase is its own PR with its own flag + live flip (mirroring ②); the worker stays until its last use is proven gone.

**Phase ③a — Agent Steam-validate (gating).**
- Agent: `POST /v1/steam/validate {app_id}` (manifest-finder + parser + cache-key + stat) + `AgentClient.steam_validate`.
- Control: rewire `validate_game` Steam path → `agent_client.steam_validate`, behind `steam_validate_via_agent` (default False → existing worker path; True → agent). Flag-off = byte-identical equivalence net.
- Live cutover: flip the flag, validate a known game, confirm counts match the worker baseline (the spike already proved SHA-equivalence; this proves the end-to-end stat).
- After: `manifest_expand` unused. Worker still present.

**Phase ③b — Library enumerate via SteamPrefill.**
- `SteamPrefillDriver.list_owned` (+ the plan-time recon on its source) + rewire the Steam `library_sync` handler, behind `steam_enumerate_via_prefill`.
- Live: run a library_sync, confirm `games` populates equivalently.
- After: `library_enumerate` unused.

**Phase ③c — Delete the worker stack.** Once ③a + ③b are flipped on and stable, nothing calls the worker. Delete:
- **Code:** `platform/steam/{worker,client,protocol,session,enumerate}.py`; the `manifest_fetch` handler + its API route + CLI command; the Steam `auth_begin/complete/status` endpoints; `Deps.steam_client`; the `api/main.py` worker construct/start/stop/singleton + `set_steam_client_singleton`.
- **Build/deps:** `requirements-steam-worker.{in,txt}`; the Dockerfile `venv-steam-worker` stage (lines ~24-28, 53); settings `steam_worker_python_path` + `steam_session_dir`.
- **DB:** migration dropping `manifests.raw` (slim the table per §2).
- The `steam_validate_via_agent` + `steam_enumerate_via_prefill` flags collapse to unconditional (remove the old paths).

**Deploy:** ③a/③b rebuild the agent image (it already has the SteamPrefill + cache mounts). ③c's deletion shrinks the image (drops the entire `venv-steam-worker` — the ValvePython/gevent tree).

## 4. Error handling / edge cases

- **App with no SteamPrefill manifest** (not prefilled yet): `/v1/steam/validate` returns an `outcome="error"` / `chunks_total=0` with a clear reason (`no_manifest_in_cache`) — validate_game records it as today's "no manifests" error, not a crash.
- **Stale gid:** the agent picks the gid from `successfullyDownloadedDepots.json` (what SteamPrefill last prefilled). If a `.bin` for that gid is missing (cache cleared), fall back to the newest `.bin` for the depot, or report `manifest_missing`.
- **Multi-depot apps:** dedup chunk SHAs across all depots of the app (matching today's per-depot loop + global dedup in `validate_game`).
- **Parser robustness:** the protobuf walk tolerates unknown fields (skips non-field-1 / non-length-delimited); a malformed `.bin` → `error` for that app, not a crash. Validate input `app_id` as a non-negative int at the agent boundary.
- **Agent unreachable** (flag-on): `validate_game` records `last_error` + the job fails cleanly (existing `AgentError` handling), never a crash-loop.

## 5. Testing

TDD throughout; existing validate tests are the equivalence net.

- **Agent parser** (new, `tests/agent/`): the protobuf walk against a committed sample `.bin` fixture → expected chunk-SHA set; multi-depot dedup; malformed `.bin` → error; unknown-field tolerance.
- **Agent `/v1/steam/validate`**: against a temp cache tree + a sample `.bin` + a sample `successfullyDownloadedDepots.json` — asserts cached/missing/outcome; `no_manifest_in_cache` path; bad `app_id` → 400.
- **`AgentClient.steam_validate`**: MockTransport — result mapping + typed `AgentError`.
- **Equivalence (the headline)**: `validate_game` flag-off = existing worker tests pass unchanged; flag-on + mocked `AgentClient` → same `ValidationResult` + same DB writes.
- **`list_owned` + library_sync** (③b): driver parses a sample owned-apps source; handler upserts `games` equivalently (flag-off unchanged).
- **Deletion (③c)**: the suite still passes with the worker stack gone; assert the removed routes (`manifest/fetch`, steam `auth/*`) 404; the image builds without `venv-steam-worker`.
- **Live (operator-collaborative, per phase):** ③a flip + validate a known game (counts vs worker baseline); ③b flip + library_sync; ③c post-deploy smoke (validate + library_sync + prefill all green, no worker process).

## 6. Scope / YAGNI

**In scope:** the agent `/v1/steam/validate` (parser + cache-key + stat) + `AgentClient.steam_validate`; `validate_game` Steam rewire; `SteamPrefillDriver.list_owned` + library_sync rewire; deletion of the worker stack, `manifest_fetch`, Steam auth flow, the worker venv + requirements; the `manifests.raw` drop migration; per-phase flags + live flips.

**Out of scope / deferred:**
- **Epic** — entirely untouched (its validate is the deferred `verify_cached`; its prefill/auth already modern).
- **A control-plane in-process ValvePython parser** for the legacy zstd DB blobs — not needed; validate sources from SteamPrefill, and the `raw` column is dropped. (The spike confirmed `steam.core.manifest` imports gevent-free, but we don't use it.)
- **Re-fetching/migrating existing DB manifest blobs** — not migrated; they're dropped. Validate re-derives from SteamPrefill's cache.
- **The ④ LXC move** — separate roadmap step; ③ keeps everything on the UGREEN.
- **Steam credential intake UI** — the deleted auth flow is not replaced (SteamPrefill owns auth; re-auth is Karl running SteamPrefill, as today).

**Invariant restated:** validate (live Game_shelf badges) works at every phase boundary — flag-off is byte-identical, flag-on is proven before the worker use is removed, and the worker is deleted only after its last use is gone.
