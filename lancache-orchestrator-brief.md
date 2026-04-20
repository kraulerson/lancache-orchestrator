# Lancache Cache Orchestrator — Project Brief

**Date:** 2026-04-19
**Revision:** 3 (dropped second SPA; simplified DB and config layers; added title resolution and event-loop discipline sections).
**Status:** Technical evaluation complete, ready for project planning.

---

## Executive Summary

A native Python replacement for the SteamPrefill/EpicPrefill toolchain is **feasible and recommended** for Steam and Epic. It is **not recommended** for Ubisoft Connect, EA App, or GOG Galaxy — each fails on different grounds (caching broken upstream, no usable protocol library, or traffic that Lancache fundamentally cannot intercept). The validator reads Lancache's cache state directly from disk (cheap, deterministic, authoritative) rather than trusting any flat-file tracker.

**Architecturally, the orchestrator is a fully autonomous service that runs on the DXP4800 alongside Lancache in the same Docker Compose stack.** It owns its own SQLite database, its own scheduler, and its own platform authentication. It has **zero runtime dependency** on Game_shelf or anything outside the DXP4800. Downloads originate on the DXP4800's 2.5GbE path and flow through Lancache locally — never through Game_shelf's 1GbE uplink.

The **rich user interface lives entirely in Game_shelf** (existing system on the ThinkStation, Proxmox node `ferrumcorde`). The orchestrator ships only a CLI for auth flows and a single-file HTML status page for diagnostics when Game_shelf is unreachable. There is no second React SPA to maintain. Game_shelf proxies cache state from the orchestrator's REST API into its library UI and forwards user actions back. Either side going offline is survivable; neither blocks the other.

Build order: **Steam first, Epic second, validator concurrent with both, REST API early, Game_shelf integration once state is stable.** Ubisoft/EA/GOG are explicitly out of scope — reasons in §9.

---

## 1. Technical Findings

### 1.1 Steam CDN — feasible in pure Python

The Python `steam` library (`ValvePython/steam`) implements the full Steam CDN download pipeline: login with 2FA, license enumeration, PICS product info, CDN server list via `IContentServerDirectoryService/GetServersForSteamPipe`, manifest request codes (`ContentServerDirectory.GetManifestRequestCode#1`), manifest download and filename decryption, and chunk download. Source locations verified in `steam/client/cdn.py` (937 lines) and `steam/client/__init__.py`.

- **Upstream is effectively dormant:** last PyPI release Dec 2022 (1.4.4), last commit May 2023, Python classifiers top out at 3.9. It still runs on 3.12 but is not tested there.
- **Active fork `fabieu/steam-next`** released 2.1.0 in Jan 2026, last push 2026-04-15. `pyproject.toml` declares `>=3.9,<4.0`, modernized deps, six/backports removed. API surface is identical to upstream. Bus-factor 1 (single maintainer, 3 stars).
- **SteamPrefill's approach is our blueprint.** It uses SteamKit2 for auth + metadata only. The actual chunk loop **bypasses SteamKit2 entirely** — raw `HttpClient` GETs to the Lancache IP with `Host:` override, body read into a 4 KB buffer and discarded (`DownloadHandler.cs:108-113`). No decryption, no disk write. Pure cache-filling.

**Steam CDN mechanics (verified):**
1. Connect to Steam CM, authenticate, receive refresh token.
2. License callback → owned packages; PICS → owned apps + depots.
3. PICS app info → depots blob with `manifests.public.gid` per depot.
4. `GetServersForSteamPipe(cellId)` → CDN server list (filter type `SteamCache`/`CDN`, `AllowedAppIds.Length == 0`).
5. Per-depot `GetManifestRequestCode(depotId, appId, manifestId, "public")` — **5-minute TTL**, refresh as needed.
6. `GET http://{cdn}/depot/{depotId}/manifest/{gid}/5/{requestCode}` → protobuf manifest (filenames AES-encrypted with depot key).
7. Dedupe chunks by SHA across the manifest's FileMapping entries.
8. Per chunk: `GET http://{lancacheIp}/depot/{depotId}/chunk/{hexSha}` with `Host: {cdnVhost}` and `User-Agent: Valve/Steam HTTP Client 1.0`. Stream-discard response.

Lancache IP is resolved via DNS on `lancache.steamcontent.com`, probing `localhost`, `172.17.0.1`, and the hostname, then verifying `GET /lancache-heartbeat` returns `X-LanCache-Processed-By` header (`LancacheIpResolver.cs`). Loop-detection 508 response is fatal-config, not transient.

### 1.2 Epic CDN — feasible, meaningfully easier than Steam

The `legendary-gl/legendary` project (5188 stars, last commit 2026-04-15, Python 3.10+) is the gold-standard open-source reimplementation of the Epic Games Launcher. It handles the entire protocol: OAuth2 auth, library enumeration, manifest URL lookup, JSON and binary manifest parsing, chunk download, AES-GCM decryption for v22+ manifests.

**Critical gotcha:** `pip install legendary-gl` gives **v0.20.9 from Sept 2021** — five years stale. Development continued on GitHub without PyPI releases. Install from git or vendor the modules.

**Files we'd vendor/use:**
- `legendary/api/egs.py` — REST client, auth, library, manifest URL. Same OAuth flow and same hardcoded public launcher credentials (`34a02cf8f4414e29b15921876da36f9a`) EpicPrefill uses.
- `legendary/models/manifest.py` — binary manifest parser (magic `0x44BEC00C`, zlib, AES-GCM secrets v22+).
- `legendary/models/json_manifest.py` — JSON manifest path converts to same internal model.
- `legendary/core.py` for `auth_code()`, `auth_sid()`, `auth_ex_token()`, `get_cdn_urls()`, `get_cdn_manifest(disable_https=True)`.

The `disable_https=True` flag is explicitly designed for DNS-based caches — already a hook for Lancache. Base URLs include `download.epicgames.com`, `cdn{1,2,3}.epicgames.com`, `cdn{1,2,3}.unrealengine.com`, Akamai, Fastly, Cloudflare fronts (14 hostnames in `uklans/cache-domains/epicgames.txt`).

**Auth UX:** Epic has no device-code flow. User pastes an auth code once from `https://legendary.gl/epiclogin`; refresh tokens rotate silently forever thereafter (10-min pre-expiry buffer).

**Chunk URL pattern at the cache layer:**
```
GET http://{lancache_ip}/Builds/{appId}/CloudDir/ChunksV4/{group}/{hash}_{guid}.chunk
Host: download.epicgames.com   (or any of the 14 CDN hostnames)
```

EpicPrefill's downloader (`DownloadHandler.cs`) sets `HttpRequestMessage.Headers.Host = upstreamCdn.Host` — identical pattern to Steam.

### 1.3 Ubisoft Connect — skip

