# Data Contract — lancache_orchestrator

**Phase:** 0
**Step:** 0.3
**Generated from:** Intake §5 + FRD v1 + Brief §5 (SQLite schema) + Brief §3.2 (auth model)
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

---

## 1. Purpose

This document is the formal Data Contract between the orchestrator and every actor it talks to — the human operator via CLI, the browser via the status page, Game_shelf via proxy, and every third-party upstream (Steam CM, Steam CDN, Epic Games Services, Epic CDN, Lancache nginx). It defines **what data crosses each boundary, how it is validated, how sensitive it is, where it persists, and what happens when each upstream goes dark.**

It is a Phase 0 artifact. It does not specify implementation — that belongs to Phase 1 (Project Bible) and Phase 2 (construction). But the Project Bible must be derivable from this contract without ambiguity.

Sensitivity classifications from the Intake template: **Public, Internal, Confidential, PII, Financial, Health/Medical, Regulated**.

---

## 2. Inputs

### 2.1 Input surface summary

The orchestrator has **six distinct input surfaces**, each with a different trust level:

| # | Surface | Trust | Transport | Required? |
|---|---|---|---|---|
| IS1 | Docker secrets mounted at `/run/secrets/*` | Trusted (host-level) | Read-only file | Yes (orchestrator_token); conditional (steam_webapi_key) |
| IS2 | Environment variables from Docker Compose | Trusted (host-level) | Process env | Yes |
| IS3 | CLI stdin (interactive auth prompts) | Trusted (DXP4800 shell) | TTY | Yes for F1, F2 |
| IS4 | REST API request bodies + headers | Authenticated (bearer) | HTTPS/HTTP over LAN | Yes for all mutations |
| IS5 | Upstream HTTP responses (Steam/Epic/Lancache) | Untrusted (external) | HTTP(S) | Yes — all sync cycles |
| IS6 | SQLite DB (on restart — re-reads own writes) | Trusted (own filesystem) | File | Yes |

Every data field crossing one of these surfaces is listed below with validation rules and classification.

### 2.2 Docker secrets (IS1)

| Secret file | Purpose | Sensitivity | Validation at load |
|---|---|---|---|
| `/run/secrets/orchestrator_token` | Bearer token for `/api/v1/*` authentication (F9, F11, F14) | **Confidential** | Required. Non-empty after `.strip()`. Minimum length 32 chars. Fails fast with `CRITICAL orchestrator_token_missing` + exit(1) if missing. Logged as `token_sha256_prefix=<first 8 hex of SHA256>` — never the token itself. |

**Zero environment-variable or DB persistence.** Steam Web API key fallback **removed from MVP (DQ1 resolution 2026-04-20)** — Steam library enumeration uses the full CM login path only. Revisit Post-MVP if a "library-only, no CDN prefill" mode is requested.

### 2.3 Environment variables (IS2)

All parsed by `pydantic-settings` into a typed `Settings` model at startup. Validation errors → log + exit(1).

| Variable | Type | Default | Validation | Sensitivity |
|---|---|---|---|---|
| `LANCACHE_HOST` | str (hostname) | `lancache` | Non-empty. Must resolve to a reachable host on the compose network. | Internal |
| `SCHEDULE_CRON` | str (cron expr) | `0 */6 * * *` | Valid APScheduler cron expression. Parser validates before scheduler registration. | Internal |
| `VALIDATION_SWEEP_CRON` | str (cron expr) | `0 3 * * 0` | Same validation as SCHEDULE_CRON. Must be a cron with longer period than SCHEDULE_CRON. | Internal |
| `PREFILL_CONCURRENCY` | int | 32 | 1 ≤ n ≤ 128. Chunk-level concurrency bound on httpx semaphore. | Internal |
| `PER_PLATFORM_PREFILL_CONCURRENCY` | int | 1 | 1 ≤ n ≤ 4. Games-at-once per platform (Brief §3.5). | Internal |
| `STEAM_PREFERRED_CDN` | str (hostname) or empty | empty (let Steam pick) | If set, valid hostname. Not validated against Steam's actual CDN list at startup. | Internal |
| `EPIC_PREFERRED_CDN` | str (hostname) | `download.epicgames.com` | Must be in the `uklans/cache-domains/epicgames.txt` allowlist (validated at startup). | Internal |
| `CORS_ORIGINS` | str (CSV) | empty | Each entry must parse as a URL origin. Entries with wildcards rejected. | Internal |
| `LOG_LEVEL` | str | `INFO` | One of DEBUG/INFO/WARNING/ERROR/CRITICAL. | Internal |
| `CACHE_ROOT` | str (abs path) | `/data/cache/cache` | Absolute path. Existence + read-access checked at startup (ID2 health flag derives from this). | Internal |
| `STATE_DIR` | str (abs path) | `/var/lib/orchestrator` | Absolute path. Existence + read/write-access checked at startup. | Internal |
| `SWEEP_WARN_HOURS` | int | 2 | 1 ≤ n ≤ 48. | Internal |
| `API_BIND_HOST` | str | `0.0.0.0` | Valid IP. 127.0.0.1 acceptable for debugging; `0.0.0.0` default for container. | Internal |
| `API_PORT` | int | 8765 | 1024 ≤ n ≤ 65535. | Internal |

**Never via env var:** tokens, secrets, credentials, API keys. These must go through IS1.

