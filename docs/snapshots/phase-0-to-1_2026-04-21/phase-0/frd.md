# Functional Requirements Document — lancache_orchestrator

**Phase:** 0
**Step:** 0.1
**Generated from:** `PROJECT_INTAKE.md` v1.1 + `lancache-orchestrator-brief.md` rev 3
**Track:** Light
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

---

## 1. Product Intent

A fully autonomous Python service running on the DXP4800 NAS alongside Lancache. It owns its own SQLite database, APScheduler cron, per-platform authentication (Steam CM + Epic OAuth), and a FastAPI REST API on port 8765. Its job is to proactively fill the Lancache nginx cache with owned Steam and Epic games, validate cache state by reading the nginx cache directory from disk (never by trusting a flat-file log), and expose current state to operators via CLI, a single-file HTML status page, and a REST API consumed by Game_shelf as the rich UI.

The orchestrator has **zero runtime dependency** on Game_shelf. If Game_shelf is offline, caching continues unchanged. If the orchestrator is offline, Game_shelf's library UI still works — only the cache columns degrade.

---

## 2. Must-Have Features — Expanded

Each feature below is the Intake §4.1 entry, expanded with: a complete business-logic trigger, a complete failure/recovery flow, explicit dependencies on other features, and measurable acceptance criteria.

### 2.0 Canonical-format summary (per `templates/generated/frd.tmpl`)

The expanded specs for F1–F17 below are the authoritative source. This compact table mirrors the template's canonical shape so the Manifesto synthesis step (Step 0.4) and future `check-phase-gate.sh` section checks find a predictable structure.

| # | Feature | Logic Trigger | Failure State | Rationale |
|---|---------|--------------|---------------|-----------|
| F1 | Steam CM Authentication | If no valid session at startup or on `orchestrator-cli auth steam` → prompt for creds + 2FA, persist refresh token | Auth fail → mark platform `expired`, surface on status page, other platforms continue | Required for all Steam operations; owner-of-library credential scope |
| F2 | Epic OAuth Authentication | If no valid session at startup or on `orchestrator-cli auth epic` → exchange auth code, persist tokens, rotate silently with 10-min buffer | Auth fail → mark `expired`, continue operating for Steam | Required for Epic operations; different token lifetime than Steam |
| F3 | Steam Library Enumeration | Every 6h (or on CLI `library sync`) with active Steam session → pull PICS, upsert `games` | API fail → log, retry next cycle; partial → process what returned | Source-of-truth for what Steam games exist to prefill |
| F4 | Epic Library Enumeration | Every 6h (or on CLI `library sync`) with active Epic session → pull EGS assets, resolve titles, upsert | Same pattern as F3; title fallback to `epic:<id>` | Same role as F3 for Epic |
| F5 | Steam CDN Prefill | If `current_version != cached_version` and not blocked → fetch manifest, dedupe chunks, stream-discard through Lancache | 403 expiry → refresh code + retry; 508 → fatal config; other → mark job failed | Core value delivery — actually fills the cache |
| F6 | Epic CDN Prefill | Same trigger as F5 for Epic | Same pattern as F5 + Epic v22 decrypt errors surface | Core value delivery for Epic |
| F7 | Cache Validator (disk-stat) | On prefill completion, on API/CLI request → compute cache keys, `os.stat()` expected paths | Self-test fails → validator unhealthy, `/api/health` 503; mount missing → all validations `error` | Trust anchor — addresses the whole reason for the project (unreliable flat-file trackers) |
| F8 | Block List | On `POST /api/v1/games/*/block` or CLI → insert row; prefill checks table | Idempotent; last-write-wins on concurrent mutation | Operator control over unwanted large downloads |
| F9 | REST API | Long-running FastAPI on :8765 with bearer auth; every non-health endpoint requires token; `POST /platforms/*/auth` additionally 127.0.0.1-only | Missing secret → refuse start; 401 on bad token (timing-safe); 500s include correlation ID | Primary integration surface — Game_shelf, CLI, status page all consume |
| F10 | Status Page | GET `/` serves <20 KB single-file HTML; JS polls API every 2s with browser-side `sessionStorage` token | Color-independent indicators required; API-unreachable banner with Retry | Diagnostic surface when Game_shelf unavailable; colorblind-safe by constraint |
| F11 | CLI (`orchestrator-cli`) | `docker compose exec orchestrator orchestrator-cli <subcommand>` hits local API | API down → exit 2 with guidance; auth mismatch → exit 3 | Auth flows live here; operator surface on DXP4800 |
| F12 | Scheduled Sync Cycle | APScheduler cron fires → Steam then Epic: refresh auth → enumerate → diff → prefill → validate | Reaper cleans mid-flight jobs at startup; scheduler-health in `/api/health` (JQ3) | Autonomy — no human intervention in steady state |
| F13 | Scheduled Full-Library Validation Sweep | Separate APScheduler cron (default Sun 03:00) → F7 validate every cached game in batches of 10 | Skipped when validator unhealthy; warn if sweep >2h; per-game errors don't abort | Mitigates R13 (eviction drift) surfaced by Skeptical PM review |
| F14 | Game_shelf Backend Proxy Routes (in Game_shelf repo) | Express routes at `/api/cache/*` proxy to orchestrator API with injected bearer token | 401 → 502 + log; timeout → 503; `POST /platforms/*/auth` explicitly not proxied | Promoted to MVP by OQ1; satisfies Intake SC #5 |
| F15 | Game_shelf Cache Badge + Cache Panel | React components rendered inline in Library and GameDetail pages; 7 states with color + icon + text label | Offline → badges `—`, mutations disabled; tolerant merging on schema mismatch | Promoted to MVP by OQ1; colorblind-safe display of cache state |
| F16 | Game_shelf Cache Dashboard Page | New `pages/Cache.jsx` with stats, platform auth cards, recent jobs, block-list management | Offline → full-page banner + skeleton sections | Promoted to MVP by OQ1; diagnostic + bulk-action surface |
| F17 | Orchestrator ↔ Game_shelf Graceful Degradation | Bidirectional offline handling with single-shot retry (no storms); `/api/v1/` versioning + health-version in responses | Network partition → each side's local flow unaffected; polling reconciles on reconnect; bearer token never reaches frontend | Promoted to MVP by OQ1; survivability invariant |

---

### F1. Steam CM Authentication

**Trigger.** On container start, and on any operator-invoked `orchestrator-cli auth steam` command.

**Behavior.**
1. On startup, read `/var/lib/orchestrator/steam_session.json`. If the file exists and contains a non-empty `refresh_token`, attempt silent reconnect via `steam-next` `SteamClient.login(refresh_token=...)`.
2. On silent-reconnect success: update `platforms.steam.auth_status = 'ok'`, persist any rotated token back to `steam_session.json`, record `auth_expires_at` from the token payload, and log `steam_auth_refreshed` with correlation ID.
3. On silent-reconnect failure OR missing session file: set `platforms.steam.auth_status = 'expired'` (or `'never'` if no prior auth), log the reason, and **continue startup** — do not crash.
4. On CLI invocation (`orchestrator-cli auth steam`): prompt for username → password → Steam Guard code (prompted only if Steam challenges). Persist refresh token to `steam_session.json` with mode `0600`. Update `platforms.steam.*` accordingly.

**Failure State.**
- **Invalid credentials:** CLI prints "Steam rejected credentials — check username/password." No DB write. Exit code 1.
- **Steam Guard required but not provided:** CLI prompts for the 5-character alphanumeric code. Three wrong attempts → CLI exits with guidance. No DB write.
- **Steam CM unreachable (network):** CLI prints "Cannot reach Steam CM servers." `platforms.steam.auth_status` remains unchanged. Exit code 2.
- **Session file corrupted on startup:** Log `steam_session_corrupted` at ERROR, set `auth_status = 'error'`, continue startup. CLI reconnect required.
- **Surfacing:** Status page shows `auth_status` with color + icon + text label ("Steam: Expired — run `orchestrator-cli auth steam` on DXP4800"). REST API `/api/platforms` returns same.

