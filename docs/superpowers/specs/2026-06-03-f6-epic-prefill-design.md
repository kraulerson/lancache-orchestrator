# F6 — Epic CDN Prefill (full Epic stack) — Design

**Date:** 2026-06-03
**Feature:** F6 (PROJECT_BIBLE / FRD) — Epic CDN Prefill.
**Spike:** `spikes/spike_b_epic_prefill.py` (PASS, Milestone A) — proves the entire
flow in pure Python (httpx OAuth + binary-manifest parse + version-aware chunk
paths + `Host`-header lancache routing + cache-HIT verify).

## Goal

Bring Epic Games to parity with the existing Steam pipeline: authenticate, enumerate
the owned library into `games`, fetch + parse Epic manifests, and **prefill** a
game's chunks through the lancache so they get cached — then verify the cache took.
One cohesive feature ("full Epic stack"), all **pure-Python / async-httpx in the
orchestrator process** — no `legendary` runtime dependency, no gevent, no worker
subprocess (unlike Steam, which needs the steam-next/gevent isolation of ADR-0013).

## Scope decisions (Orchestrator-approved, 2026-06-03)

1. **Full Epic stack** in one feature: OAuth → library enumerate → manifest fetch +
   parse → prefill download → validate.
2. **Pure-Python manifest parsing** (spike B) over vendoring `legendary` modules
   (the Phase-0 Manifesto wording). Rationale: spike-proven; avoids a GPL-3 vendoring
   obligation and gevent-style complexity; keeps the parser fully under our control
   and unit-testable. Recorded in **ADR-0014**; documented as a deliberate deviation
   from the Manifesto's F6 wording.