### 2.4 CLI interactive stdin (IS3)

| Prompt | Consumed by | Validation | Sensitivity |
|---|---|---|---|
| Steam username | F1 CLI | Non-empty. Trimmed. UTF-8. No length cap (Steam enforces). | **Confidential** (credential component) |
| Steam password | F1 CLI | Non-empty. Read via `getpass` (no echo). No logging, not persisted. Passed directly to steam-next. | **Confidential** |
| Steam Guard code | F1 CLI | 5-character alphanumeric (mobile) OR 6-digit numeric (email) — exact shape depends on JQ1 resolution. Validated by steam-next, not locally. | **Confidential** (OTP) |
| Epic auth code | F2 CLI | Non-empty; normalized (strip `?code=` prefix if user paste-the-URL-by-mistake). Further validated by Epic on exchange. | **Confidential** (short-lived auth grant) |
| Status-page `prompt()` entry | F10 browser | Non-empty string. Trimmed. Stored in sessionStorage on client side. | **Confidential** (same token as IS1) |

**All credential inputs are held in memory only during the CLI invocation.** On successful exchange, only the refresh tokens persist (IS6 §2.6). On failure, no persistence.

### 2.5 REST API inputs (IS4)

Pydantic models enforce every schema. Bearer token validation precedes schema validation. For `POST /api/v1/platforms/{name}/auth`, the 127.0.0.1 origin check precedes bearer validation.

| Endpoint | Body schema | Query/path validation | Sensitivity of input |
|---|---|---|---|
| `GET /api/v1/health` | (none) | (none) | Public |
| `GET /api/v1/games` | (none) | Optional `?platform=steam\|epic`, `?status=...`. Strict enum. | Internal |
| `GET /api/v1/games/{platform}/{app_id}` | (none) | `platform ∈ {steam, epic}`. `app_id` matches `^[A-Za-z0-9_\-]{1,64}$`. | Internal |
| `POST /api/v1/games/{platform}/{app_id}/validate` | Empty or `{}` | Same as above | Internal |
| `POST /api/v1/games/{platform}/{app_id}/prefill` | Empty or `{"force": bool}` | Same | Internal |
| `POST /api/v1/games/{platform}/{app_id}/block` | `{"reason": str?, "source": enum?}` | Same. `reason` ≤ 500 chars, UTF-8. `source ∈ {cli, gameshelf, api, config}`, default `api`. | Internal |
| `DELETE /api/v1/games/{platform}/{app_id}/block` | (none) | Same | Internal |
| `GET /api/v1/platforms` | (none) | (none) | Internal |
| `POST /api/v1/platforms/{name}/auth` | platform-specific (see below) | 127.0.0.1 origin check; `name ∈ {steam, epic}` | **Confidential** |
| `GET /api/v1/jobs` | (none) | Optional `?state=queued\|running\|succeeded\|failed\|cancelled`, `?kind=...`, `?limit=1..500` default 50 | Internal |
| `GET /api/v1/jobs/{id}` | (none) | `id` positive integer | Internal |
| `GET /api/v1/stats` | (none) | (none) | Internal |

**`POST /api/v1/platforms/steam/auth` body schema:**
```json
{
  "username": "str (1..256)",
  "password": "str (1..4096)",
  "steam_guard": "str (5..6)? optional"
}
```

**`POST /api/v1/platforms/epic/auth` body schema:**
```json
{
  "auth_code": "str (1..1024)"
}
```

**Input handling rules (apply to every endpoint):**
1. Request size cap: 32 KiB body, 8 KiB headers (FastAPI/Starlette default, explicit).
2. Unknown fields rejected (`extra='forbid'` on every Pydantic model).
3. Numeric fields rejected if they exceed their declared range.
4. String fields rejected if they contain NUL bytes.
5. All fields sanitized for logging (the auth body is redacted entirely; `Authorization` header replaced with `Bearer <redacted>`).

### 2.6 Upstream HTTP responses (IS5)

This is the highest-risk input surface. Every external response is untrusted.

| Upstream | Response we consume | Validation we must apply | Sensitivity of our handling |
|---|---|---|---|
| Steam CM (steam-next client) | Auth callbacks, license list, PICS app/package info, manifest GIDs, depot keys, manifest request codes | Library-enforced shape. We must additionally: reject `app_id` strings not matching `^[0-9]{1,10}$`; reject missing `common.name` → fallback to `steam:<id>`; never trust `depots[*].size` without bounds check. | Internal (app IDs and titles) + **Confidential** (depot keys held in RAM only, never persisted) |
| Steam CDN (chunk GETs) | HTTP response bodies | We **never read the body into application memory**. Stream-discard pattern (Brief §3.1). Only status code, `X-Upstream-Cache-Status`, `X-LanCache-Processed-By`, `Content-Length` are consumed. 508 response → mark fatal-config. | Public (the content itself never enters orchestrator process memory persistently). |
| Epic Games Services (legendary client) | OAuth tokens, library assets, catalog bulk responses, manifest URLs | Library-enforced shape + our checks: auth code exchange produces structured response; reject if missing `access_token`/`refresh_token`. Library paginated; `next` field optional but must be str or null if present. | Internal + **Confidential** (tokens, RAM + persisted to IS6 §2.6) |
| Epic CDN | Same as Steam CDN | Same stream-discard pattern. | Public |
| Lancache nginx | Heartbeat (`GET /lancache-heartbeat` → `X-LanCache-Processed-By` header) + all chunk responses above | Heartbeat: header present = true. Chunk responses: body ignored, only status + cache-status header are consumed. | Internal |