**Depends on:** F11 (CLI must exist to run `auth steam`). **Prerequisite for:** F3, F5, F12.

**Acceptance.**
- [ ] Given a valid prior session file, container starts and Steam auth_status is `ok` within 30s with no CLI prompt.
- [ ] Given an expired session, startup completes, Epic operations still work, and status page shows actionable Steam reconnect instruction.
- [ ] Given no prior auth, CLI flow completes end-to-end and subsequent restart silent-reconnects.
- [ ] Credentials are NEVER logged, NEVER written to `games`/`platforms`/any other table, and NEVER accessible through the REST API.
- [ ] **2FA type disambiguation (JQ1 resolution 2026-04-20):** if `steam-next` exposes the challenge type in its callback, the CLI prompt displays the specific type ("Steam Guard (mobile authenticator): " or "Steam Guard (email): "). If `steam-next` does not expose the type, the prompt reads "Steam Guard code (check Steam mobile app OR the email Steam just sent): " and the README Security/Operations section documents the distinction. **Verification of steam-next capability is a Phase 1 Step 1.2 deliverable**; the implementation path chosen in Phase 1 binds this acceptance criterion's exact text.

---

### F2. Epic OAuth Authentication

**Trigger.** On container start, and on any operator-invoked `orchestrator-cli auth epic` command.

**Behavior.**
1. On startup, read `/var/lib/orchestrator/epic_session.json`. If `refresh_token` is present and not expired-within-10-min, silently rotate via legendary's `auth_ex_token()`.
2. On rotation success: persist new tokens, set `platforms.epic.auth_status = 'ok'`, record `auth_expires_at`, log `epic_auth_refreshed`.
3. On rotation failure OR missing session: set `auth_status = 'expired'` (or `'never'`), log reason, continue startup.
4. On CLI invocation: print the URL `https://legendary.gl/epiclogin`, prompt for the auth code the user pastes. Exchange via `auth_code()`. Persist tokens to `epic_session.json` mode `0600`.
5. A 10-minute pre-expiry buffer governs rotation — do not wait until the token has literally expired.

**Failure State.**
- **Invalid/expired auth code:** CLI prints "Epic rejected auth code — get a fresh one at https://legendary.gl/epiclogin." Exit code 1.
- **Epic services unreachable:** CLI prints "Cannot reach Epic Games Services." `auth_status` unchanged. Exit code 2.
- **Refresh-token rotation fails mid-operation (during scheduled sync):** Mark `auth_status = 'expired'`, abort the in-flight Epic job, log with correlation ID. Next scheduled sync retries the silent rotation; user sees persistent status until CLI reconnect.
- **Session file corrupted:** Same treatment as F1.
- **Surfacing:** Same as F1.

**Depends on:** F11. **Prerequisite for:** F4, F6, F12.

**Acceptance.**
- [ ] Silent rotation succeeds within 5s of container start given non-expired refresh token.
- [ ] Rotation happens ≥10 min before token expiry (verifiable via logs).
- [ ] Expired Epic auth does not prevent Steam operations from running.
- [ ] Auth code is never stored after exchange; only tokens persist.

---

### F3. Steam Library Enumeration

**Trigger.** (a) APScheduler cron firing (default every 6 hours, `SCHEDULE_CRON` env var configurable); (b) operator invokes `orchestrator-cli library sync --platform steam`; (c) POST to a future internal admin endpoint (not in MVP).

**Behavior.**
1. Verify `platforms.steam.auth_status == 'ok'`. If not, log `steam_library_sync_skipped_auth_expired` and exit this invocation cleanly (other platforms still run).
2. Using the active CM session: call license callback → enumerate owned packages → call PICS `get_product_info(apps=[...], packages=[...])` in batches.
3. For each owned app: extract `common.name` (title), depot list, `manifests.public.gid` per depot. Size estimate from depot sizes.
4. UPSERT into `games`: `(platform='steam', app_id, title, owned=1, current_version=gid, size_bytes, metadata=depot_list_json)`.
5. Any previously-owned app no longer in the response: set `owned=0` (do not delete; keep for history).
6. Update `platforms.steam.last_sync_at = now()`.
7. Log `steam_library_synced` with counts (added, updated, removed-ownership).

**Failure State.**
- **PICS API unavailable / timeout:** Log error, leave `games` untouched, set `platforms.steam.last_error`, retry on next cycle. Partial responses: process what was returned, log warning with count discrepancy.
- **Rate limiting from Steam:** Back off per response hint; if no hint, 60-second fixed backoff, max 3 retries within the cycle, then abort cycle.
- **Authentication expires mid-sync:** Abort cleanly, mark `auth_status='expired'`, log, retry next cycle after reconnect.
- **Title field unexpectedly empty:** Store `'steam:<app_id>'` as title, log `steam_title_missing`, attempt re-fetch next cycle.

**Depends on:** F1 (active Steam session). **Prerequisite for:** F5, F7 (validator needs games to validate), F12.

**Acceptance.**
- [ ] Enumerates full owned library in a single cycle for a ~1500-app account in < 10 min.
- [ ] Does not lose history on ownership revocation (rare, but possible via refunds or account family-share changes).
- [ ] Survives transient PICS failures without corrupting `games` table.
- [ ] Structured log entries emit for cycle start, success, and each failure mode.

---

### F4. Epic Library Enumeration

**Trigger.** Same schedule/sources as F3.

**Behavior.**
1. Verify `platforms.epic.auth_status == 'ok'`.
2. Call EGS `launcher-public-service/assets` — paginate if `next` field present.
3. For each asset, collect `catalogItemId`.
4. Batch-resolve titles via `catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared/namespace/{ns}/bulk/items?id={catalogItemId[,...]}` — send in chunks (Epic caps bulk-resolve at ~50 IDs per request).
5. UPSERT into `games`: `(platform='epic', app_id=catalogItemId, title, owned=1, current_version=buildVersion, size_bytes=None initially)`.
6. Size is computed lazily at manifest-fetch time (F6), not here, because the `assets` endpoint does not include it.
7. Ownership revocation handled as in F3.
8. Update `platforms.epic.last_sync_at`.

**Failure State.**
- **Title-resolution batch returns partial:** UPSERT rows with `title = 'epic:<app_id>'` for misses. Log `epic_title_unresolved` per miss. Retry next cycle.
- **Pagination response malformed:** Process successfully-parsed pages, log `epic_pagination_truncated`, retry next cycle.
- **Token rotation fails mid-sync:** Same as F3 — abort, mark expired, retry next cycle after reconnect.
- **Epic returns 429 (rate limit):** Honor `Retry-After`; else 60-second backoff, max 3 retries.

**Depends on:** F2. **Prerequisite for:** F6, F7, F12.

**Acceptance.**
- [ ] Enumerates full library in < 10 min for a typical library.
- [ ] Titles resolve for ≥95% of owned assets on first sync; remainder resolved within 2 cycles.
- [ ] Codename-only rows (`epic:<id>`) are visible in logs and status page until resolved.

---

### F5. Steam CDN Prefill

**Trigger.** For each game row where `platform='steam'` AND `owned=1` AND `NOT IN block_list` AND (`current_version != cached_version` OR `status IN ('not_downloaded','validation_failed','pending_update')`), scheduled run enqueues a prefill job. Also triggered manually via POST `/api/games/steam/{app_id}/prefill` (respects block list; returns 409 if blocked).