- `uklans/cache-domains/uplay.txt` contains `*.cdn.ubi.com`.
- **`lancachenet/monolithic` issue #195 (open since July 2024) reports Ubisoft Connect downloads no longer cache.** Ubisoft moved payloads to HTTPS or a hostname outside `*.cdn.ubi.com`. No upstream fix in 21 months.
- Protocol RE exists but is incomplete: `YoobieRE/ubisoft-demux-node` (TypeScript, socket-level "Demux" protobuf protocol) and `YoobieRE/ubisoft-manifest-downloader` (Windows-only, 9 stars, WIP). Manifest schema is not public — must be dumped from the VMProtect-packed Ubisoft client.
- Months of RE work against a brittle moving target, with no cache to fill at the end.

### 1.4 EA App — skip

- `origin.txt` covers three legacy hostnames; `cache_domains.json` carries a `"mixed_content": true` warning and an "HTTP traffic only" caveat.
- Origin was retired 2025-04-17; the EA App refactor moved many endpoints to HTTPS. Community reports caching effectiveness has dropped.
- Only usable OSS effort is `ArmchairDevelopers/Maxima` (Rust, 172 stars, same author as `gogdl`/`comet`). README: *"pre-pre-pre-alpha-quality software… being made open source prematurely."* Doesn't support Battlefield 3/4 or pre-"Download-In-Place" titles.
- Hostile vendor with TLS-pinned download sessions. Not a tractable target.

### 1.5 GOG Galaxy — skip for Lancache prefill

- **GOG is not in `uklans/cache-domains` at all.** No `gog.txt`, never has been.
- GOG's CDN is `gog-cdn-fastly.gog.com` — HTTPS-only to Fastly. Lancache can't usefully intercept HTTPS without a CA-trusted MITM, which is outside its model.
- `Heroic-Games-Launcher/heroic-gogdl` (Python, MIT) is a complete, production-quality GOG downloader (OAuth, v1+v2 manifests, xdelta3 delta patching, parallel chunked download). Plug-and-play if your goal is "prefetch GOG files to local storage." **But that's not prefill.** Clients won't transparently hit a Lancache-style cache for GOG regardless of what you build.

### 1.6 Lancache validation — disk stat is authoritative

Sourced from `lancachenet/monolithic` master overlay configs:

**Cache zone** (`overlay/etc/nginx/conf.d/20_proxy_cache_path.conf`):
```
proxy_cache_path /data/cache/cache levels=2:2 keys_zone=generic:...
                 slice 1m ... use_temp_path=off
```
Two non-defaults matter: `levels=2:2` (not the common `1:2`) and the cache root is `/data/cache/cache/` (doubled — the volume is `/data/cache`, nginx writes into a `cache/` subdir).

**Cache key** (`30_cache_key.conf`):
```
proxy_cache_key  $cacheidentifier$uri$slice_range;
```
where `$cacheidentifier` is mapped in `30_maps.conf`:
```
map "$http_user_agent£££$http_host" $cacheidentifier {
    default $http_host;
    ~Valve\/Steam\ HTTP\ Client\ 1\.0£££.* steam;
}
```
**Steam content keys on the literal string `steam`, not the host.** Everything else keys on `Host:`. Every object is split into 1 MiB slices (`slice 1m`) and each slice is a separate cache entry.

**On-disk path formula** (with `levels=2:2`): `md5(key) = H`, file lives at
```
/data/cache/cache/<H[28:30]>/<H[30:32]>/<H>
```
Nginx slices the *last* bytes first for the directory levels. Community guides that assume `levels=1:2` are wrong for Lancache.

**Response header:** `X-Upstream-Cache-Status` (values: `HIT | MISS | BYPASS | EXPIRED | STALE | UPDATING | REVALIDATED`). `X-LanCache-Processed-By` identifies traffic that went through Lancache.

**Key side-effect warning:** Any HEAD probe on a missing chunk triggers an upstream fetch — you can't probe read-only. `?nocache=1` returns `BYPASS`, useless for validation. Disk stat is the only truly side-effect-free probe.

**Validation cost per game:** Lancache reslices to 1 MiB at the proxy regardless of upstream chunk size, so a 50 GB game ≈ 51,200 cache entries, 100 GB ≈ 102,400. Disk `stat()` on ~50k files takes 50–500 ms single-threaded. Exhaustive validation is cheap. HEAD probing exhaustively is 1–2 s at 64-way concurrency but triggers upstream fills on misses.

---

## 2. System Architecture

### 2.1 Topology and autonomy

Two physical hosts, two independent services:

- **DXP4800** (Ugreen NAS, 2.5GbE, 57TB, ~12TB cache used) — runs **Lancache** and **orchestrator** in the same Docker Compose stack. Downloads originate here and stay on 2.5GbE.
- **Lenovo ThinkStation P360 Tiny** (Proxmox node `ferrumcorde`, 192.168.1.20, 1GbE) — runs **Game_shelf** in an LXC container. Existing system, pre-dates this project.

The orchestrator is **fully autonomous**. It owns its own SQLite DB, its own scheduler (node-cron equivalent), its own platform auth, and its own REST API. It does not call Game_shelf for anything at runtime. Game_shelf going down — for Proxmox maintenance, reboot, or a bug — does not stop or degrade caching.

Game_shelf is a **display and command passthrough**. It calls the orchestrator's REST API to render cache state in its existing library UI, and proxies user actions (block/unblock/force-prefill/validate) back to the orchestrator. If the orchestrator is unreachable, the library UI still works — cache columns simply show "orchestrator offline."

```
┌──────────────────────────────────────────────────────────────────────┐
│  Game_shelf  (Lenovo ThinkStation P360, Proxmox LXC, 1GbE)           │
│                                                                      │
│  Existing Express 5 backend (port 3001)                              │
│    ├── /api/{auth,setup,launchers,games,sync,metadata,tags}          │
│    └── NEW: /api/cache/*  (proxy routes → orchestrator API)          │
│                                                                      │
│  Existing React frontend (Vite, TanStack Query, Tailwind)            │
│    ├── pages/Library.jsx    — cache column added                     │
│    ├── pages/GameDetail.jsx — cache panel added                      │
│    └── pages/Cache.jsx      — NEW dashboard page                     │
└──────────────────────────────────────────────────────────────────────┘
                          │       ▲
                          │ HTTPS │   (bearer token, 1GbE path)
                          ▼       │   Read-only calls for UI render +
                                      user-action POSTs (block, prefill).
                                      Game data downloads NEVER flow here.
┌──────────────────────────────────────────────────────────────────────┐
│  DXP4800 (Ugreen NAS, 2.5GbE, Docker Compose stack)                  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  orchestrator (Python 3.12, FastAPI, port 8765)                │  │
│  │    Steam adapter · Epic adapter · Validator · Scheduler        │  │
│  │    SQLite (aiosqlite, raw SQL) · own platform auth             │  │
│  │    CLI (orchestrator-cli) + single-file status.html at /       │  │
│  └─────────┬────────────────────────────────────┬─────────────────┘  │
│            │ HTTP downloads                     │ read-only          │
│            │ (Host-header override,             │ bind-mount         │
│            │  stream-discard)                   │                    │
│            ▼                                    ▼                    │
│  ┌───────────────────────┐           ┌─────────────────────────────┐ │
│  │  Lancache nginx        │◄──────────┤ /data/cache (shared volume) │ │
│  │  (docker compose peer) │           └─────────────────────────────┘ │
│  └───────────────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
                                          │
                                          │  game clients on LAN
                                          ▼
                                    (MikroTik switch,
                                     pfSense, Pi-hole DNS)
```