**Explicit handling rules for upstream responses:**

1. **No eval, exec, or pickle on upstream data.** All deserialization via `json.loads` (Epic), protobuf (Steam manifests), or the vendored legendary binary-manifest parser.
2. **Bounded read.** Every `httpx.stream(...)` call has a `timeout=httpx.Timeout(connect=5, read=10, write=5, pool=30)` and an explicit chunk-size loop that cannot exceed the declared `Content-Length` + 64 KiB slack.
3. **Response body size cap (DQ7 resolution 2026-04-20).** Manifest responses capped at **128 MiB by default**, configurable via `MANIFEST_SIZE_CAP_BYTES` env var (1 MiB ≤ n ≤ 1 GiB). Typical manifests are 100 KB–10 MB; 128 MiB covers rare 50 MB flight-sim / MMO outliers with headroom. Responses exceeding the cap → abort, log `upstream_manifest_oversize`, fail the enqueuing game.
4. **No trust of upstream-supplied file paths.** Manifest chunk paths are treated as opaque tokens — they appear in computed cache keys and in the Lancache request URL, never in local filesystem paths.
5. **TLS:** Steam CM uses a TLS connection library-side. Legendary's `disable_https=True` is explicitly for the CDN paths that go through Lancache (per Brief §1.2). **All authentication paths (Steam CM, Epic OAuth) use TLS.** Chunk paths route to `http://lancache:80` on the compose network — not TLS, but internal to the DXP4800.

### 2.7 Persistence reload (IS6)

On every container start, the orchestrator reads its own prior writes from SQLite. **Not a trust boundary in the security sense**, but a correctness boundary — schema drift, partial writes from crash, and migration-ordering bugs manifest here.