**Behavior.**
1. Transition game `status` → `downloading`. Create `jobs` row with `kind='prefill'`, `state='running'`, `source` per caller.
2. Request manifest via `steam-next` CDN client: `get_manifest(app_id, depot_id, manifest_gid, branch='public')` — returns protobuf manifest with chunk list (SHA + size per chunk).
3. Compute unique chunk set (dedupe by SHA).
4. Acquire per-depot manifest request code; cache with a **4.5-minute TTL** (under the Steam 5-min server-side limit).
5. For each chunk: `httpx.AsyncClient.stream('GET', f'http://lancache/depot/{depot_id}/chunk/{sha_hex}', headers={'Host': cdn_vhost, 'User-Agent': 'Valve/Steam HTTP Client 1.0'})`. Read response body in 64 KiB buffers and **discard** (body is not written to disk by the orchestrator — Lancache caches it).
6. Concurrency bounded by `asyncio.Semaphore(PREFILL_CONCURRENCY)`, default 32.
7. On all chunks complete: update `games.cached_version = current_version`, `games.status = 'up_to_date'`, `games.last_prefilled_at = now()`. Job → `succeeded`.
8. Trigger F7 validation immediately after successful prefill (implicit — not in Intake; see Implicit Dependencies §5).

**Failure State.**
- **Manifest request code expired mid-run (HTTP 403 on chunk GET):** Refresh per-depot code, retry that chunk once. Persistent 403: mark job `failed`, game `status='failed'`, log, next cycle retries.
- **Individual chunk 404 from Lancache/upstream:** Log per-chunk error; retry chunk up to 3 times with exponential backoff (1s, 4s, 16s). Persistent: mark job `failed`, game `status='failed'`.
- **Lancache returns HTTP 508 (loop detection):** **Fatal config error.** Job fails. Set a process-level flag that the status page and `/api/health` surface as "Lancache misconfigured — see `X-LanCache-Processed-By` response header." Subsequent prefills refuse to start until container restart.
- **Network drop / timeout mid-run:** 10-second per-chunk read timeout. Partial-completion: job marked `failed`, retry policy decides chunk-level retries as above. Game `status='failed'`.
- **Steam session expires mid-run:** Abort job cleanly, mark `auth_status='expired'` on platform, job `failed` with reason `'steam_auth_expired'`. Next cycle after CLI reconnect retries.
- **Disk full on cache volume:** Lancache returns 5xx; surfaced same as 508 (fatal-config-ish; operator must free cache space).
- **Concurrent prefills tripping Steam rate limits:** Default `PREFILL_CONCURRENCY=32` is chunk-level; **platform-level prefill concurrency is 1 (one game at a time per platform)** per Brief §3.5. Exceeding causes rate limits → mark job failed with `'steam_rate_limited'`.

**Depends on:** F1, F3, Lancache container reachable on compose network. **Prerequisite for:** none (terminal action).

**Acceptance.**
- [ ] A 50 GB Steam game prefills successfully through Lancache with no disk writes by the orchestrator itself.
- [ ] `X-LanCache-Processed-By` response header verified present on sampled chunk responses (Lancache path confirmed).
- [ ] p99 latency of GET `/api/health` stays under 100 ms during a full-speed prefill (Brief Spike F requirement).
- [ ] Mid-run 403 recovers without job failure.

---

### F6. Epic CDN Prefill

**Trigger.** Analogous to F5: scheduled diff-triggered or manual via API.

**Behavior.**
1. Same state machine as F5.
2. Fetch manifest via vendored `legendary.api.egs.get_cdn_manifest(app_info, platform, disable_https=True)` — `disable_https` forces plain HTTP so Lancache can intercept.
3. Parse binary manifest (`legendary.models.manifest`) or JSON manifest (`legendary.models.json_manifest`). Both produce the same internal chunk list.
4. Pin preferred CDN host from `STEAM_PREFERRED_CDN` env var (default `download.epicgames.com`) via legendary's `preferred_cdn` mechanism — this keeps cache-key locality predictable.
5. Per chunk: `GET http://lancache/Builds/{appId}/CloudDir/ChunksV4/{group}/{hash}_{guid}.chunk` with `Host: download.epicgames.com` (or configured preference). Stream-discard as in F5.
6. Update `games` and `jobs` rows on completion.
7. Trigger F7 post-prefill validation.

**Failure State.** Mirror of F5. Epic-specific:
- **Manifest URL token expired (embedded in manifest URL):** Re-fetch manifest (which re-issues the URL). Retry chunk.
- **v22+ manifest AES-GCM decryption fails:** Surface as `'epic_manifest_decrypt_error'`. Typically indicates manifest format version bump → upstream library update required. Fatal for that manifest; other games proceed.

**Depends on:** F2, F4. **Prerequisite for:** none.

**Acceptance.**
- [ ] A 30 GB Epic game prefills successfully through Lancache.
- [ ] Preferred-CDN pinning verified in logs and via disk-stat spot-check (same cache-key locality expected across runs).
- [ ] Vendored legendary modules are importable from a `vendor/legendary/` subtree with a pinned upstream SHA recorded in a VENDORED.md.

---

### F7. Cache Validator (disk-stat)

**Trigger.** (a) Automatically on prefill completion (F5/F6); (b) manually via `POST /api/games/{platform}/{app_id}/validate`; (c) CLI `orchestrator-cli game <platform>/<app_id> validate`; (d) optionally, a scheduled full-library validation sweep (post-MVP — not in v1).

**Behavior.**
1. **Startup self-test** (runs once per container boot, before any validation is accepted):
   - Select the most-recently-prefilled game with `cached_version` set.
   - Expand its manifest to a known chunk.
   - Compute cache key: Steam → `"steam" + uri + slice_range`; Epic → `http_host + uri + slice_range`.
   - MD5 the key → 32-char hex `H`. Expected disk path: `/data/cache/cache/<H[28:30]>/<H[30:32]>/<H>`.
   - Issue one HEAD to Lancache with headers identical to a real prefill; assert `X-Upstream-Cache-Status: HIT`. Stat the computed path; assert `size > 0`.
   - If either check fails: log `cache_key_formula_drift_suspected` at CRITICAL, mark validator unhealthy, `/api/health` returns 503 until restart.
2. For each validation request:
   - Load manifest. Expand to 1 MiB-sliced cache entries (Lancache reslices at the proxy).
   - For each expected cache entry: compute path; `os.stat()`; file exists + size > 0 = cached.
   - Optional deeper check (configurable, off by default): read first ~1 KiB of the file and confirm embedded plaintext `KEY: ...` matches — collision detection.
   - Batch `stat()` calls in 256-file chunks via `loop.run_in_executor(None, ...)` to keep event loop responsive.
3. Produce `validation_history` row: `chunks_total`, `chunks_cached`, `chunks_missing`, `outcome` ∈ {`cached`, `partial`, `missing`, `error`}.
4. Update `games.status` based on outcome: all cached → `up_to_date`; any missing → `validation_failed`.

**Failure State.**
- **Cache volume not mounted / inaccessible:** All validations return `outcome='error'`. `/api/health` exposes `validator_unhealthy=true` and the specific mount error. Self-test also fails.
- **Disk-stat timeout / EIO:** Mark validation errored, log, do not change `games.status`.
- **Formula drift (self-test fails):** Described above — refuse further validations, loud alert on status page.
- **Manifest unavailable (e.g., platform API down):** Cannot derive expected chunks → `outcome='error'`. Games whose manifest can't be replayed remain in their prior status.
- **HEAD probe on self-test triggers an upstream fetch:** By design, self-test uses a **known-cached** chunk (last successful prefill), so this cannot happen. If it does, it's a bug — fail loudly.

**Depends on:** F5 and/or F6 (must have cached content), read access to `/data/cache` bind mount.

**Acceptance.**
- [ ] Validates a 50 GB game in < 5s (≤51,200 stat calls, batched).
- [ ] Self-test fails loudly if cache-key formula drifts (simulate by altering `levels` or cache_identifier).
- [ ] `outcome='partial'` transitions game to `validation_failed`, not silently hiding drift.
- [ ] Never issues HEAD probes on suspected-missing chunks (would cause unintentional prefill per Brief §1.6).