Key consequence of this topology: **downloads never traverse the 1GbE ThinkStation link.** The orchestrator, Lancache, and the cache volume all live on the DXP4800. A 100 GB game prefill pulls from WAN → Lancache → disk, all inside the DXP4800's Docker network.

### 2.2 Co-tenancy with Lancache

The orchestrator joins Lancache's existing `docker-compose.yml` as a sibling service. It shares the cache volume read-only (`/data/cache`) and optionally the log volume (`/data/logs`) for passive HIT/MISS observation. Lancache's network is unchanged; the orchestrator connects to it via the default compose network and resolves Lancache by service name (`http://lancache:80`) rather than IP-probing — simpler and deterministic.

A minimal addition to Lancache's compose:

```yaml
services:
  orchestrator:
    image: ghcr.io/<you>/lancache-orchestrator:latest
    restart: unless-stopped
    ports:
      - "8765:8765"            # REST API + status page
    volumes:
      - monolithic-cache:/data/cache:ro
      - monolithic-logs:/data/logs:ro
      - orchestrator-state:/var/lib/orchestrator
    environment:
      - ORCHESTRATOR_TOKEN_FILE=/run/secrets/orchestrator_token
      - LANCACHE_HOST=lancache           # compose service name
      - CACHE_ROOT=/data/cache/cache
    depends_on:
      - lancache
    secrets:
      - orchestrator_token

volumes:
  orchestrator-state:

secrets:
  orchestrator_token:
    file: ./secrets/orchestrator_token.txt
```

---

## 3. Component Design

### 3.1 Platform adapters (orchestrator)

Each adapter is a Python module implementing a small interface:

```python
class PlatformAdapter(Protocol):
    name: str  # "steam" | "epic"
    async def authenticate(self, *, interactive: InteractiveChannel | None = None) -> AuthResult: ...
    async def refresh_token_if_needed(self) -> None: ...
    async def enumerate_library(self) -> list[OwnedApp]: ...
    async def get_manifest(self, app: OwnedApp) -> Manifest: ...
    async def prefill(self, manifest: Manifest, on_progress: Callable) -> PrefillResult: ...
```

**Steam adapter:**
- Use `fabieu/steam-next` via `SteamClient` / `CDNClient`, pinned to a git SHA.
- Keep gevent isolated — run the `SteamClient` in a dedicated thread with `gevent.monkey.patch_minimal()`, bridge to asyncio via `run_in_executor`. Everything else (HTTP fan-out, API, DB) stays on asyncio.
- CellID captured from login callback, persisted; stale cellID means far-away CDNs and worse Lancache routing.
- Chunk HTTP fan-out uses `httpx.AsyncClient` with `asyncio.Semaphore(32)`. Stream-discard pattern:
  ```python
  async with client.stream("GET", f"http://{lancache_host}/depot/{depot_id}/chunk/{sha_hex}",
                           headers={"Host": cdn_vhost, "User-Agent": STEAM_UA}) as r:
      async for _ in r.aiter_raw(chunk_size=65536): pass
  ```
- Manifest request codes cached per-depot with 4.5-minute TTL (under the 5-min server limit).

**Epic adapter:**
- Vendor `legendary/api/egs.py`, `legendary/models/manifest.py`, `legendary/models/json_manifest.py`. Lift just the auth + manifest bits into our own thin coordinator.
- Same HTTP fan-out pattern as Steam, but URL is `http://{lancache_host}/{chunk.path}` with `Host:` set to the chosen CDN hostname from `base_urls`.
- Pin a preferred CDN host (e.g., `download.epicgames.com`) via legendary's `preferred_cdn` mechanism so cache-key locality is predictable.

### 3.2 Per-platform authentication (orchestrator owns it)

The orchestrator authenticates **independently** to each platform — it does not reuse Game_shelf's tokens, sessions, or credential store. Reasons:
1. Autonomy — the orchestrator keeps working if Game_shelf is down or its DB is reset.
2. Different scopes — Game_shelf needs public catalogue metadata and user library reads; the orchestrator also needs CDN-level access to download content.
3. Different lifetimes — CDN download sessions are long-lived and stateful; library enumeration can be short-lived API calls.

**Steam (two separate auth surfaces):**

| Purpose | Mechanism | Persistence | User action |
|---|---|---|---|
| Library enumeration (what games are owned) | Steam CM login via `steam-next` (username/password + Steam Guard) yields the license callback → PICS. Alternative: Steam Web API `IPlayerService/GetOwnedGames` with an API key. | Refresh token in `/var/lib/orchestrator/steam_session.json` (CM path). Or API key in config. | One-time login OR one-time API key paste. |
| CDN content download | Same Steam CM session as above — the CM login yields depot keys and manifest request codes. | Shared with library-enumeration session. | Same. |

Preferred path: **single CM login** drives both library and CDN. Steam Web API key is a fallback for library enumeration if we want faster/simpler library refresh without the full CM handshake (e.g., checking for new purchases every 6 hours without reconnecting). Orchestrator supports both, configurable.

**Epic (single auth surface):**