- Migration script (ID1) runs first. Applies any unapplied numbered `.sql` files atomically.
- Startup reaper (ID6) marks any `jobs.state='running'` rows as `failed` with error `'container_restart'`.
- Session files (`steam_session.json`, `epic_session.json`) read; if corrupted JSON, log error and treat as `never` state (don't block startup).

---

## 3. Transformations (processing steps)

Each step is discrete and has its own failure mode.

| Step | Input | Output | Failure behavior |
|---|---|---|---|
| T1. Library enumeration | Steam CM session OR Steam Web API key OR Epic OAuth session | Rows in `games` table | Partial response → log + upsert what was returned. Full failure → set `platforms.<name>.last_error`. |
| T2. Manifest fetch | `(platform, app_id, version)` + active session | Parsed manifest object (protobuf for Steam, vendored Epic parser for Epic) | 403 (expired manifest-code) → refresh code, retry once. Parse failure → fail the game, log `manifest_parse_error`. |
| T3. Chunk dedupe | Parsed manifest | Set of unique chunk SHAs + Lancache URL + Host header | Pure function. No I/O failure modes. |
| T4. Chunk fan-out | Dedupe set | Lancache cache populated | Per-chunk retry (3x with 1s/4s/16s backoff). Final fail → mark job `failed`. |
| T5. Cache-key computation | `(platform, uri, slice_range)` | `md5` hex + disk path | Pure function. Self-tested at boot. |
| T6. Disk-stat validation | Set of predicted paths | `(chunks_total, chunks_cached, chunks_missing, outcome)` | EIO → mark validation `error`. Missing file → counted as `chunks_missing`. |
| T7. Diff | `games.current_version` vs `cached_version`; block list lookup | Prefill job enqueue decisions | Pure function. |
| T8. Title resolution (Epic) | List of catalogItemIds | Dict of id→title | Per-batch partial → resolved entries upsert, missing entries get `epic:<id>` sentinel. |
| T9. Cron-trigger serialization | Scheduled time + cycle function | APScheduler job invocation | Cron parse error → reject on settings load (fail-fast). Misfire → APScheduler grace-time (24h). |
| T10. API request → job | Validated Pydantic model | Row inserted in `jobs`, game status updated | Concurrent job dedupe: if `(platform, app_id, kind)` already has a `running`/`queued` job, return 409 Conflict with existing job_id. |
| T11. Log emission | Event + context | JSON line on stdout (captured by Docker) | structlog is non-blocking; failures surface as stderr. |
| T12. Health aggregation | Scheduler state + Lancache heartbeat + cache volume state + validator self-test | `/api/v1/health` response | Any component unhealthy → 503. Body always valid JSON. |

---

## 4. Outputs

Every output has a consumer, a format, and a latency expectation. Consumers are documented so breaking changes can be anticipated.

| Output | Consumer(s) | Format | Latency target | Breaking-change cost |
|---|---|---|---|---|
| Structured JSON logs on stdout | `docker compose logs`, operator, future log-aggregator | JSON lines, one event per line | Real-time | Low — internal format; documented in a log-schema doc during Phase 2 |
| `GET /api/v1/health` | Game_shelf, status page, potential Prometheus exporter (Post-MVP) | JSON `{"status": "ok\|degraded", "version": "...", "uptime_sec": n, "scheduler_running": bool, "lancache_reachable": bool, "cache_volume_mounted": bool, "validator_healthy": bool, "git_sha": "..."}` | < 50 ms p99 idle, < 100 ms p99 under Spike F load | **High** — Game_shelf's soft-warning logic depends on shape |
| `GET /api/v1/games` | Game_shelf, CLI | JSON array of game objects | < 500 ms for 2200 games (Intake SC) | High — versioned under `/api/v1/` |
| `GET /api/v1/games/{platform}/{app_id}` | Game_shelf, CLI, status page | JSON with validation_history and recent jobs embedded | < 100 ms (Intake SC) | High |
| `GET /api/v1/platforms` | Game_shelf, status page, CLI | JSON array of platform objects | < 50 ms (Intake SC) | High |
| `GET /api/v1/jobs` | Game_shelf, status page, CLI | JSON array bounded by `?limit` | < 100 ms (Intake SC) | High |
| `GET /api/v1/jobs/{id}` | Game_shelf, CLI | JSON with payload | < 100 ms | High |
| `GET /api/v1/stats` | Game_shelf, status page | JSON with disk_free_bytes, cache_size_bytes, lru_headroom_bytes, hit_count_last_hour (from access-log tail if enabled — Post-MVP), queue_depth, platform breakdowns | < 200 ms (Intake SC) | High |
| `POST` / `DELETE` responses (validate, prefill, block, unblock) | Game_shelf, CLI | **Normalized envelope (DQ6 resolution 2026-04-20):** `{"ok": bool, "job_id": int|null, "message": str|null}`. `ok=true` on success. `job_id` present for actions that queue a background job (validate, prefill); null for synchronous actions (block, unblock). `message` optional human-readable detail (e.g., "already queued as job #42" for 409 retry). | < 100 ms (Intake SC implicit via API responsiveness) | High |
| Status page HTML | Browser | Single-file HTML + inline CSS + inline JS, < 20 KB gzipped | < 1 s first paint (Intake SC) | Medium — served at `/`, not a URL external actors navigate to |
| CLI stdout | Operator (human), scripts (Post-MVP with `--json`) | Plain text (MVP) | Line-streamed | Medium — CLI contract change requires major version bump |
| SQLite `.backup` file (via external cron) | Operator, potential restore | SQLite binary | Weekly (ID9) | N/A — recovery tool |

**Response shape example — `/api/v1/games` single entry:**
```json
{
  "platform": "steam",
  "app_id": "570",
  "title": "Dota 2",
  "owned": true,
  "blocked": false,
  "status": "up_to_date",
  "current_version": "8123456789012345678",
  "cached_version": "8123456789012345678",
  "size_bytes": 67432198144,
  "last_validated_at": "2026-04-19T22:14:07Z",
  "last_prefilled_at": "2026-04-19T15:02:41Z",
  "last_error": null,
  "recent_jobs": [],
  "validation_history_latest": {
    "outcome": "cached",
    "chunks_total": 64342,
    "chunks_cached": 64342,
    "chunks_missing": 0,
    "finished_at": "2026-04-19T22:14:07Z"
  }
}
```

**Response-shape evolution rules:**
1. Add fields freely; Game_shelf's frontend ignores unknown fields (F15 tolerant merging).
2. Remove fields only in a new `/api/v2/` major version. Soft-deprecate by keeping the field with a `null` value for one minor version.
3. Rename fields never. Add the new name, deprecate the old, remove at v2.

---

## 5. Third-Party Integrations — Data Flow and Fallbacks

Each integration has a clear contract: what the orchestrator sends, what it receives, what happens when the integration is unavailable or misbehaves.

### 5.1 Steam CM (client: `fabieu/steam-next`)

| Aspect | Detail |
|---|---|
| **What we send** | Username, password, Steam Guard code (during interactive auth); PICS app/package queries; manifest request code queries (`IContentServerDirectoryService.GetManifestRequestCode`) |
| **What we receive** | Refresh token (post-auth), license callbacks, PICS app/depot blobs with `common.name` and `manifests.public.gid`, manifest request codes (5-min TTL), depot keys |
| **Auth type** | Username + password + Steam Guard (interactive); refresh token (silent) |
| **Session lifetime** | Refresh token: Valve-controlled, typically months. Manifest request code: 5 min (per-depot). |
| **Fallback when down** | F1/F3: set `platforms.steam.auth_status='expired'` if session itself fails; `last_error` if PICS/manifest-code fetch fails. Other platforms continue. Per-game prefill marks `games.status='failed'` with error; retries next cycle. |
| **Data we persist from it** | Refresh token (IS6 §2.6), `app_id`, `title`, `depot_ids`, `manifest_gid`, `size_bytes`. Raw manifest blob compressed into `manifests.raw` for replay. **No depot keys, no passwords, no Steam Guard codes.** |
| **Data sensitivity** | Credentials: Confidential (RAM-only); refresh token: Confidential (persisted IS6); game titles/IDs: Internal |

### 5.2 Steam CDN (HTTP GETs, via Lancache)

| Aspect | Detail |
|---|---|
| **What we send** | GET requests with `Host: <cdn_vhost>` override and `User-Agent: Valve/Steam HTTP Client 1.0` |
| **What we receive** | Chunk bodies (stream-discarded, not read into memory), `X-Upstream-Cache-Status` header, `X-LanCache-Processed-By` header |
| **Auth type** | None (public CDN, gated by manifest request code embedded in URL) |
| **Fallback when down** | Per-game prefill fails; retry next cycle. If Lancache specifically returns 508, treat as fatal-config. |
| **Data we persist from it** | Nothing in this orchestrator — the data goes into Lancache's disk cache, which the orchestrator only reads for validation via `os.stat()`. |
| **Data sensitivity** | Public (game binaries) |

### 5.3 Epic Games Services (vendored `legendary` modules)

| Aspect | Detail |
|---|---|
| **What we send** | Auth code (one-time); OAuth refresh requests; library-assets queries; catalog bulk queries; manifest-URL queries |
| **What we receive** | OAuth access + refresh tokens; library-assets JSON (paginated); catalog JSON with titles; manifest URLs with CDN base URLs |
| **Auth type** | OAuth2 (authorization code + refresh token). Hardcoded public launcher client ID (`34a02cf8f4414e29b15921876da36f9a` per Brief §1.2). |
| **Session lifetime** | Refresh tokens rotate silently on use; valid indefinitely unless Epic-side revocation. |
| **Fallback when down** | Same pattern as Steam: platform-isolated failure. Title resolution partial → sentinel `epic:<id>`. |
| **Data we persist from it** | Access/refresh tokens (IS6 §2.6), `catalogItemId`, `title`, `buildVersion`. Raw manifest blob compressed into `manifests.raw`. |
| **Data sensitivity** | Credentials: Confidential (auth code RAM-only, tokens persisted IS6); catalog titles/IDs: Internal |

### 5.4 Epic CDN (via Lancache)

Same shape as Steam CDN. URL pattern `http://lancache/Builds/{appId}/CloudDir/ChunksV4/{group}/{hash}_{guid}.chunk` with `Host: download.epicgames.com` (or per-config preferred CDN). Stream-discard identical.

### 5.5 Lancache nginx (compose peer)

| Aspect | Detail |
|---|---|
| **What we send** | All Steam/Epic CDN GETs (redirected via `Host:` header); heartbeat probe (`GET /lancache-heartbeat`) at startup and every 30s thereafter |
| **What we receive** | `X-LanCache-Processed-By: <version>` header on heartbeat; proxied CDN bodies via stream-discard |
| **Auth type** | None (internal compose network) |
| **Fallback when down** | **Fatal for the orchestrator's purpose.** Startup: `/api/v1/health` exposes `lancache_reachable=false` and returns 503. Ongoing: prefills fail; re-check heartbeat before every cycle. |
| **Data we persist from it** | Heartbeat header value (for diagnostics); no other data. |
| **Data sensitivity** | Public (all traffic is standard HTTP through a LAN cache) |

### 5.6 Lancache filesystem (read-only bind mount)

| Aspect | Detail |
|---|---|
| **What we send** | `os.stat()` syscalls on predicted paths (F7) |
| **What we receive** | Stat struct (file exists / size / mtime) |
| **Auth type** | Unix filesystem permissions (read-only mount) |
| **Fallback when down** | If `CACHE_ROOT` not mounted: `/api/v1/health` exposes `cache_volume_mounted=false`; all validations return `outcome='error'`. |
| **Data we persist from it** | Nothing directly; validation outcomes are derived. |
| **Data sensitivity** | Public (chunk files are public CDN content) — but **filesystem paths are not exposed in the REST API**. |

### 5.7 Game_shelf (outbound — proxied calls back to us)

The orchestrator does NOT make outbound calls to Game_shelf. Integration is strictly one-directional: Game_shelf calls the orchestrator's REST API. If this ever changes (e.g., an eventual push-notification from orchestrator to Game_shelf), it's a new integration that requires revising this contract.

---

## 6. Data Persistence

### 6.1 What persists across sessions

**SQLite DB** at `${STATE_DIR}/state.db` (default `/var/lib/orchestrator/state.db`). Schema per Brief §5 + numbered `.sql` migrations in-repo. Tables (all created in `migrations/0001_initial.sql` per DQ2 resolution 2026-04-20): `platforms`, `games`, `manifests`, `block_list`, `validation_history`, `jobs`, `cache_observations` (the last populated only when Post-MVP access-log tail ships, but schema is present now so no later migration is required to introduce it).

**Referential integrity (DQ8 resolution 2026-04-20):** the `games.platform` foreign key is declared with explicit `ON DELETE RESTRICT`. Platform rows are effectively an enum and are never deleted in MVP; the RESTRICT clause ensures any future code that accidentally attempts `DELETE FROM platforms` hits a referential-integrity error rather than silently cascade-deleting every game.

**Session files** at `${STATE_DIR}/`:
- `steam_session.json` — mode 0600. Contents: `{"refresh_token": "...", "cell_id": n, "persisted_at": "ISO8601", "expires_hint": "ISO8601|null"}`. Secure handling per §7.
- `epic_session.json` — mode 0600. Contents: `{"access_token": "...", "refresh_token": "...", "expires_at": "ISO8601", "persisted_at": "ISO8601"}`.

**Container image metadata** — Docker secret files at `/run/secrets/*` are managed by the operator, not the orchestrator. Their lifecycle is outside the contract.

### 6.2 What is ephemeral (in-memory only)

- httpx connection pool and open streams.
- asyncio Semaphore state (prefill concurrency bound).
- APScheduler `MemoryJobStore` — the single recurring cron re-registered at every startup from config (this is intentional per Brief §3.5).
- Depot keys fetched from Steam CM during a prefill (used once per cycle, discarded when the CDNClient object is garbage-collected).
- Manifest request codes cached per-depot for 4.5 minutes.
- Correlation IDs propagated through async context for a single request/job.
- CLI interactive prompt state (username, password, 2FA code, auth code — all cleared from memory on CLI exit).

### 6.3 Expected data volumes

Projections over the first 12 months of operation:

| Entity | 12-month estimate | Storage estimate | Growth rate |
|---|---|---|---|
| `games` rows | ~2,600 (user's library is stable) | ~1 MB | Flat with occasional purchases |
| `manifests` rows | ~2,600 (latest per game, older versions pruned) | ~500 MB (compressed raw manifests; average ~200 KB each) | Grows when games update |
| `platforms` rows | 2 | < 1 KB | Constant |
| `block_list` rows | < 100 (most users block a dozen games) | < 10 KB | Slow |
| `validation_history` rows | ~15 per game per month × 2,600 games × 12 months ≈ **470,000** | ~100 MB | Linear |
| `jobs` rows | ~8 per game per month (sync + prefill + validate + sweep) × 2,600 × 12 ≈ **250,000** | ~50 MB | Linear |
| `cache_observations` rows (Post-MVP access-log tail) | ~0 in MVP | 0 | N/A in MVP |

**Total DB size ceiling:** < 1 GB at 12 months. Vacuuming monthly (F11 CLI `db vacuum`) keeps fragmentation low.

**Pruning rules (Phase 2):**
- `validation_history`: keep last 90 days per game.
- `jobs`: keep last 90 days of `succeeded`/`failed` rows; keep indefinitely for `error` rows with non-null `error` field.
- `manifests`: keep latest N (default 3) versions per game.
- `cache_observations`: keep last 30 days (Post-MVP only).

### 6.4 Backup and recovery

Per Intake §5.4: **weekly `sqlite3 state.db ".backup /backup/orchestrator-YYYYMMDD.db"` via external cron** on the DXP4800 host. Backup includes the session files (`*.json`).

- **In scope for MVP:** Document the backup command in the deployment guide (Phase 4).
- **Out of scope for MVP:** Automated backup verification, point-in-time-recovery, off-host backup replication. Document in Phase 4 handoff as future hardening.

### 6.5 Data retention

| Category | Retention policy |
|---|---|
| Active state (`games`, `platforms`, `block_list`) | Keep forever; never auto-prune |
| Historical state (`validation_history`, `jobs`) | Prune at 90 days; do not auto-prune rows with truthy `error` fields |
| Raw manifests (`manifests.raw`) | Keep latest 3 versions per game |
| Session tokens (`*.json`) | Keep until next successful rotation or operator revocation |
| Docker secrets (`/run/secrets/*`) | Operator-managed; rotate when operator rotates |
| Structured logs | Container stdout; retained per Docker's log-driver configuration (operator concern) |

---

## 7. PII & Credential Handling

**The orchestrator handles credentials but stores no PII in the traditional sense.**

| Data | PII? | Handling |
|---|---|---|
| Steam username | Not PII by itself (could be a pseudonym), but **may be the user's real name or a personally-identifiable handle** | Held in RAM during CLI auth only. **Never logged. Never persisted.** Passed directly to steam-next. |
| Steam password | Credential, not PII | Held in RAM during CLI auth only. **Never logged. Never persisted.** |
| Steam Guard code | Credential, OTP | Same. |
| Steam refresh token | Credential | Persisted at `steam_session.json` mode 0600. Not exposed via REST API. Logged only as SHA256 prefix. |
| Epic auth code | Credential, short-lived | RAM-only, never persisted. |
| Epic refresh token | Credential | Persisted mode 0600. Not exposed via API. SHA256-prefix in logs. |
| Bearer API token | Credential | Docker secret. SHA256-prefix in logs. Timing-safe comparison in code. |
| Game titles | **Internal.** Can reveal what the operator owns, which for a single-user system is mildly private but not regulated. | Returned by REST API (to Game_shelf and status page). Not logged except in DEBUG. |
| Cache paths | Internal | Computed, not stored. Never returned in REST API responses (only counts and outcomes). |

**Hard rules (every feature):**
1. No credential ever appears in a log line at any level.
2. No credential ever appears in a REST API response body or header.
3. Authorization headers are redacted in logs (`Bearer <redacted>`).
4. Credential comparison uses `hmac.compare_digest` (timing-safe).
5. Secrets pass to child processes (CLI subprocesses) only via stdin or env, never command-line arguments.
6. On container SIGTERM: flush pending logs (no credential flush needed — not in memory), close DB, exit cleanly.

**Sensitivity classifications applied (summary):**
- **Confidential:** All credentials, tokens, secrets (items §2.2, §2.4, §2.5 auth endpoints, §5.1/§5.3 refresh tokens)
- **Internal:** Game library metadata, cache paths, job state, config (everything else in RAM/DB)
- **Public:** Game binary content (lives in Lancache, not the orchestrator) and upstream heartbeat-header values
- **PII / Financial / Health / Regulated:** None. The orchestrator does not handle any of these.

---

## 8. Data Flow (end-to-end)

Explicit map of how data moves from input → storage → output across a full cycle.

```
          ┌──────── Docker secrets (IS1) ─────────┐
          │                                        │
          ├──► orchestrator_token (startup)        │
          │    └─► bearer-auth middleware (F9)     │
          │                                        │
          └──► steam_webapi_key (optional)         │
               └─► F3 fallback path                │

          ┌──────── Env vars (IS2) ──────────────┐
          │                                        │
          └──► pydantic-settings → Settings object │
               └─► used by every feature           │

          ┌──────── CLI stdin (IS3) ─────────────┐
          │                                        │
          ├──► Steam username/pw/guard ───►      │
          │    steam-next.SteamClient.login()      │
          │    └─► refresh token → session file (IS6)
          │    └─► platforms.steam row (DB)         │
          │                                        │
          └──► Epic auth code ───►               │
               legendary.core.auth_code()           │
               └─► tokens → session file (IS6)      │
               └─► platforms.epic row (DB)          │

          ┌──────── Scheduler tick (T9) ──────────┐
          │                                        │
          ▼                                        │
   F12 sync cycle                                  │
     │                                             │
     ▼                                             │
   F1/F2 refresh ──► session files (IS6)           │
     │                                             │
     ▼                                             │
   F3/F4 enumerate ──► games rows (DB)             │
     │     └─► T8 title resolution (Epic)          │
     ▼                                             │
   T7 diff ──► prefill candidates                  │
     │                                             │
     ▼                                             │
   F5/F6 prefill ──► T2 manifest fetch             │
     │                    └─► manifests rows (DB)  │
     │              T3 chunk dedupe                │
     │              T4 chunk fan-out               │
     │                    └─► streams through      │
     │                        Lancache (IS5)       │
     │                    └─► body discarded       │
     │                    └─► jobs row (DB) w/ progress
     ▼                                             │
   F7 validate (auto) ──► T5 cache-key compute     │
                              └─► T6 disk-stat     │
                                    └─► validation_history (DB)
                                    └─► games.status update

          ┌──────── REST API ingress (IS4) ──────┐
          │                                        │
          ▼                                        │
   bearer-auth ──► 401 if missing/wrong            │
     │                                             │
     ▼                                             │
   127.0.0.1 check (POST /platforms/*/auth only)   │
     │                                             │
     ▼                                             │
   Pydantic validation ──► 400 if malformed        │
     │                                             │
     ▼                                             │
   handler ──► read/write DB ──► response (output)

          ┌──────── Outputs ──────────────────────┐
          │                                        │
          ├──► /api/v1/* JSON responses            │
          │    └─► Game_shelf backend proxy (F14)  │
          │    └─► browser status page (F10)       │
          │    └─► CLI (F11)                       │
          │                                        │
          ├──► status.html at GET /                │
          │                                        │
          └──► structlog JSON on stdout            │
               └─► Docker logs (operator surface)
```

---

## 9. Open Questions — Resolved by Orchestrator 2026-04-20

### DQ1. `steam_webapi_key` in MVP — **RESOLVED: drop**
**Decision.** The Steam Web API key optional fallback is removed from MVP entirely. Intake §5.1 row removed; Data Contract §2.2 row removed; Brief §3.2 "single CM login" becomes the only path. Post-MVP: re-add if a user ever wants a "library-only, no CDN prefill" mode.

### DQ2. `cache_observations` table in initial migration — **RESOLVED: create now**
**Decision.** Orchestrator overrode the "defer" recommendation. The table ships in `migrations/0001_initial.sql` even though MVP does not populate it (access-log tail is Post-MVP v1.1). Rationale (accepted): avoid a later schema-change migration when the access-log feature ships; empty-table cost is trivial.

### DQ3. `manifests.raw` storage — **RESOLVED: BLOB in SQLite**
**Decision.** Matches Brief §5 schema. Single-file backup, simpler migration, no external filesystem layout to manage. Revisit if `VACUUM` on the manifests table exceeds 5 s at 12-month data volume.

### DQ4. Log retention ownership — **RESOLVED: Docker logging driver owns retention**
**Decision.** Orchestrator contract ends at `sys.stdout`. Phase 4 `HANDOFF.md` must include a section: "The orchestrator writes JSON logs to stdout. Log rotation and retention are controlled by the Docker logging driver (e.g., `json-file` with `max-size=50m` and `max-file=5` is a sensible default). Configure in your `docker-compose.yml`'s `logging:` block."

### DQ5. Audit trail for `block_list` mutations — **RESOLVED: accept current schema**
**Decision.** `source` column + structured logs are sufficient for single-user operation. No soft-delete columns. If this ever becomes multi-user, revisit.

### DQ6. Mutation response envelope — **RESOLVED: normalize to `{"ok", "job_id", "message"}`**
**Decision.** Every mutation endpoint (F8 block/unblock, F5/F6 prefill triggers, F7 validate triggers) returns the same envelope shape. `job_id` present for actions that queue a job; null for synchronous ones. `message` optional. Ship in Phase 2 F9 implementation. FRD F9 table annotated; §4 outputs table updated above.

### DQ7. Manifest size response cap — **RESOLVED: 128 MiB, configurable**
**Decision.** Default `MANIFEST_SIZE_CAP_BYTES = 128 * 1024 * 1024`. Env-var configurable between 1 MiB and 1 GiB. §2.6 rule 3 updated above. §2.3 env-var table to be updated at Phase 1 Project Bible time (adding it now would require changing the Intake; the env var is a Phase 1 architecture artifact).

### DQ8. Platform FK `ON DELETE RESTRICT` — **RESOLVED: yes, explicit**
**Decision.** Explicit clause in `migrations/0001_initial.sql`. §6.1 updated above with the rationale.

---

## 9a. Carry-Forward Notes

- **DQ1 propagates to Intake §5.1:** drop the "Steam Web API key (optional fallback)" row. Edit scheduled next.
- **DQ2/DQ8 are schema-specifics** that flow into `migrations/0001_initial.sql` at Phase 2. Record in Phase 1 Project Bible as part of the data-model section.
- **DQ6** affects FRD F9 endpoint descriptions (response bodies). Apply a small annotation to the F9 table referencing DQ6.
- **DQ7** is an env var not currently listed in the Data Contract §2.3 table. Add at Phase 1 time (Project Bible is the canonical env-var spec).

---

## 9b. State Boundaries (canonical format per `data-contract.tmpl`)

| Data | Lifecycle | Persistence | Backup Required |
|------|-----------|-------------|-----------------|
| `platforms` rows | Created at first migration; updated on every auth/sync | SQLite `/var/lib/orchestrator/state.db` | Yes (weekly `.backup`) |
| `games` rows | Created on first library enumeration per app; updated on diff/prefill/validate | SQLite | Yes |
| `manifests` rows (incl. `raw` BLOB) | Created on manifest fetch; pruned to latest 3 versions per game | SQLite | Yes |
| `block_list` rows | Created on block request; deleted on unblock | SQLite | Yes |
| `validation_history` rows | Created on every validation run; pruned at 90 days | SQLite | Yes (retention may be short post-prune) |
| `jobs` rows | Created on every job enqueue; pruned at 90 days except `error` rows | SQLite | Yes |
| `cache_observations` rows (Post-MVP populate) | Created by access-log tail; pruned at 30 days | SQLite | Yes |
| `steam_session.json` | Created on first Steam auth; rotated on refresh | Host filesystem, `/var/lib/orchestrator/*.json` mode 0600 | Yes |
| `epic_session.json` | Created on first Epic auth; rotated on refresh | Same | Yes |
| Docker secrets (`orchestrator_token`) | Operator-managed; never touched by orchestrator | Mounted at `/run/secrets/*` | Operator concern (separate from our backup) |
| Correlation IDs, httpx connections, semaphore state | Created per request/job; destroyed on task completion | RAM only | No — ephemeral by design |
| Depot keys, manifest request codes | Created during a prefill cycle; discarded at cycle end | RAM only; manifest-code cached 4.5 min | No |
| APScheduler jobs | Registered at container start from config | `MemoryJobStore` — in-RAM | No — re-registered at startup |

---

## 9c. Sensitivity Classification Summary (canonical format per `data-contract.tmpl`)

| Classification | Data Items | Handling Requirements |
|---------------|------------|----------------------|
| **PII** | None | N/A — orchestrator handles no PII in the regulated sense |
| **Confidential (credentials)** | Steam username/password/Steam-Guard code; Epic auth code; Steam refresh token; Epic refresh + access tokens; bearer API token; (Post-MVP would include: Steam Web API key — **removed from MVP by DQ1**) | RAM-only during CLI flows; persisted tokens at mode 0600; never logged (SHA256 prefix only); never exposed in REST API responses; timing-safe comparison for API token |
| **Internal** | Game library metadata (titles, app_ids, depot_ids, manifest GIDs, sizes); job state; validation history; block list; scheduler state; cache path formulas | Returned via bearer-authenticated REST API; logged at INFO/DEBUG without restriction; not disclosed outside the LAN trust boundary |
| **Public** | Game binary content (lives in Lancache, not in orchestrator process memory); Lancache heartbeat header values; orchestrator version/build metadata exposed at `/api/v1/health` | No restrictions |
| **Financial / Health / Medical / Regulated** | None | N/A |

---

## 10. Review Checklist (per Builder's Guide Step 0.3)

- [x] Every input has validation rules and sensitivity classification — ✅ all of §2 covers IS1–IS6
- [x] Every third-party dependency has a fallback behavior — ✅ §5 table per integration
- [x] PII fields identified — ✅ §7 confirms none-handled; credentials inventoried
- [x] Transformations discrete and failure-mode'd — ✅ §3 T1–T12
- [x] Outputs have consumers and latency targets — ✅ §4 table
- [x] Persistence boundary (stored vs ephemeral) defined — ✅ §6

---

## 11. Sign-off

**All eight Data Questions resolved 2026-04-20.** Data Contract frozen for Phase 0.

**Downstream edits triggered by resolutions:**
- Intake §5.1: drop "Steam Web API key (optional fallback)" row (DQ1).
- FRD F9 endpoint table: annotate mutation responses with the normalized envelope (DQ6).
- `migrations/0001_initial.sql` (Phase 2 artifact): includes `cache_observations` table (DQ2); declares `games.platform` FK as `ON DELETE RESTRICT` (DQ8); `manifests.raw` BLOB column (DQ3); populates `block_list` with the schema as specified (DQ5).
- Phase 1 Project Bible env-var table: includes `MANIFEST_SIZE_CAP_BYTES` (DQ7 default 128 MiB, range 1 MiB – 1 GiB).
- Phase 4 HANDOFF.md (template-generated): includes log-retention guidance (DQ4).

**Next Phase 0 step:** 0.4 — Product Manifesto synthesis.