---

### F8. Block List

**Trigger.** (a) `POST /api/games/{platform}/{app_id}/block` with optional `{reason}` body; (b) `DELETE /api/games/{platform}/{app_id}/block`; (c) CLI equivalents.

**Behavior.**
1. `block` → INSERT or UPDATE into `block_list` with `(platform, app_id, reason, source)`. Idempotent.
2. `unblock` → DELETE from `block_list`. Idempotent.
3. F5/F6 enqueue step checks `block_list` and skips matching `(platform, app_id)`. Log `prefill_skipped_blocked` with correlation ID.
4. F7 (validation) **does not** honor the block list — operators may want to confirm a blocked game isn't accidentally cached.
5. `games.status` displays `blocked` when a row matches, overriding prefill-state transitions (but not validation outcomes).

**Failure State.**
- **Block request for unknown `(platform, app_id)`:** Still accepted (INSERT). Game row is created on next library sync if it appears; pre-blocking is valid.
- **Concurrent block/unblock race:** Last write wins. Both are single-row operations — atomic at SQLite level.

**Depends on:** F9 (API) OR F11 (CLI).

**Acceptance.**
- [ ] Blocked game is skipped by scheduled prefill; log entry attributable via `source` and correlation ID.
- [ ] Manual validation on a blocked game still runs.
- [ ] Block persists across container restarts.

---

### F9. REST API (FastAPI, port 8765)

**Trigger.** Long-running uvicorn server inside the orchestrator container. Bound to all interfaces inside the Docker network; pfSense restricts port 8765 to the trusted VLAN at the network edge.

**Behavior (endpoint surface — per Brief §3.7):**

| Method | Path | Required | Notes |
|---|---|---|---|
| GET | `/api/health` | Yes | Liveness + version + uptime + lancache_reachable flag + validator_healthy flag |
| GET | `/api/games` | Yes | Full list with pagination optional; includes `(platform, app_id, title, status, current_version, cached_version, last_validated_at, blocked)` |
| GET | `/api/games/{platform}/{app_id}` | Yes | Detail + recent validation_history + recent jobs |
| POST | `/api/games/{platform}/{app_id}/validate` | Yes | Enqueue validation job |
| POST | `/api/games/{platform}/{app_id}/prefill` | Yes | Enqueue prefill job; respects block list (409 if blocked) |
| POST | `/api/games/{platform}/{app_id}/block` | Yes | Add to block list |
| DELETE | `/api/games/{platform}/{app_id}/block` | Yes | Remove |
| GET | `/api/platforms` | Yes | Per-platform auth + last sync state |
| GET | `/api/jobs` | Yes | Active + recent with progress |
| GET | `/api/jobs/{id}` | Yes | Single job detail |
| GET | `/api/stats` | Yes | Cache disk usage + LRU headroom |
| POST | `/api/platforms/{name}/auth` | Yes | **Bound to 127.0.0.1 only** (OQ2). Remote calls rejected with 403 even with valid token. Transport for CLI auth flows (F1/F2); not exposed to Game_shelf or LAN clients. Body schema documented in interface docs. |

**Auth.** Every endpoint except `GET /api/health` requires `Authorization: Bearer <token>`. Token loaded from `/run/secrets/orchestrator_token` at startup. Missing header → 401. Wrong token → 401. Malformed token (not a string or wrong length) → 401. **Timing-safe** token comparison (`hmac.compare_digest`).

**Per-endpoint origin restriction.** `POST /api/platforms/{name}/auth` additionally checks `request.client.host == '127.0.0.1'` before processing. Rejected with 403 `{"error": "forbidden_non_local"}` otherwise. This is a hardening layer on top of the bearer token — credentials enter only at the CLI on the DXP4800 host, never from Game_shelf or any LAN client.

**CORS.** Default-deny; allowlist `CORS_ORIGINS` env var (CSV). Documented default includes Game_shelf's origin.

**Versioning.** Mounted at `/api/v1/...`; aliased under `/api/...` for the MVP. Breaking changes require a new prefix.

**Failure State.**
- **Unauthenticated:** 401 `{"error": "unauthorized"}`. No timing leakage.
- **Malformed JSON body:** 400 `{"error": "bad_request", "details": "..."}` (Pydantic validation).
- **Unknown game in path:** 404 `{"error": "not_found"}`.
- **Internal error:** 500 `{"error": "internal_error", "correlation_id": "..."}`; full traceback in logs, never in response.
- **Docker secret missing at startup:** Container **refuses to start** with a loud log `orchestrator_token_missing` — better than running unauthenticated by accident.

**Depends on:** All data-producing features (F3–F8), structured logging, bearer-token loading. **Prerequisite for:** F10, F11 (CLI calls API locally), Game_shelf integration.

**Acceptance.**
- [ ] `GET /api/health` responds in < 50 ms p99 at idle.
- [ ] p99 < 100 ms under Spike F load (32 concurrent chunk downloads).
- [ ] Malformed requests never leak stack traces.
- [ ] Token comparison is timing-safe (SAST check + unit test).
- [ ] 401 does NOT distinguish "missing token" from "wrong token" (prevents enumeration).

---

### F10. Status Page (single-file HTML at GET /)

**Trigger.** Any GET to `/` (not `/api/*`).

**Behavior.**
1. FastAPI serves a static single HTML file (< 20 KB target). No framework, no build step, no `npm`.
2. On page load, JS prompts for the bearer token via `prompt()` and stores it in `sessionStorage`. If token already in `sessionStorage`, uses it silently.
3. Polls `/api/health`, `/api/platforms`, `/api/jobs?state=running&state=queued`, `/api/stats` every 2 seconds via `fetch()`.
4. Renders:
   - Orchestrator version, uptime, Lancache reachability indicator.
   - Per-platform auth state with **both** a colored indicator AND an icon AND a text label (colorblind-safe per Intake §9).
   - Active jobs with progress bars + last 10 completed.
   - Last N errors.
   - Disk usage on cache volume, LRU headroom.
5. "Retry" button on banner when API is unreachable.

**Accessibility (hard constraint from Intake §9).**
- Every status indicator uses text + icon + position in addition to color. Never color alone.
- Icons come from an inline SVG sprite (no icon font dependency).

**Failure State.**
- **API unreachable from the status page:** Display "Orchestrator API unreachable. Is uvicorn running?" — this only happens if the HTTP server is down, in which case the page wouldn't load either. Edge case: uvicorn up but internal routing broken — show the banner + Retry.
- **401 from API:** Clear sessionStorage token, re-prompt.
- **Token never correct:** User can escape by closing the tab; no lockout. Log 401 events at WARN on server side.

**Depends on:** F9.

**Acceptance.**
- [ ] Page loads under 1 second on a 2.5GbE LAN.
- [ ] Polling every 2 s does not observably increase API p99 latency.
- [ ] WCAG color-independence: operator (colorblind) can distinguish all status states with color disabled in a browser test mode.
- [ ] Total static file size < 20 KB gzipped.

---

### F11. CLI (orchestrator-cli, Click-based)

**Trigger.** Invoked via `docker compose exec orchestrator orchestrator-cli <subcommand>` on the DXP4800 host.

**Behavior.**
- Bundled in the same container image; reads the same `/run/secrets/orchestrator_token` to authenticate against the local REST API (`http://127.0.0.1:8765/api/v1/...`).
- Subcommand set per Brief §3.8: `auth steam|epic|status`, `library sync [--platform X]`, `game <platform>/<app_id> [status|validate|prefill|block|unblock]`, `jobs [--active]`, `jobs <id>`, `db migrate`, `db vacuum`, `config show`.
- Output is human-readable by default. **`--json` flag deferred to Post-MVP** (OQ6): MVP CLI consumers are the operator (human) only.