| Purpose | Mechanism | Persistence | User action |
|---|---|---|---|
| Library enumeration | EGS REST API with OAuth bearer (from legendary's auth_code flow). | Refresh token in `/var/lib/orchestrator/epic_session.json`. | One-time: paste auth code from `https://legendary.gl/epiclogin`. |
| CDN content download | Same EGS OAuth session — returns manifest URLs and CDN base URLs. | Shared. | Same. |

Same token covers both library + CDN. Tokens rotate silently after first use.

**Auth UX exposure:**
- Orchestrator status page surfaces per-platform auth state with a pointer to the CLI command to reconnect (`orchestrator-cli auth steam` / `auth epic`).
- Game_shelf's Cache tab shows the same auth state and an amber banner when a reconnect is needed, with explicit instructions to run the CLI on the DXP4800 host.
- Credentials are entered at the CLI on the DXP4800 itself — never through Game_shelf's frontend, never through a browser form served by the orchestrator. This keeps the credential surface tight and avoids the "web form collects a Steam password" class of risk.

### 3.3 Game title resolution

The orchestrator resolves display titles from **platform metadata only** and persists them on `games.title` at library-sync time. No runtime dependency on Game_shelf for titles — preserving the autonomy we spent §2 establishing.

- **Steam:** `PICS.apps[app_id].common.name` is always populated, stable, and matches what users see in the Steam client. `steam-next` returns it as part of `get_product_info(apps=[app_id])`.
- **Epic:** the `launcher-public-service/assets` endpoint returns opaque asset records with codename-style identifiers, but chaining through `catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared/namespace/{ns}/bulk/items?id={catalogItemId}` returns `title` — EpicPrefill, legendary, and the Epic Games Launcher all use this exact chain. Pinned to library-sync time, so the call cost is once per owned app per cycle, not per-request.

**Fallback:** if both the live platform title and our cached title are missing (e.g., newly-entitled item mid-sync, transient API error), the API and status page render `{platform}:{app_id}` and log a warning. Full re-resolution happens on the next 6-hour sync.

**Explicitly rejected:** querying Game_shelf for titles. It would add a runtime cross-host dependency for cosmetic output, and Game_shelf's titles come from IGDB enrichment — the same platform title we already have, laundered through another system. No information gain, autonomy loss.

### 3.4 Cache validator

Pure-function mapping `(platform, app_id, manifest) → ValidationReport`.

Primary mechanism: **disk stat**. For each cache entry predicted from the manifest (expanded to 1 MiB slices):
1. Build the exact request URL + `Host:` the adapter would use.
2. For Steam: compute cache_key = `"steam" + uri + slice_range`. For Epic: `http_host + uri + slice_range`.
3. MD5 the key. Stat `/data/cache/cache/<md5[28:30]>/<md5[30:32]>/<md5>`.
4. File exists + non-empty = cached. Optional deeper check: read first ~1 KiB, confirm the embedded plaintext `KEY: ...` line matches (detects collisions).

Self-test at startup: issue one HEAD probe against a chunk we know is cached (last successful prefill), verify we get `HIT` and the computed disk path exists. Fail loudly if the formula has drifted.

Optional passive layer: tail `/data/logs/access.log` (configured `NGINX_LOG_FORMAT=cachelog-json`) and update a last-seen timestamp per cache entry. Useful for detecting eviction drift.

**Not using:** exhaustive HEAD probing (side-effectful on misses), nginx admin API (doesn't exist; only `stub_status` traffic counters), manifest-cached-as-proxy (false positives on partial downloads).

### 3.5 Orchestrator core

- Scheduler: `APScheduler` with a 6-hour cron trigger (configurable) on a "check-all" job. Per-game jobs are ephemeral, created when work is needed. **`MemoryJobStore`** — the only scheduled job is the single recurring cron, re-registered at container start from config. No persistent jobstore needed (would otherwise drag in SQLAlchemy, which we've dropped — see §6). Per-game work is tracked in our own `jobs` table, not APScheduler's internal state.
- State machine per game: `unknown → cataloged → manifest_fetched → prefilling → validating → cached | stale | blocked | failed`. Transitions are audited in `validation_history`.
- Diff logic: for each owned app, compare `manifest.build_id` / `manifest.gid` against the last successfully cached version. Enqueue prefill if different. Treat validation-failed entries (chunks missing despite believed-cached) the same as stale.
- Concurrency: one prefill at a time per platform by default (Lancache bandwidth is the bottleneck, and concurrent prefills from the same Steam login trip rate limits). Expose as config.
- Block list honored at enqueue time, never silently.

### 3.6 Event loop discipline (API responsiveness under load)

A 100 GB prefill at 300 Mbps runs for ~45 minutes. During that window the REST API must stay responsive (Game_shelf is polling; users may trigger validations or block actions). The design keeps three work zones cleanly separated on a single process:

1. **Main asyncio event loop — async-native only.** FastAPI handlers, httpx chunk fan-out (`async with client.stream(...)` + `aiter_raw`), SQLite queries via `aiosqlite`, APScheduler triggers. Every operation here either awaits I/O or yields promptly. The 32-concurrency chunk fan-out does cooperative reads on each 64 KB buffer — latency on any one socket doesn't block the others or the API. This is exactly the pattern FastAPI reverse proxies run at multi-Gbps.
2. **Steam CM session — dedicated thread with gevent monkey-patch.** `steam-next`'s `SteamClient` uses gevent for its internal I/O. It runs in a worker thread launched at startup; the event loop talks to it only via `loop.run_in_executor(steam_thread_pool, fn)`. Gevent never touches the main loop, and the main loop never calls sync Steam code directly.
3. **Validator and disk-stat bursts — default thread pool.** `os.stat()` for ~50k files is blocking kernel I/O; `hashlib.md5` on cache keys is CPU. Both run in `loop.run_in_executor(None, ...)` batches, typically 256-file chunks. The default thread pool (`ThreadPoolExecutor(max_workers=min(32, cpu_count+4))`) handles this without starving the API.

What could go wrong, and the guards:
- A forgotten `requests.get` or other sync call on the main loop would block everything. Enforce with a pre-commit grep + a CI lint rule rejecting `requests`, `urllib`, and `sqlite3` imports in non-executor code paths.
- An accidental `time.sleep` in a FastAPI handler. Same lint rule.
- An extremely long chunk download stalled on upstream would tie up one httpx connection; the semaphore still grants 31 others and the API stays responsive. 10-second read timeout per chunk prevents indefinite stalls.

**Spike F (new):** under-load API responsiveness test. Target: `GET /api/health` p99 < 100 ms with 32 concurrent chunk downloads sustaining ≥300 Mbps to Lancache. Run before committing the asyncio-only design; if it fails, subprocess isolation for the downloader is the fallback.

### 3.7 API layer (orchestrator)

FastAPI, mounted at `/api`, served on port 8765. The REST API is the primary interface — consumed by Game_shelf's Express backend (server-to-server proxy), by the orchestrator's built-in CLI (for local ops), and by the single-file HTML status page (for at-a-glance diagnostics). There is no second React SPA.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + version + uptime |
| GET | `/api/games` | list with platform, status, last validated, block state, cached bytes |
| GET | `/api/games/{platform}/{app_id}` | detail + validation history |
| POST | `/api/games/{platform}/{app_id}/validate` | trigger validation |
| POST | `/api/games/{platform}/{app_id}/prefill` | force prefill (respects block list) |
| POST | `/api/games/{platform}/{app_id}/block` | add to block list |
| DELETE | `/api/games/{platform}/{app_id}/block` | remove from block list |
| GET | `/api/platforms` | per-platform status, auth expiry, last sync |
| POST | `/api/platforms/{name}/auth` | submit Steam credentials / Epic auth code (orchestrator-only — Game_shelf forwards the UI but never the credentials) |
| GET | `/api/jobs` | active + recent jobs with progress |
| GET | `/api/jobs/{id}` | single job detail |
| GET | `/api/events` (WebSocket or SSE) | live progress stream |
| GET | `/api/stats` | cache size, HIT/MISS aggregates from log tail, disk free, LRU headroom |
| GET | `/api/config` | non-secret config snapshot (schedule, parallelism, cache path) |

**Auth on the API:** single bearer token in header `Authorization: Bearer <token>`, stored in a Docker secret (`/run/secrets/orchestrator_token`). LAN-only service — no OAuth, no user accounts. Game_shelf's Express backend holds the same token as an env var and injects it on proxy calls; Game_shelf's frontend never sees it. The status page at `/` reads the token from a browser-side prompt (stored in `sessionStorage`) — good enough for a single-user diagnostic tool.

**CORS:** default deny. Explicit allowlist of Game_shelf's origin (`http://gameshelf.local`, `http://192.168.1.20`, etc.) configurable via env var. Direct browser access works at `http://<dxp4800>:8765/`.

**Stability:** path and payload schemas are versioned in `/api/v1/...` (aliased under `/api/` for now). Breaking changes require a new prefix. Keeping Game_shelf unbroken across orchestrator upgrades is the primary contract discipline.

### 3.8 Operator surface — CLI and single-file HTML status page

The orchestrator's own user interface is deliberately minimal. **No second React SPA.** Operations happen either through Game_shelf (rich UI, Phase 3), the CLI (auth flows and local ops), or the HTML status page (diagnostics when Game_shelf is down).

**CLI: `orchestrator-cli`** — a Click-based command bundled in the same container image. Runs with `docker compose exec orchestrator orchestrator-cli <subcommand>`. Hits the local REST API over the compose network with the same bearer token (read from the Docker secret, not stored elsewhere).

| Subcommand | Purpose |
|---|---|
| `auth steam` | Interactive Steam login: username → password → Steam Guard code (if prompted). Persists refresh token. |
| `auth epic` | Prompt for Epic auth code pasted from `https://legendary.gl/epiclogin`. Exchanges + persists. |
| `auth status` | Show per-platform auth state and expiry. |
| `library sync [--platform X]` | Force a library enumeration now. |
| `game <platform>/<app_id>` | Show full state for one game. |
| `game <platform>/<app_id> validate | prefill | block | unblock` | Trigger action. |
| `jobs [--active]` / `jobs <id>` | List or inspect. |
| `db migrate` | Apply pending migration `.sql` files. |
| `db vacuum` | Manual SQLite VACUUM. |
| `config show` | Print effective config (env-derived). |

**Status page: `status.html`** — a single static file served by FastAPI at `GET /`. Hand-written HTML + vanilla JS + CSS, no framework, no build step, no `npm` anywhere in the orchestrator repo. Polls `/api/health`, `/api/platforms`, `/api/jobs?state=running&state=queued`, and `/api/stats` every 2 s via `fetch()` and renders:

- Orchestrator version, uptime, Lancache reachability indicator.
- Per-platform auth state (green/amber/red), last library sync, token expiry, reconnect hint pointing at the CLI command.
- Active jobs with progress bars; last 10 completed jobs.
- Last N errors from `/api/jobs?state=failed`.
- Disk usage on the cache volume and LRU headroom.

Total file size target: <20 KB. One HTML file committed verbatim in the repo. Skin it with inline CSS — Tailwind and friends are overkill here. This is diagnostics, not UX.

### 3.9 Block list / config management

- Block list is a dedicated table in the orchestrator's DB (not a flag on `games`) so we keep provenance (source, when, why).
- Matches by `(platform, app_id)` exact; wildcard patterns deferred.
- Applies to: scheduled prefill, diff-triggered prefill. Does **not** block manual validation — you may want to confirm a blocked game isn't accidentally cached.

**Config: environment variables + Docker secrets. That's it.** Two mechanisms, not four:

- **Environment variables** for all non-secret config, parsed by `pydantic-settings` into a typed `Settings` object at startup. The surface is small — `LANCACHE_HOST`, `SCHEDULE_CRON`, `PREFILL_CONCURRENCY`, `STEAM_PREFERRED_CDN`, `EPIC_PREFERRED_CDN`, `CORS_ORIGINS`, `LOG_LEVEL`, `CACHE_ROOT`, `STATE_DIR`. All declared in Docker Compose's `environment:` block (or an `.env` file alongside the compose file). `pydantic-settings` provides type coercion and validation — effectively free.
- **Docker secrets** for the bearer token (`/run/secrets/orchestrator_token`) and for platform credentials if persisted (`/run/secrets/steam_webapi_key` for the optional Web API fallback). No secrets in env vars, no secrets in the DB, no secrets in YAML.

No `config.yaml`. No layered precedence. No `.ini`. If the config needs to grow past ~20 keys, revisit then.

---

## 4. Game_shelf Integration

### 4.1 Backend additions (Express 5)

New router file `backend/src/routes/cache.js` mounted at `/api/cache` in `server.js`. All routes proxy to the orchestrator using an injected bearer token (`process.env.ORCHESTRATOR_TOKEN`) and an HTTP client (axios — already a dep). Environment additions:

```
ORCHESTRATOR_URL=http://dxp4800.local:8765
ORCHESTRATOR_TOKEN=<matches the DXP4800 secret>
ORCHESTRATOR_TIMEOUT_MS=5000
```

Routes (all require existing Game_shelf auth middleware):

| Method | Game_shelf path | Upstream |
|---|---|---|
| GET | `/api/cache/health` | `GET /api/health` |
| GET | `/api/cache/games` | `GET /api/games` (returns array, Game_shelf merges on `(platform, app_id)` with its `games` table) |
| GET | `/api/cache/games/:platform/:app_id` | `GET /api/games/{platform}/{app_id}` |
| POST | `/api/cache/games/:platform/:app_id/validate` | pass-through |
| POST | `/api/cache/games/:platform/:app_id/prefill` | pass-through |
| POST | `/api/cache/games/:platform/:app_id/block` | pass-through |
| DELETE | `/api/cache/games/:platform/:app_id/block` | pass-through |
| GET | `/api/cache/platforms` | pass-through |
| GET | `/api/cache/jobs` | pass-through |
| GET | `/api/cache/stats` | pass-through |
| GET | `/api/cache/events` | **SSE/WebSocket proxy** — optional; can also poll |

The router is thin (~150 LoC): wrap axios with an orchestrator client, handle 401 (token mismatch — log loudly, return 502 to frontend), handle timeout/refused (return 503 with `{status: "orchestrator_offline"}`), never bubble raw orchestrator 5xx to the user.

A tiny `services/orchestratorClient.js` centralizes the axios instance with base URL, timeout, and auth header. One place to change.

Critically, **no scheduled job in Game_shelf pulls cache data.** The React frontend pulls on demand via TanStack Query. If the orchestrator is slow or offline, only the cache UI degrades — Game_shelf's library/metadata/sync flows are untouched.

### 4.2 Frontend additions (React + TanStack Query + Tailwind)

Three touch points, following the repo's existing conventions:

1. **`frontend/src/components/CacheBadge.jsx` (new)** — renders a small pill: `cached` (green), `pending-update` (amber), `downloading` (blue, with spinner), `missing` (gray), `blocked` (slate), `validation-failed` (red), or `unknown` (ghost). Used inline on `GameCard.jsx` and `GameRow.jsx`.
2. **`frontend/src/components/CachePanel.jsx` (new)** — full cache detail: current version, cached version, chunk coverage %, last validated, recent jobs, action buttons (Validate, Force Prefill, Block/Unblock). Rendered in a new section inside `pages/GameDetail.jsx`.
3. **`frontend/src/pages/Cache.jsx` (new)** — standalone cache dashboard page, linked from `Nav.jsx`: overall stats (disk usage, HIT/MISS ratio, queue depth), platform auth status panels with reconnect actions, recent jobs feed, global block list management.

Data layer — add a `frontend/src/utils/cacheApi.js` with TanStack Query hooks:

```js
export function useCacheHealth() {
  return useQuery({
    queryKey: ['cache', 'health'],
    queryFn: async () => (await fetch('/api/cache/health')).json(),
    staleTime: 30_000,
    retry: false,  // orchestrator offline is a state, not an error to retry
  });
}

export function useCacheForGame(platform, appId) {
  return useQuery({
    queryKey: ['cache', 'game', platform, appId],
    queryFn: async () => {
      const r = await fetch(`/api/cache/games/${platform}/${appId}`);
      if (r.status === 503) return { status: 'orchestrator_offline' };
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    staleTime: 60_000,
  });
}
// ... plus mutations for validate, prefill, block/unblock
```

Merging strategy on `pages/Library.jsx`: a single `useQuery(['cache','games'])` fetches the orchestrator's full list once and keeps it in cache for 60 s; each `GameCard` reads its own entry from the cache map by `(platform, app_id)`. One request per library view, not one per card.

Styling: follow the repo's Tailwind + `lucide-react` icon conventions. Badge colors match existing status semantics (green/amber/red/slate).

### 4.3 Graceful degradation (both directions)

**Orchestrator offline from Game_shelf's perspective:**
- `useCacheHealth()` returns `error` or `data = undefined` within the 5 s timeout.
- `pages/Library.jsx` renders a dismissible banner: *"Cache orchestrator unreachable — cache state hidden. Library browsing is unaffected."*
- Cache badges on cards render as a neutral "—" with tooltip "Cache status unavailable."
- Mutation actions (validate, prefill, block) are disabled with tooltips pointing at the same banner.
- No throbbing retries. A manual **Retry** button on the banner re-runs the health query.

**Game_shelf offline from the orchestrator's perspective:**
- Orchestrator keeps running unchanged. Scheduled prefills fire. Validations complete. Disk fills.
- When Game_shelf comes back, it pulls fresh state via `GET /api/cache/games` and reconciles.
- The orchestrator's status page at `http://<dxp4800>:8765/` and the CLI on the DXP4800 host remain fully functional as a fallback ops surface.

**Version skew:**
- Orchestrator exposes its API under `/api/v1/`. Game_shelf's proxy targets that version explicitly. A `/api/cache/health` response includes the orchestrator's version; the frontend shows a soft warning if the backend/frontend detect a version mismatch range.

---

## 5. Data Model (orchestrator's SQLite — entirely separate from Game_shelf's DB)

The orchestrator's DB lives at `/var/lib/orchestrator/state.db` on the DXP4800. It has no foreign keys to or from Game_shelf. Access is via **`aiosqlite` with raw SQL** — no ORM. Migrations are numbered `.sql` files in `migrations/` (`0001_initial.sql`, `0002_…`, etc.), applied on startup by a ~50-LoC migrate script modeled on Game_shelf's `backend/src/db/migrate.js` pattern: `schema_migrations(id INTEGER PRIMARY KEY, applied_at TIMESTAMP)` tracks what has run, and the script executes any file whose number is greater than `MAX(id)` inside a transaction. No Alembic. No SQLAlchemy.

Rationale: seven tables, straightforward CRUD, no polymorphism, no lazy relationships. SQL is easier to read and audit than ORM DSL for this schema, and dropping SQLAlchemy also removes the need for APScheduler's `SQLAlchemyJobStore` (see §3.5 — we use `MemoryJobStore`). The tradeoff — no automatic schema-from-Python-classes, no query builder — is a feature at this scale.

```sql
CREATE TABLE platforms (
    name TEXT PRIMARY KEY,                -- 'steam' | 'epic'
    auth_status TEXT NOT NULL,            -- 'ok' | 'expired' | 'error' | 'never'
    auth_method TEXT NOT NULL,            -- 'steam_cm' | 'steam_webapi' | 'epic_oauth'
    auth_expires_at TIMESTAMP,
    last_sync_at TIMESTAMP,
    last_error TEXT,
    config JSON
);

CREATE TABLE games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL REFERENCES platforms(name),
    app_id TEXT NOT NULL,                 -- platform-native app id
    title TEXT NOT NULL,
    owned BOOLEAN NOT NULL DEFAULT 1,
    size_bytes INTEGER,
    current_version TEXT,                 -- steam: manifest gid; epic: build_version
    cached_version TEXT,
    status TEXT NOT NULL,                 -- 'unknown'|'not_downloaded'|'up_to_date'|
                                          -- 'pending_update'|'downloading'|'validation_failed'|'blocked'|'failed'
    last_validated_at TIMESTAMP,
    last_prefilled_at TIMESTAMP,
    last_error TEXT,
    metadata JSON,
    UNIQUE(platform, app_id)
);
CREATE INDEX idx_games_status ON games(status);
CREATE INDEX idx_games_platform_app ON games(platform, app_id);

CREATE TABLE manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    chunk_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    raw BLOB NOT NULL,                    -- compressed parsed manifest for replays
    UNIQUE(game_id, version)
);

CREATE TABLE block_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    app_id TEXT NOT NULL,
    reason TEXT,
    source TEXT NOT NULL DEFAULT 'cli',        -- 'cli' | 'gameshelf' | 'api' | 'config'
    blocked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, app_id)
);

CREATE TABLE validation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    manifest_version TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    method TEXT NOT NULL,                 -- 'disk_stat' | 'head_probe' | 'mixed'
    chunks_total INTEGER NOT NULL,
    chunks_cached INTEGER NOT NULL,
    chunks_missing INTEGER NOT NULL,
    outcome TEXT NOT NULL,                -- 'cached'|'partial'|'missing'|'error'
    error TEXT
);
CREATE INDEX idx_vh_game ON validation_history(game_id, started_at DESC);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                   -- 'prefill'|'validate'|'library_sync'|'auth_refresh'
    game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
    platform TEXT,
    state TEXT NOT NULL,                  -- 'queued'|'running'|'succeeded'|'failed'|'cancelled'
    progress REAL,
    source TEXT NOT NULL DEFAULT 'scheduler', -- 'scheduler' | 'cli' | 'gameshelf' | 'api'
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    error TEXT,
    payload JSON
);
CREATE INDEX idx_jobs_state ON jobs(state, kind);

CREATE TABLE cache_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TIMESTAMP NOT NULL,
    event TEXT NOT NULL,
    cache_identifier TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER
);
CREATE INDEX idx_co_time ON cache_observations(observed_at DESC);
```

WAL mode on. Vacuum monthly via scheduler job. `manifests.raw` keeps compressed parsed manifests for replaying validation without re-fetching from the platform. The `source` columns on `block_list` and `jobs` let the orchestrator attribute actions back to Game_shelf vs CLI vs direct API — useful for debugging and provenance.

---

## 6. Technology Stack

### 6.1 Orchestrator (DXP4800)

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | library ecosystem for both platforms |
| Async runtime | asyncio + httpx | mature, streaming, bounded concurrency |
| Steam SDK | `fabieu/steam-next` (git SHA pinned) | only actively maintained Python option; upstream is dormant |
| Epic SDK | Vendored subset of `legendary-gl/legendary` (git SHA pinned) | PyPI package is 5 years stale; vendor just what we need |
| Web framework | FastAPI | async native, OpenAPI, easy WebSockets/SSE |
| DB driver | `aiosqlite` + raw SQL | seven tables, simple CRUD — no ORM earns its keep |
| Migrations | numbered `.sql` files + ~50-LoC migrate script | copies Game_shelf's proven pattern; zero deps |
| Scheduler | APScheduler (`MemoryJobStore`) | only one recurring cron; no jobstore persistence needed |
| Config | `pydantic-settings` (env vars only) + Docker secrets | two mechanisms, typed, validated |
| Logging | `structlog` → JSON | machine-parseable |
| CLI | `click` | auth + ops; bundled in same container |
| Status page | single hand-written `status.html` | diagnostics UI, no build toolchain |
| Container | Python 3.12-slim, multi-stage | small image |
| Process mgmt | single process, `uvicorn` + APScheduler in-process | no sidecars, no broker |
| Tests | `pytest` + `pytest-asyncio` + httpx test client | standard |

**Dependencies dropped from this revision:** SQLAlchemy, Alembic, React, TypeScript, Vite, shadcn/ui, TanStack Query (all in-orchestrator). The orchestrator image is now a Python-only container with no Node.js build step.

### 6.2 Game_shelf additions (ThinkStation LXC)

Game_shelf already uses Express 5, better-sqlite3, node-cron, axios, React 18, Vite, TanStack Query, Tailwind, lucide-react. **No new dependencies required** — axios handles the proxy HTTP calls, TanStack Query handles the frontend data layer, lucide-react has icons for cache statuses. Scope of change is one backend route file, one service helper, three React components, one new page, and a Nav entry. This is the *only* rich UI in the system.

**Explicitly not used:**
- Celery/Redis (orchestrator) — single-process workload.
- Postgres — SQLite is plenty at this scale on both sides.
- Shared database / shared ORM — autonomy requires separation.
- Multi-container orchestrator split — single container is simpler.
- A "sync engine" between orchestrator and Game_shelf DBs — there is no sync; Game_shelf queries live when rendering.
- A second React SPA inside the orchestrator — status page + CLI covers the "Game_shelf is down" use case at a fraction of the complexity.

---

## 7. Risk Register

| # | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | `fabieu/steam-next` is single-maintainer, 3-star fork | Breaks Steam adapter if abandoned | Medium | Pin git SHA; fork into our own namespace; be prepared to maintain manifest/auth code internally |
| R2 | Steam login protocol changes faster than library can patch | Steam adapter dark until fixed | Medium | Keep .NET subprocess fallback design on paper: thin SteamKit2 wrapper exposing JSON-over-stdio for login + manifest-request-code + manifest fetch. Spike in phase 0. |
| R3 | Epic hardcoded launcher client creds rotated | Auth breaks for us and every legendary user | Low | Industry-wide risk; recovery is upstream's problem. Monitor `legendary` repo. |
| R4 | Manifest request code 5-min TTL | Mid-run failures on long prefills | High | Refresh per-depot code when >4.5 min old; retry once on 403 then re-fetch code |
| R5 | Lancache cache-key formula changes upstream | Disk-stat validator returns false MISSes | Low | Startup self-test (HEAD probe + path compute) fails loudly; HEAD-probe fallback |
| R6 | HEAD probe on missing chunk triggers upstream fetch | Validation becomes unintentional prefill | High (if misused) | Prefer disk-stat; HEAD only for self-test + sampling |
| R7 | `proxy_cache_lock` blocks HEAD probes up to 1h during active fills | Validator stalls | Low | 5 s client-side timeout on all probes |
| R8 | CellID drift (Steam) | Wrong CDN; cache misses | Low | Capture from login callback, persist, configurable override |
| R9 | Lancache 508 loop detection | Prefill fails with cryptic error | Low | Treat as fatal-config; surface clearly on status page and in CLI output |
| R10 | `gevent` monkey-patching clashes with asyncio | Runtime hangs | Medium | Isolate steam-next in dedicated thread; never call gevent from event loop |
| R11 | Users accidentally `pip install legendary-gl` (stale v0.20.9) | Broken build | Medium | Vendor the modules; don't depend on PyPI name |
| R12 | Manifest-format changes (Epic binary v23+, Steam format bumps) | Parser breaks | Low | Pin SDK commits; ride updates deliberately |
| R13 | Cache eviction invalidates previously-validated state | User sees "cached" but it's been evicted | Medium | Re-validate on schedule; tail access.log; expose LRU pressure |
| R14 | Disk-stat requires same-host filesystem access | Orchestrator must live on DXP4800 | Accepted | Deployment model is explicit; fallback is HEAD-only with known side effects |
| R15 | 2600 games × ~50k chunks avg ≈ 130M disk stats per full validation | I/O hit, bounded | Accepted | Parallelize; batched `os.scandir` per directory level; incremental validation (only re-check games whose manifest changed) |
| R16 | **Download path routing through 1GbE by accident** | 2.5× throughput loss | Low (architecturally prevented) | Orchestrator runs *on* DXP4800 and uses local compose network to reach Lancache. Document topology; add a startup assertion that Lancache is reachable on the compose network (not via ThinkStation). |
| R17 | **Game_shelf and orchestrator API version skew** | Proxy 500s, stale UI | Medium | `/api/v1/` prefix; health endpoint exposes versions; CI integration test from Game_shelf against a pinned orchestrator image |
| R18 | **Bearer token leak between hosts** | API takeover on LAN | Low | Docker secret on DXP4800; env var on Game_shelf (LXC-scoped); rotation documented; token never reaches browser |
| R19 | **Orchestrator reachable from untrusted LAN segments** | Data exposure / abuse | Low | LAN-only; bind to appropriate interface; pfSense rule restricting port 8765 to trusted VLAN |
| R20 | **Game_shelf down at the moment a prefill needs user interaction (e.g. Steam Guard re-auth)** | Can't respond from Game_shelf UI | Medium | `orchestrator-cli auth steam`/`auth epic` on the DXP4800 host handles all auth flows; Game_shelf was never on the critical path for credential entry |
| R21 | **Orchestrator ships a breaking API change while Game_shelf hasn't been updated** | Cache UI breaks for users | Medium | Versioned routes + tolerant frontend merging (extra fields ignored, missing fields rendered as "—") |
| R22 | **API stalls during active prefills (event-loop saturation)** | Game_shelf UI hangs; CLI commands time out | Medium | §3.6 event-loop discipline + Spike F load test before committing to asyncio-only. Fallback: isolate download fan-out in a subprocess. |
| R23 | **Title resolution misses on newly-entitled Epic titles** | Status page shows `epic:xxx` until next sync | Low | 6-hour sync refreshes; fallback renders platform:app_id; logged for inspection |
| R24 | **Dropping `SQLAlchemyJobStore` means scheduled jobs don't survive container restart** | Recurring cron re-registers at startup from config — this is the desired behaviour | Accepted | Documented; the only scheduled work is the 6-hour cycle, which has no state to preserve |

**Spikes before full build (phase 0, ~1 week):**
- Spike A: Steam — steam-next + raw httpx Lancache prefill of a small known app. Confirm URL/header/cache-hit pattern byte-for-byte.
- Spike B: Epic — legendary auth + vendored manifest parse + httpx prefill of one game. Confirm cache fill.
- Spike C: Validator — compute disk path for a known-cached chunk (from spike A), `stat()` it. Confirm formula on actual Lancache deployment.
- Spike D: gevent-asyncio bridge — confirm steam-next in executor thread doesn't deadlock under load.
- Spike E: **End-to-end topology** — stand up a throwaway orchestrator container in the DXP4800 Compose stack, prove it reaches Lancache via compose network, prove Game_shelf on the ThinkStation can reach its REST API, prove a prefill pulls bytes on the 2.5GbE path.
- Spike F (new): **API responsiveness under load** — run the chunk fan-out at full tilt (32 concurrent, ≥300 Mbps sustained) while pinging `GET /api/health` and `GET /api/games` from a second client. Target: p99 latency < 100 ms. If we miss, subprocess-isolate the downloader and re-measure.

---

## 8. Delivery Plan

Four phases, each independently shippable.

**Phase 0 — Spikes (1 week).** The six spikes A–F above. Exit criterion: each moving part works in isolation; adjust the plan if any fail.

**Phase 1 — Steam adapter + validator + autonomous orchestrator (~3 weeks).** Orchestrator container deployable alongside Lancache. Steam adapter end-to-end (auth → enumerate → manifest → prefill). Validator with disk-stat + startup self-test. REST API v1 per §3.7. CLI (`orchestrator-cli`) for auth + ops. Single-file `status.html`. APScheduler 6-hour cycle with `MemoryJobStore`. No Game_shelf integration yet; ops happens via CLI and status page. (Timeline reduced from earlier 3–4 weeks because no React SPA is being built.)

**Phase 2 — Epic adapter (2 weeks).** Vendored legendary modules + Epic adapter. Validator already generalized from phase 1 (Epic cache key = host-based, vs Steam's `"steam"` literal). Title resolution chain wired through catalog-public-service.

**Phase 3 — Game_shelf integration (1–2 weeks).** Backend proxy routes (`backend/src/routes/cache.js` + `services/orchestratorClient.js`). Frontend `CacheBadge`, `CachePanel`, `Cache.jsx` page, `cacheApi.js` hooks. Graceful-offline behaviour. Integration tests from Game_shelf against a locally-running orchestrator. After this phase, the rich UI story is complete.

**Phase 4 — Operational hardening (ongoing).** Access-log tail for live HIT/MISS. Cache-size and LRU-headroom observability. Metric exports (Prometheus endpoint if/when wanted). Backup/restore of orchestrator SQLite.

---

## 9. Platform Priority Order

1. **Steam** (phase 1) — largest library, best-understood protocol, active library fork. Highest value per unit work.
2. **Epic** (phase 2) — second-largest library, easier protocol than Steam. Built second so validator and orchestrator patterns are settled.
3. **Validator + operator surface + Game_shelf integration** — interleaved through phases 1–3. Orchestrator ships with CLI + status page only; Game_shelf is the rich UI.
4. **Ubisoft Connect — NOT BUILT.** Lancache caching is broken upstream (issue #195, open since July 2024). No protocol library reaches production quality. Reconsider if and only if monolithic#195 is fixed AND `ubisoft-manifest-downloader` reaches a usable state.
5. **EA App — NOT BUILT.** No usable OSS downloader (`Maxima` is explicitly alpha). Modern endpoints largely non-cacheable. Hostile vendor. Reconsider in 12+ months if Maxima stabilizes.
6. **GOG — NOT BUILT for prefill.** GOG is not in `uklans/cache-domains`. Traffic is HTTPS to Fastly; Lancache cannot intercept without MITM. `heroic-gogdl` is a separate project if a local mirror is ever wanted.

**Stretch goal** (not priority): a reverse view — log-tail-derived activity for hostnames the orchestrator didn't seed — as situational awareness for unknown cached content. Cheap to add once access.log tail exists.

---

## Appendix A — Key URLs and constants

- Steam trigger domain: `lancache.steamcontent.com`
- Steam User-Agent (required): `Valve/Steam HTTP Client 1.0`
- Steam chunk URL: `http://{lancache_host}/depot/{depot_id}/chunk/{sha_hex}`
- Epic auth code: `https://legendary.gl/epiclogin`
- Epic chunk URL: `http://{lancache_host}/Builds/{appId}/CloudDir/ChunksV4/{group}/{hash}_{guid}.chunk`
- Lancache cache root (container): `/data/cache/cache/`
- Lancache access log (container): `/data/logs/access.log`
- Lancache cache-domains: https://github.com/uklans/cache-domains
- Lancache heartbeat probe: `GET http://{host}/lancache-heartbeat` → header `X-LanCache-Processed-By`
- `$cacheidentifier` for Steam: literal string `steam`; for everything else: `$http_host`
- Cache key: `$cacheidentifier$uri$slice_range` (slice = 1 MiB)
- Disk path formula: `/data/cache/cache/<md5[28:30]>/<md5[30:32]>/<md5>`
- Orchestrator API port: `8765`
- Orchestrator → Lancache (compose net): `http://lancache:80`
- Game_shelf → Orchestrator: `${ORCHESTRATOR_URL}` (e.g., `http://dxp4800.local:8765`)

## Appendix B — Hosts and ports

| Host | Role | Network | Services |
|---|---|---|---|
| DXP4800 | storage, cache, orchestrator | 2.5GbE | Lancache (80/443), orchestrator (8765), cache volume 12 TB used of 57 TB |
| Lenovo ThinkStation P360 Tiny (`ferrumcorde`) | Proxmox host | 1GbE | Game_shelf LXC (3001 backend / 80 frontend) |
| MikroTik switch | switching | — | — |
| pfSense | firewall | — | port 8765 restricted to trusted VLAN |
| Pi-hole | DNS | — | `lancache.steamcontent.com` + Epic hostnames → Lancache IP |

## Appendix C — Out-of-scope decisions to revisit

- **TLS between Game_shelf and orchestrator.** LAN-only + bearer token is fine for now. Add TLS if the trust boundary changes (e.g., external access through a reverse proxy).
- **Multi-user auth.** Single bearer token is fine for LAN.
- **Proxmox HA for Game_shelf.** Not relevant — Game_shelf being down is survivable; the orchestrator keeps working.
- **Alternative to APScheduler** — revisit only if scheduling needs outgrow single-process (unlikely).
- **Backup/restore of SQLite state.** Cheap cron job for now.
- **Shared block-list between Game_shelf and orchestrator.** Not needed — orchestrator owns the block list; Game_shelf's UI mutates it via the API. If we ever want "hide blocked games from Game_shelf's library view," that's a frontend filter against the proxied cache data, not a schema sync.