3. **Validation = sample-based cache-HIT now; F7-Epic disk-stat deferred.** F7's
   disk-stat needs the Epic *on-disk* cache-key, which (unlike Steam's spike-A4 key)
   can only be derived from real cached Epic chunks — a chicken-and-egg that unblocks
   only after a live Epic prefill. So F6 ships header-based cache verification
   (re-request a sample of chunks, assert `X-Upstream-Cache-Status: HIT`); the full
   F7-Epic disk-stat validator is a fast follow-up once the cache-key is derivable.

## Architecture

New package `src/orchestrator/platform/epic/` plus targeted additions to the existing
prefill/jobs/api/validator layers. Each unit has one responsibility and a stubbed
boundary for unit testing.

| Unit | File | Mirrors | Responsibility / dependencies |
|---|---|---|---|
| Models | `platform/epic/models.py` | — | `AuthTokens`, `EpicChunk`, `EpicManifest`, `EpicLibraryItem` dataclasses. Pure data. |
| OAuth | `platform/epic/oauth.py` | F1 Steam auth | Exchange auth-code → tokens; refresh access token from stored refresh token; persist refresh token to `epic_session_dir` (0600); update the `platforms` row (`auth_status`, `auth_expires_at`, `last_error`). httpx only. |
| Library | `platform/epic/library.py` | BL11 sync | Paginated library enumeration (`includeMetadata`, cursor) → list of items. Pure fetch+map; caller upserts `games`. |
| Manifest | `platform/epic/manifest.py` | BL12 fetch | v2 manifest API → manifest URI (+ `queryParams`) → download binary manifest → **`parse_manifest()`** (FString / GUID / CDL binary parse, from spike) → `EpicManifest` (version, chunks, cdn_base). `chunk_path()` version-aware (ChunksV5 + base64 for v≥22; legacy hex otherwise). |
| Downloader | `prefill/epic_downloader.py` | F5 `downloader.py` | One `httpx.AsyncClient` with `Host: <cdn-host>` + Epic UA; `Semaphore(chunk_concurrency)`; per-chunk GET `http://{lancache}/{cdn-path}/{chunk_path}`, **stream + discard**; per-chunk timeout + retry/backoff (`[1,4,16]`, 4xx not retried). Returns `EpicPrefillResult` (totals + failures). A `_build_transport()` seam for `MockTransport` tests. |
| HIT verify | `prefill/epic_downloader.py` (`verify_cached`) | spike B pass-2 | Re-request a bounded random sample of already-downloaded chunks; count `X-Upstream-Cache-Status: HIT`. Returns a HIT ratio used to classify the prefill outcome. |
| Handlers | `jobs/handlers/library_sync.py`, `manifest_fetch.py`, `prefill.py` | F5 + BL11/12 | Dispatch on `job.platform`: add `epic` branches that call the Epic units. Epic `prefill` handler: set `games.status='downloading'` → **fetch a FRESH manifest+CDN URL** (signed URLs expire) → parse → build chunk URLs → download → sample HIT-verify → enqueue `validate` (ID5) on success / `games.status='failed'` on chunk failures. |
| Auth router | `api/routers/epic_auth.py` | — | `POST /api/v1/platforms/epic/auth` (submit authorization code) → exchange + persist; `GET /api/v1/platforms/epic/auth` status. Bearer-gated. On successful auth, auto-enqueue an Epic `library_sync` (mirrors the Steam auth auto-trigger from BL11). |
| Sync trigger | `api/routers/epic_sync.py` | `routers/sync.py` | `POST /api/v1/platforms/epic/library/sync` — a **parallel** Epic route mirroring the Steam one (same in-flight dedup via the partial-unique index, 202/401/503). Keeps each platform's route explicit rather than overloading the Steam path. |
| Prefill trigger | `routers/prefill_trigger.py` (unchanged) | F5 | `POST /api/v1/games/{game_id}/prefill` is already **platform-agnostic** — it enqueues a `prefill` job for the game; the handler branches on `game.platform`. No new route. |
| Manifest fetch | internal | BL12 | Not a public route — enqueued by `library_sync` / prefill (as Steam does), dispatched on `platform`. |
| Cache key | `validator/cache_key.py` (`epic_chunk_uri`) | F7 | Add `epic_chunk_uri()` for the eventual disk-stat path; **not wired this round** (deferred per decision 3) — placed now so the follow-up is a small diff. |
| ADR | `docs/ADR documentation/ADR-0014-epic-pure-python-manifest.md` | — | Records pure-Python-over-legendary + Manifesto deviation. |
| Settings | `core/settings.py` | F5/F7 | Epic OAuth URLs + public client id/secret, library/manifest URLs, Epic CDN User-Agent, `epic_session_dir`, `epic_manifest_label` (`Live`), `epic_platform` (`Windows`), reuse `chunk_concurrency` + prefill timeout/retry settings. |

## Data flow

```
POST /platforms/epic/auth {code}
        │  oauth.exchange → tokens; persist refresh_token; platforms.auth_status='ok'
        ▼
library_sync (epic)  ── library.enumerate → upsert games(platform='epic', app_id=appName, title)
        ▼
manifest_fetch (epic, per game) ── manifest.fetch_url → download → parse_manifest
        │  store manifests(game_id, raw=<binary>, depot_id=NULL, version=<build>, chunk_count, total_bytes)
        │  update games.size_bytes
        ▼
prefill (epic, per game) ── FRESH manifest fetch+parse (signed URL freshness)
        │  → epic_downloader.prefill_chunks → lancache (stream+discard)
        │  → verify_cached (sample HIT) → enqueue validate (ID5) | games.status='failed'
        ▼
validate (ID5)  ── Steam path unchanged; Epic disk-stat = deferred follow-up
```

## Data model

**No migration.** The schema is already Epic-ready:
- `platforms` has the seeded `epic` row (`auth_method='epic_oauth'`, `auth_status`,
  `auth_expires_at`, `last_error`, `config`).
- `games` carries `platform`; `app_id` (TEXT) holds the Epic `appName`;
  `UNIQUE(platform, app_id)` separates Epic from Steam.
- `manifests` stores the binary manifest in `raw` (BLOB), `depot_id` NULL (Epic has
  no depots), `version` = Epic build version (TEXT), `chunk_count` / `total_bytes`.
- `jobs.platform` CHECK already includes `epic`; `jobs.kind` already includes
  `library_sync`, `manifest_fetch`, `prefill`, `validate`.

Epic chunk identities (GUID / hash / group / format-version) are **not** stored
relationally — they live in the `raw` binary manifest and are parsed in-process at
prefill time (as Steam chunks are expanded by the worker at validate time).

## Error handling

- **Auth**: access-token expiry → silent refresh from the stored refresh token;
  refresh failure → `platforms.auth_status='expired'` + `last_error`, surfaced (mirrors
  the Steam `NotAuthenticated` flip). Tokens never logged (reuse `core/logging` redaction).
- **Manifest**: signed URL / manifest expiry → re-fetch at prefill time (never reuse a
  stale signed URL). A `v22`-class decrypt/parse error surfaces **distinctly** (per
  Manifesto F6 failure-state) rather than as a generic parse failure.
- **Download**: per-chunk retry/backoff (`[1,4,16]`, 4xx not retried), bounded timeout;
  any failed chunk → `games.status='failed'` + job failed (mirror F5).
- **Handlers** never raise into the scheduler/worker uncaught; DB errors logged + job failed.

## Testing

- **Manifest parse**: golden-fixture binary manifests (captured/synthetic) covering a
  v≥22 (ChunksV5/base64) and a legacy (<22 hex) manifest; assert chunk count, GUIDs,
  and `chunk_path()` output against known vectors.
- **OAuth**: `MockTransport` token endpoint — code→tokens, refresh, expiry→refresh,
  refresh-failure→expired; assert no token in logs.
- **Library**: `MockTransport` paginated responses (cursor); assert full enumeration +
  `games` upsert mapping.
- **Downloader**: `MockTransport` — assert `Host` header + Epic UA, chunk path, retry-
  then-success, 4xx-not-retried, concurrency cap, stream+discard; `verify_cached` HIT
  counting.
- **Handlers**: stub Epic client + monkeypatched downloader — status transitions, fresh-
  manifest-refetch, validate-enqueue on success, failed on chunk failure, non-epic guard.
- **Triggers**: 202 / in-flight dedup / 404 / 400 / auth / 503 (mirror the Steam trigger tests).
- **Settings**: bounds + defaults for the new Epic settings.
- **Live UAT (manual stopping point)**: the Orchestrator authenticates a real Epic
  account (visit `legendary.gl/epiclogin`, paste the code), runs a real library sync +
  manifest fetch + prefill of a small Epic title against the deployed lancache, and
  confirms cache HIT. This is the post-implementation gate (analogous to F5's Steam 2FA);
  all code is unit-tested with stubs first.

## Out of scope / follow-ups

- **F7-Epic disk-stat validation** — needs the Epic on-disk cache-key, derivable only
  from real cached Epic chunks; do it after the live UAT (`epic_chunk_uri()` is staged
  now so the follow-up is small).
- Library *auto-scheduling* for Epic via the F12 cron can reuse the existing scheduler
  once Epic `library_sync` exists; wiring the periodic Epic tick is a thin follow-up.

## Stopping point

Epic OAuth requires the Orchestrator's real Epic account for live auth + UAT — a manual
step the AI cannot perform. F6's autonomous deliverable is the full, unit-tested Epic
stack behind stubs + the auth/prefill endpoints; the live UAT is where the Orchestrator
takes over (provide the auth code, run against the deployed lancache).