**Failure State.**
- **API unreachable (uvicorn not running):** CLI prints "Orchestrator API unreachable — is the service running? (`docker compose ps`)." Exit code 2.
- **401 from API (secret mismatch):** CLI prints "Token mismatch — the orchestrator_token file has changed. Restart the container." Exit code 3.
- **Unknown subcommand:** Click's default help text; exit code 2.
- **`db migrate` on a DB at the current schema version:** No-op; log "schema already at v{N}." Exit 0.

**Depends on:** F9 (the API is the transport). **Prerequisite for:** F1, F2 (auth flows live here).

**Acceptance.**
- [ ] Every subcommand round-trips through the REST API (no direct DB access from CLI).
- [ ] Help text exists for every command.
- [ ] Exit codes are documented and consistent (0 success, 1 user error, 2 environment error, 3 auth error).

---

### F12. Scheduled Sync Cycle (APScheduler cron)

**Trigger.** APScheduler fires the single recurring cron defined by `SCHEDULE_CRON` (default `0 */6 * * *` — every 6 hours on the hour).

**Behavior.**
1. Run platforms in fixed order: Steam, then Epic (serial, not parallel — avoids fan-out of CM + OAuth failures simultaneously).
2. Per platform:
   a. Refresh auth if needed (F1 or F2). Skip platform entirely on failure.
   b. Library enumeration (F3 or F4). Skip on auth expiry or API error.
   c. Diff: for each game with `owned=1 AND NOT blocked AND (current_version != cached_version OR status IN ('not_downloaded','validation_failed','pending_update'))`, enqueue a prefill job.
3. Within a platform, prefill concurrency is **1 game at a time** (Brief §3.5). Chunk-level concurrency is `PREFILL_CONCURRENCY=32`.
4. After each prefill, F7 validation runs automatically (implicit dependency).
5. On cycle completion: log `sync_cycle_complete` with (`platform`, `games_checked`, `prefills_enqueued`, `prefills_succeeded`, `prefills_failed`, `validations_run`, `elapsed_sec`).

**Failure State.**
- **APScheduler misfire (container was down when cron was due):** APScheduler's `misfire_grace_time` runs the missed job immediately on startup (default 1 hour; configure to 24 h). Missing the window entirely just waits for the next one.
- **Mid-cycle container restart:** In-flight `jobs` rows are `state='running'`; **startup reaper** marks them `state='failed'` with error `'container_restart'` so stale rows don't poison the UI. Next cycle retries.
- **Scheduler thread dies (unexpected):** uvicorn health check detects via `/api/health` exposing `scheduler_running=false`. Operator sees status page; restart container.
- **Single platform failing repeatedly:** Status page and `/api/platforms` expose the failure; other platforms continue.

**Depends on:** F1–F7.

**Acceptance.**
- [ ] A cold start with expired Steam auth and valid Epic auth completes Epic sync successfully and leaves Steam in `expired` status.
- [ ] APScheduler `MemoryJobStore` reloads the recurring cron from config on every startup (no lost cron).
- [ ] Startup reaper cleans up abandoned `jobs` rows within 5 s of container start.
- [ ] Cycle logs include all counts listed above.
- [ ] **Scheduler-health surfacing (JQ3 resolution 2026-04-20):** `/api/health` returns a `scheduler_running: bool` field derived from `APScheduler.state`. If `state != STATE_RUNNING`, `/api/health` returns HTTP 503 with body `{"status": "degraded", "scheduler_running": false, "scheduler_last_error": "<exception repr or 'unknown'>"}`. F10 status page and F16 Cache dashboard both render this as a prominent red banner with text label "Scheduler stopped — container restart required" (colorblind-safe per Intake §9). Unit test: kill the scheduler inside a test container, confirm `/api/health` flips to 503 within the next poll interval.

---

### F13. Scheduled Full-Library Validation Sweep (added by OQ7 resolution)

**Trigger.** Second APScheduler cron, separate from F12's sync cron. Default: `VALIDATION_SWEEP_CRON = '0 3 * * 0'` (Sundays at 03:00). Configurable env var.

**Behavior.**
1. Enumerate every `games` row where `cached_version IS NOT NULL AND NOT blocked`.
2. For each game, invoke F7 validator in batches of 10 in parallel (chunk-level parallelism already inside F7 via `run_in_executor`). Sweep-level parallelism is bounded at 10 to avoid starving the API.
3. On per-game validation outcome:
   - `cached` → leave `games.status` unchanged.
   - `partial` or `missing` → set `games.status = 'validation_failed'`; next F12 cycle will re-enqueue a prefill because F5/F6 treat `validation_failed` as stale.
   - `error` → log at ERROR, leave `games.status` unchanged (can't distinguish "actually cached" from "can't tell").
4. Emit a single summary log line at sweep completion: `validation_sweep_complete` with (`games_checked`, `cached`, `partial`, `missing`, `error`, `elapsed_sec`).
5. Startup reaper (F12) also clears any stale sweep in-progress rows.

**Failure State.**
- **Sweep takes longer than one hour:** Not a failure per se; log at WARN if elapsed > `SWEEP_WARN_HOURS` env var (default 2). At 2600 games × ~50 k chunks avg = ~130M stat() calls; batched and parallelized this should run in 5–15 min, but new hardware classes (ARM NAS CPU constraints per Intake §11) need the warning threshold.
- **F7 validator unhealthy (self-test failed earlier):** Sweep refuses to start; log `validation_sweep_skipped_validator_unhealthy`. `/api/health` already surfaces validator state.
- **Container restart mid-sweep:** Startup reaper marks sweep job `failed`; next cron iteration re-runs the sweep. No partial-completion bookkeeping needed — F7 re-validation is idempotent.
- **Storage read failure on one game:** Marked `error`, sweep continues. Does not abort the batch.

**Depends on:** F7, F12 (scheduler infrastructure).

**Acceptance.**
- [ ] Full sweep of 2600 games completes in < 30 min on DXP4800 hardware (validated in Spike E or equivalent).
- [ ] Discovers eviction drift within ≤ 7 days (addresses R13).
- [ ] Does not trigger upstream fetches (disk-stat only, never HEAD probes on suspected-missing).
- [ ] Sweep does not impact `GET /api/health` p99 latency beyond normal idle levels.

---

### F14. Game_shelf Backend Proxy Routes (promoted to MVP by OQ1 resolution)

**Trigger.** This feature lives in the **Game_shelf repo** (`kraulerson/Game_shelf`), not the orchestrator repo. It is a required MVP deliverable because Intake §2.3 Success Criteria #5 is directly satisfied by F14–F16.

**Behavior.**
1. Add `backend/src/routes/cache.js` mounted at `/api/cache` in Game_shelf's `server.js`.
2. Add `backend/src/services/orchestratorClient.js` — a thin axios wrapper with base URL, timeout, and `Authorization: Bearer` header injected from env.
3. Environment additions to Game_shelf (documented in its `.env.example`):
   - `ORCHESTRATOR_URL` (e.g., `http://dxp4800.local:8765`)
   - `ORCHESTRATOR_TOKEN` (matches the Docker secret on DXP4800)
   - `ORCHESTRATOR_TIMEOUT_MS` (default 5000)
4. Proxy routes (each requires existing Game_shelf auth middleware):

   | Method | Game_shelf path | Upstream orchestrator call |
   |---|---|---|
   | GET | `/api/cache/health` | `GET /api/v1/health` |
   | GET | `/api/cache/games` | `GET /api/v1/games` |
   | GET | `/api/cache/games/:platform/:app_id` | `GET /api/v1/games/{platform}/{app_id}` |
   | POST | `/api/cache/games/:platform/:app_id/validate` | pass-through |
   | POST | `/api/cache/games/:platform/:app_id/prefill` | pass-through |
   | POST | `/api/cache/games/:platform/:app_id/block` | pass-through |
   | DELETE | `/api/cache/games/:platform/:app_id/block` | pass-through |
   | GET | `/api/cache/platforms` | `GET /api/v1/platforms` |
   | GET | `/api/cache/jobs` | pass-through |
   | GET | `/api/cache/stats` | pass-through |
5. `POST /api/cache/platforms/:name/auth` is **explicitly not proxied** — auth entry is CLI-only per OQ2.
6. 401 from orchestrator (token mismatch) → log loudly in Game_shelf, return 502 with `{"error": "orchestrator_auth_mismatch"}` to frontend. Never bubble 401 to user (would be misleading).
7. Timeout / refused connection → return 503 with `{"status": "orchestrator_offline"}` so the frontend can distinguish offline from genuine errors.

**Failure State.**
- **Token mismatch between Game_shelf env var and DXP4800 Docker secret:** Game_shelf logs WARN with full detail (host, token hash prefix, attempted route); frontend gets 502 with generic error. Operator rotates by updating both sides.
- **Orchestrator version < required API version:** Brief §3.7 versioning — Game_shelf targets `/api/v1/`. If orchestrator responds to v1 routes with anything other than expected schema, individual routes return 502. `/api/cache/health` response includes orchestrator version for frontend skew detection.
- **DNS resolution of `ORCHESTRATOR_URL` fails:** Same as offline — 503. Logged.
- **Orchestrator 500 response:** Propagated as 502 to frontend; orchestrator `correlation_id` included in Game_shelf's log line for cross-system traceability.

**Depends on:** F9 (orchestrator API) reachable from Game_shelf's LXC on 1GbE path. **Prerequisite for:** F15, F16.

**Acceptance.**
- [ ] All 10 proxy routes pass an integration test from Game_shelf against a locally-running orchestrator.
- [ ] 502 distinguishable from 503 distinguishable from 5xx in frontend logs.
- [ ] Orchestrator token never exposed to Game_shelf's frontend (only the backend injects it).
- [ ] Version mismatch produces a soft warning, not a hard break.

---

### F15. Game_shelf Frontend — Cache Badge and Cache Panel (promoted to MVP by OQ1)

**Trigger.** Rendered on existing Game_shelf pages whenever a game row is displayed.

**Behavior.**
1. New component `frontend/src/components/CacheBadge.jsx` — compact pill inline on `GameCard.jsx` and `GameRow.jsx`. States:
   - `cached` — green + filled-checkmark icon + text "Cached"
   - `pending-update` — amber + arrow-up-right icon + text "Update pending"
   - `downloading` — blue + spinner + text "Downloading X%"
   - `missing` — gray + dash icon + text "Not cached"
   - `blocked` — slate + prohibition icon + text "Blocked"
   - `validation-failed` — red + warning-triangle icon + text "Validation failed"
   - `unknown` — ghost + question-mark icon + text "Unknown"
   **Colorblind-safe (Intake §9 hard constraint):** Every state uses **all three** of color + icon + text label. No state is distinguishable by color alone.
2. New component `frontend/src/components/CachePanel.jsx` — rendered as a new section inside `frontend/src/pages/GameDetail.jsx`:
   - Current manifest version vs cached version.
   - Chunk coverage % (from validation_history).
   - Last validated timestamp, last prefilled timestamp.
   - Recent 5 jobs (kind, state, progress, error if any).
   - Action buttons: Validate, Force Prefill, Block/Unblock (all call F14 proxy routes).
   - Action-button state disabled when orchestrator is offline.
3. New `frontend/src/utils/cacheApi.js` TanStack Query hooks:
   - `useCacheHealth()` — 30 s staleTime, no retry on offline.
   - `useCacheForGames()` — bulk fetch of all cache rows, 60 s staleTime. Library view reads this map by `(platform, app_id)`. **One request per library view, not N+1.**
   - `useCacheForGame(platform, appId)` — single-game detail fetch.
   - Mutations: `useValidate`, `usePrefill`, `useBlock`, `useUnblock` — with optimistic updates + rollback on 4xx/5xx.
4. Styling follows existing Game_shelf Tailwind + `lucide-react` conventions. Icon set: `lucide-react` (already a dep).

**Failure State.**
- **useCacheHealth() returns 503:** Library page shows dismissible banner: "Cache orchestrator unreachable — cache state hidden. Library browsing is unaffected." Badges render as neutral "—" with tooltip. Mutations disabled.
- **Individual mutation fails:** Toast notification with the error; optimistic update rolls back.
- **Cache row missing for a game (library has a title the orchestrator doesn't know about):** Badge renders as `unknown` with tooltip "Not yet tracked — will appear after next orchestrator sync."
- **Frontend/backend schema mismatch:** Tolerant merging — extra fields ignored, missing fields rendered as "—" (R21 mitigation).

**Depends on:** F14.

**Acceptance.**
- [ ] Library page renders 500 games with cache badges with a single `/api/cache/games` request.
- [ ] No color-only state distinction verified by reviewing a grayscale screenshot.
- [ ] Orchestrator offline does not crash or freeze the library page.
- [ ] CachePanel buttons correctly enable/disable based on orchestrator health and block state.

---

### F16. Game_shelf Frontend — Cache Dashboard Page (promoted to MVP by OQ1)

**Trigger.** Operator navigates to `/cache` in the Game_shelf frontend.

**Behavior.**
1. New page `frontend/src/pages/Cache.jsx`, linked from `Nav.jsx`.
2. Sections:
   - **Overall stats:** disk usage on cache volume, HIT/MISS ratio (from `/api/cache/stats`), queue depth, active prefills.
   - **Platform auth status:** per-platform cards (Steam, Epic) with colored + iconic + text status indicator, last-sync timestamp, token expiry, and an amber call-out with exact CLI command when reconnect is needed (e.g., "Run `docker compose exec orchestrator orchestrator-cli auth steam` on DXP4800").
   - **Recent jobs feed:** last 25 jobs with kind, state, game title, elapsed, error.
   - **Global block list management:** searchable list of blocked games, with unblock action per row.
3. Polling cadence: 10 s for stats/jobs; 60 s for platform status (changes rarely).

**Failure State.**
- Orchestrator offline: full-page banner; sections render skeleton states with "unavailable" labels. No partial-functional illusion.
- Individual section fetch fails: section-local error state, other sections unaffected.

**Depends on:** F14.

**Acceptance.**
- [ ] Page is the only place in Game_shelf that surfaces orchestrator-level diagnostics (separate from the orchestrator's own `status.html` at port 8765, which remains the fallback surface when Game_shelf is offline).
- [ ] Block list page supports ≥500 entries without pagination (scope defense — if user blocks all free-to-play games they never play, the list could be long).
- [ ] Platform-reconnect CLI command text is copy-paste-able (selectable text in UI).

---

### F17. Graceful Degradation — Both Directions (promoted to MVP by OQ1)

**Trigger.** Either orchestrator or Game_shelf becomes unreachable from the other.

**Behavior (orchestrator → Game_shelf:** Game_shelf offline).
1. Orchestrator keeps running unchanged. Scheduled prefills fire. F7 validations complete. F13 sweep runs. Disk fills.
2. Orchestrator's `status.html` at `http://<dxp4800>:8765/` (F10) and `orchestrator-cli` (F11) remain the operator surface.
3. When Game_shelf returns, it pulls fresh state via `GET /api/cache/games` on next user action.

**Behavior (Game_shelf → orchestrator:** orchestrator offline, from Game_shelf's perspective).
1. `useCacheHealth()` 5 s timeout → falls to offline state.
2. Library banner: "Cache orchestrator unreachable — cache state hidden. Library browsing is unaffected." Dismissible, with Retry button.
3. Badges: neutral `—` with tooltip "Cache status unavailable."
4. Mutations disabled with tooltips pointing at the same banner.
5. **No automatic retry storms.** One health check on initial load; Retry button for manual re-check.

**Version skew handling (R21 mitigation).**
1. Orchestrator exposes `/api/v1/`. Game_shelf targets that prefix explicitly.
2. `/api/cache/health` response includes orchestrator version.
3. Game_shelf frontend displays a soft banner if its `APP_EXPECTED_ORCHESTRATOR_VERSION` range doesn't cover the reported version. Soft = informational, not blocking.

**Failure State.**
- **Network partition (both directions intermittent):** Each side's local workflow unaffected. Polling reconciles state on reconnect.
- **Clock skew between hosts affects token lifetime:** Not an issue — tokens are long-lived static secrets, not time-bounded.
- **Bearer token leaked in Game_shelf's browser devtools by accident:** **The token is NEVER sent to the frontend.** Backend-only injection. F14 acceptance already covers this. If a developer accidentally passes the token through to the frontend during integration, code review + automated grep in CI should catch it.

**Depends on:** F9 (orchestrator health endpoint), F14 (proxy offline semantics).

**Acceptance.**
- [ ] Kill orchestrator mid-session → Game_shelf library page still renders all games; cache columns show offline state; no console errors.
- [ ] Kill Game_shelf mid-session → orchestrator shows no error (it wasn't calling Game_shelf); next scheduled cycle runs normally.
- [ ] Version skew simulation (mock older/newer orchestrator version in /api/cache/health response) → correct soft-warning behavior.
- [ ] Bearer token never appears in browser devtools Network tab or localStorage (outside of F10's own status page at port 8765).

---

## 3. Should-Have Features (Post-MVP v1.1) — updated after OQ1 and OQ7 resolution

**Removed from Post-MVP (promoted to MVP):**
- Game_shelf integration (now F14–F17).
- Scheduled full-library validation sweep (now F13).

**Remaining Post-MVP v1.1 (canonical table format per `frd.tmpl`):**

| # | Feature | Description | Deferred Because |
|---|---------|-------------|-----------------|
| 1 | SSE/WebSocket live progress stream | Real-time job progress feed to replace poll-every-2s pattern | MVP 2s polling is sufficient; SSE adds transport + frontend complexity |
| 2 | Access-log tail for passive HIT/MISS observation | Stream Lancache `access.log` to detect unexpected cache hits (games not in our library) and eviction events | Optional observability layer; disk-stat validator already answers "is it cached now?" |
| 3 | Prometheus metrics endpoint | Expose `/metrics` for external monitoring stack | No monitoring infrastructure in this homelab yet |
| 4 | Incremental validation | Only re-validate games whose manifest changed since last validation — optimization over F13 full sweep | F13 full sweep is cheap enough at 2600 games on this hardware |
| 5 | CLI `--json` flag | Machine-parseable CLI output for scripting | MVP consumers are the human operator (OQ6) |

---

## 4. Will-Not-Have Features (verbatim from Intake §4.3 + violation check)

**Canonical table format per `frd.tmpl`:**

| # | Feature | Exclusion Rationale |
|---|---------|-------------------|
| 1 | Ubisoft Connect prefill | Lancache caching broken upstream (monolithic#195, 21-month-open issue); protocol libraries incomplete and targeting moving proprietary protocol |
| 2 | EA App prefill | Hostile vendor with TLS-pinned sessions; only OSS effort (Maxima) is self-described "pre-pre-pre-alpha"; endpoints largely non-cacheable post-2025 refactor |
| 3 | GOG prefill | GOG CDN not in `uklans/cache-domains`; HTTPS-only to Fastly; Lancache cannot intercept without MITM — architecturally impossible |
| 4 | Game installation or launching | Scope discipline: the orchestrator fills the cache, it does not install or run games |
| 5 | Multi-user or multi-tenant support | Single user, single Lancache instance, LAN-only deployment |
| 6 | A second React SPA inside the orchestrator | Rich UI lives exclusively in Game_shelf (F14–F17); orchestrator ships single-file HTML diagnostics page (F10) + CLI (F11) |
| 7 | ORM (SQLAlchemy / Alembic) | Seven tables, straightforward CRUD; raw SQL via `aiosqlite` is easier to read and audit; also removes SQLAlchemy + dependency chain for `APScheduler` jobstore |

**Violation check.** None of the 17 Must-Have features (F1–F17) implicitly requires any excluded item. F5/F6 are Steam/Epic only by design. F10 is single-file HTML (no SPA). F7/F5/F6/F8 all use raw SQL (no ORM). F14–F17 live in the Game_shelf repo, which does use React — but that's an existing React app, not a new SPA inside the orchestrator. No violations.

---

## 5. Implicit Dependencies (not in Intake §4.1; must be addressed in Phase 1 or explicitly deferred)

These are features or cross-cutting requirements that the 12 Must-Haves assume but don't name. Calling them out now avoids "discovered during construction" surprises.

| # | Dependency | Why it's implicit | Recommendation |
|---|---|---|---|
| ID1 | **SQLite DB initialization + migration framework** | F3/F4/F5/F6/F7/F8/F9 all read/write `games`, `platforms`, `manifests`, `block_list`, `validation_history`, `jobs`, `cache_observations`. No feature names "schema exists" as a requirement. | Add a pre-F1 foundational requirement: numbered `.sql` migrations in `migrations/` applied by a ~50-LoC migrate script on container start. Brief §5 already specifies this. Must be F0 (zero) in the build order. |
| ID2 | **Lancache reachability self-test on container start** | F5/F6 assume Lancache is reachable at `http://lancache:80` (compose service name). F7 self-test assumes `/data/cache` is mounted read-only. If either is wrong, every feature silently fails. | Add to `/api/health`: `lancache_reachable` (from `GET /lancache-heartbeat` check) and `cache_volume_mounted` booleans. Container does NOT refuse to start (graceful degradation to allow operator diagnosis) but surfaces loudly. |
| ID3 | **Structured logging with correlation IDs** | CLAUDE.md Construction Rule: "Every significant operation produces a log entry with timestamp, severity, and correlation ID." Not named in any Intake feature. | Add as cross-cutting requirement. `structlog` per Brief §6.1. Correlation ID generated at job-creation or API-request entry, propagated through async context. |
| ID4 | **Secret loading at startup** | F9 requires `/run/secrets/orchestrator_token` to exist. F1/F2 optionally read `/run/secrets/steam_webapi_key`. Missing secret must fail fast, not at first request. | Container startup validates all required secrets exist and are non-empty; logs and exits non-zero if missing. |
| ID5 | **Automatic post-prefill validation** | F5/F6 Intake triggers say "after each prefill completes, the validator must compute…" — this ties F7 to F5/F6 but F7's Intake entry lists "on demand (via API/CLI)" without mentioning the post-prefill automatic case. | Make it explicit in F5/F6/F7 acceptance criteria: every successful prefill job emits a validation job. Already captured in FRD §2 above. |
| ID6 | **Startup reaper for abandoned jobs** | F12 resilience against container restart depends on cleaning up `jobs` rows in `state='running'` at boot. Not called out in any Intake feature. | Add to F12 as implicit requirement (captured in FRD §2). |
| ID7 | **CLI bundling inside the container** | F11 requires the `orchestrator-cli` entrypoint. Deployment model is `docker compose exec` so the CLI must be on `$PATH` inside the image. | Add to Dockerfile/image build in Phase 2; not a separate feature. |
| ID8 | **Status page accessibility compliance** | Intake §9 states "the operator is colorblind. Never rely on color alone." F10 mentions this but the enforcement mechanism is implicit. | Add an accessibility check step (linter or manual) to Phase 2 Build Loop for F10. Light track doesn't require formal WCAG AA, but this single constraint is hard. |
| ID9 | **Backup of SQLite state** | Intake §5.4 requires weekly SQLite backup; Brief Appendix C marks it as "out of scope" but the Intake says it's required. | Light-track interpretation: a simple cron on DXP4800 (outside the container) using `sqlite3 state.db ".backup /backup/...".` Not a container feature; document in deployment guide during Phase 4. |
| ID10 | **pfSense / network segmentation (external)** | Brief R19 mitigation says port 8765 is restricted by pfSense firewall rule. This is not a software feature. | Document as a deployment prerequisite in Phase 4 handoff. Out of scope for the orchestrator code itself. |

---

## 6. Cross-Cutting Requirements

These apply to every feature.

1. **Security.** No credentials in logs. No secrets in environment variables (only Docker secrets for token and optional webapi_key). Timing-safe token comparison. Bearer token required on every non-`/api/health` endpoint. Input validation via Pydantic on every endpoint.
2. **Observability.** Structured JSON logs via `structlog`. Correlation IDs on every log entry within a request/job. Log levels: DEBUG (verbose chunk-level), INFO (feature-level), WARN (recoverable), ERROR (feature impact), CRITICAL (service-level).
3. **Performance.** Event-loop discipline per Brief §3.6: async-native on main loop; gevent isolated to dedicated thread for `steam-next`; blocking I/O (disk stat, MD5) via `run_in_executor`. Spike F load test gates the asyncio-only design.
4. **Accessibility.** Colorblind-safe in F10 and in any CLI output that uses color for status (always include text label).
5. **Stability.** Container restarts must not lose any state that wasn't ephemeral by design. Migrations are idempotent. Job reaper handles mid-flight interruption.
6. **Testability.** Every feature has unit tests for pure logic (manifest dedup, cache-key computation, diff logic) and integration tests for I/O boundaries (DB, HTTP to mock Lancache). Per CLAUDE.md: test-first.
7. **Upstream dependency monitoring — `fabieu/steam-next` bus-factor policy (OQ4 resolution).** The Steam adapter pins `fabieu/steam-next` by git SHA. A CI job (or scheduled agent) checks `https://github.com/fabieu/steam-next/commits/main` weekly. **If the upstream has had no commits for >15 days, fork to `github.com/kraulerson/steam-next` immediately** and re-pin the orchestrator's requirement to the fork. This is meaningfully tighter than the Brief R1 mitigation (which said "be prepared to maintain internally"). Formalize as ADR in Phase 1 Step 1.2.

---

## 7. Open Questions — Resolved by Orchestrator 2026-04-20

All seven questions from the initial FRD draft have been resolved. Decisions recorded here for the Product Manifesto and future audit.

### OQ1. Game_shelf integration classification — **RESOLVED: keep as MVP**
**Decision.** Game_shelf integration remains in the MVP. Intake §2.3 Success Criteria #5 is now directly satisfied by new Must-Have features **F14 (backend proxy routes), F15 (CacheBadge + CachePanel), F16 (Cache dashboard page), F17 (graceful degradation)**.
**Scope impact.** +1–2 calendar weeks of work per Brief §8. Cross-repo delivery required (this repo + `kraulerson/Game_shelf`). Intake §4.1 updated to include F14–F17; Intake §4.2 updated to remove Game_shelf item.

### OQ2. `POST /api/platforms/{name}/auth` transport — **RESOLVED: bind to 127.0.0.1**
**Decision.** Endpoint exists, but enforces `request.client.host == '127.0.0.1'` in addition to the bearer token. Remote calls rejected with 403 `{"error": "forbidden_non_local"}`. F9 updated accordingly.
**Rationale.** Preserves "CLI always talks through API" invariant while ensuring credentials can only be submitted from the DXP4800 host itself.

### OQ3. Brief-phase vs Solo Orchestrator-phase terminology — **RESOLVED: use Build Milestones A–E**
**Decision.** All subsequent project artifacts refer to Brief delivery phases as **"Build Milestones":**
- **Milestone A** — Spikes (original Brief "Phase 0")
- **Milestone B** — Steam adapter + core orchestrator (Brief "Phase 1")
- **Milestone C** — Epic adapter (Brief "Phase 2")
- **Milestone D** — Game_shelf integration (Brief "Phase 3"; now also MVP-scope)
- **Milestone E** — Operational hardening (Brief "Phase 4")

Phase 0–4 terminology is reserved exclusively for Solo Orchestrator methodology phases (Discovery, Architecture, Construction, Validation, Release). The Intake §13 initialization prompt will be annotated with this mapping.

### OQ4. `fabieu/steam-next` bus-factor policy — **RESOLVED: fork if upstream silent >15 days**
**Decision.** Pin upstream by git SHA in Phase 2. Monitor weekly. **If no commits in main for >15 days, fork immediately to `github.com/kraulerson/steam-next` and re-pin.** §6 cross-cutting requirement #7 updated. Formalize in a Phase 1 ADR.
**Note.** 15 days is meaningfully tighter than the brief's implicit "dormant" threshold. Monitoring must be automated (CI cron or scheduled agent) because 15 days is short enough to miss if checked manually.

### OQ5. Status page authentication UX — **RESOLVED: keep `prompt()`**
**Decision.** F10 uses a browser `prompt()` for bearer-token entry, stored in `sessionStorage`. Diagnostic page, single operator, not worth a login-form surface.

### OQ6. CLI `--json` flag — **RESOLVED: Post-MVP**
**Decision.** F11 ships with human-readable output only in MVP. `--json` moved to Post-MVP list.

### OQ7. Scheduled full-library validation sweep — **RESOLVED: add as MVP F13**
**Decision.** Weekly scheduled full-library validation sweep is now Must-Have **F13**. Default cadence Sundays 03:00 via separate APScheduler cron. Mitigates R13 (cache eviction drift) directly.

---

## 7a. Carry-Forward Notes

- **F13 cadence tunable:** Weekly default is sized for ARM NAS CPU. Profile during Milestone A/B Spike E on real hardware; adjust if sweep takes > 30 min.
- **Steam-next fork monitoring:** Implementation in Milestone B. ADR required.
- **Game_shelf PR scope:** Milestone D = a single PR to `kraulerson/Game_shelf` covering F14–F17. Will reference this FRD.
- **OQ1 scope expansion formalization:** This scope change should be noted in `APPROVAL_LOG.md` when the Phase 0 → Phase 1 gate is crossed, documenting that the original Intake Post-MVP list was modified at Phase 0 close.

---

## 8. Review Checklist (per Builder's Guide Step 0.1)

- [x] Every Must-Have has a logic trigger (If X, then Y) — ✅ F1–F17
- [x] Every Must-Have has a defined failure state — ✅ F1–F17
- [x] No feature described in vague terms — ✅ triggers, acceptance, and failure paths all concrete
- [x] Will-Not-Have list has at least 3 items — ✅ 7 items, no violations
- [x] Contradictions surfaced and resolved — ✅ OQ1–OQ7 all closed
- [x] Implicit dependencies flagged — ✅ 10 items in §5

---

## 9. Sign-off

**All Open Questions resolved** (2026-04-20). FRD is ready for Phase 0 Step 0.2.

**MVP feature count:** 17 Must-Haves (original 12 + F13 validation sweep + F14–F17 Game_shelf integration).

**Scope expansion acknowledgement:** OQ1 promoted Game_shelf integration to MVP, expanding the build by ~1–2 weeks and introducing a cross-repo dependency (`kraulerson/Game_shelf`). To be recorded in `APPROVAL_LOG.md` at the Phase 0 → Phase 1 gate.

**Any future Must-Have change requires an explicit scope decision recorded in `APPROVAL_LOG.md`.**

**Next Phase 0 step:** 0.2 — User Journey Map (Skeptical PM persona).
