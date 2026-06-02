# F5 Steam CDN Prefill — Design Spec

**Date:** 2026-05-29
**Feature:** F5 — download a Steam game's depot chunks **through** the lancache
so they get cached. This is the feature that *generates* the content F7
validates; together they close the orchestrator's core loop (prefill → cache →
validate). Steam-only for MVP; F6 (Epic) deferred.

**Spike:** `spikes/spike_a5_prefill.md` — request shape, key alignment, and the
mode-000 finding all verified live against the lancache host.

---

## 1. Scope

### In (MVP)

- A `prefill` job-kind handler that downloads a game's current depot chunks
  through the lancache (stream-and-discard), so lancache caches them.
- Manual trigger: `POST /api/v1/games/{game_id}/validate`'s sibling
  `POST /api/v1/games/{game_id}/prefill` (bearer-gated, in-flight dedup).
- Async chunk downloader: `httpx.AsyncClient`, bounded concurrency
  (`asyncio.Semaphore`), per-chunk timeout + bounded retry/backoff.
- Chunk-list reuse: latest manifest per depot → worker `manifest.expand` →
  deduped `(depot_id, sha)`; if the game has no manifests, fetch them first via
  the existing `manifest_fetch` path.
- `games.status='downloading'` while running; job `progress` updated as chunks
  complete.
- **ID5:** on successful prefill, enqueue a `validate` job for the game.
- **F7 readability enhancement** (bundled, per user decision): `disk_stat`
  "cached" now also requires the owner-read bit, so mode-000 cache files are
  not counted.

### Out (deferred)

- **F6 Epic prefill** — different CDN/manifest path; post-MVP.
- Scheduled / library-wide prefill (F13 sweep territory).
- Partial/resume optimization (only-download-changed-since-last) beyond
  "skip already-cached chunks is NOT done in v1 — see D7").
- Real-time progress streaming (SSE/WS).

---

## 2. Architecture & components

1. **`src/orchestrator/prefill/downloader.py`** — the async HTTP engine:
   - `async def prefill_chunks(chunk_uris: list[str], settings, *, on_progress=None) -> PrefillResult`
     — downloads each URI through lancache with a bounded
     `asyncio.Semaphore(settings.prefill_concurrency)`, streaming and
     discarding the body. Per-chunk: `httpx` GET with `User-Agent` +
     `Host` headers, `prefill_chunk_timeout_sec` read timeout, retry with
     backoff (`prefill_retry_backoffs_sec`, default `[1,4,16]`). A chunk that
     still fails after retries is recorded as failed.
   - `PrefillResult` dataclass: `chunks_total, chunks_ok, chunks_failed,
     failures: list[(uri, reason)]` (failures truncated/capped).
   - "ok" = HTTP 2xx. Uses one shared `AsyncClient` (connection pooling).
     Pure-ish: takes URIs + settings; the caller builds URIs and persists
     results. Unit-testable against a local `ASGITransport`/stub server.

2. **`src/orchestrator/prefill/chunk_uris.py`** (or a helper in downloader) —
   pure: `steam_chunk_download_uri(depot_id, sha) -> str` →
   `/depot/{depot_id}/chunk/{sha}` (reuse/validate via the existing
   `validator.cache_key.steam_chunk_uri`, which already validates depot/sha
   shape — DRY).

3. **`src/orchestrator/jobs/handlers/prefill.py`** — `prefill_handler(job,
   deps)`:
   - non-steam / unknown game → `ValueError`; `deps.steam_client is None`
     only matters if a manifest fetch is needed.
   - set `games.status='downloading'`.
   - ensure manifests: if the game has ≥1 manifest row, use them; else run the
     manifest-fetch (reuse `manifest_fetch_handler`'s steam_client call or the
     stored path) — then expand each latest-per-depot manifest to chunk SHAs
     (reuse `validator.disk_stat`'s latest-per-depot query + worker
     `manifest_expand`), dedup `(depot_id, sha)`, build URIs.
   - call `prefill_chunks(...)`, updating job `progress`.
   - on success (no failed chunks): leave status as-is and **enqueue a
     `validate` job** (ID5) — the validate run sets the final
     `up_to_date`/`validation_failed`. On failures: set
     `games.status='failed'`, record error; mark the job failed.

4. **`src/orchestrator/api/routers/prefill_trigger.py`** —
   `POST /api/v1/games/{game_id}/prefill`: 404 unknown, 400 non-steam,
   in-flight dedup (queued/running `prefill` → existing job_id), 202+job_id,
   503 on `PoolError`. Mirrors `validate_trigger.py`.

5. **Settings** additions:
   - `lancache_base_url: str = "http://127.0.0.1"` (the prefill target; the
     monolithic container on the host).
   - `steam_cdn_host: str = "lancache.steamcontent.com"` (Host header).
   - `prefill_user_agent: str = "Valve/Steam HTTP Client 1.0"`.
   - `prefill_concurrency: int = Field(32, ge=1, le=256)`.
   - `prefill_chunk_timeout_sec: float = Field(10.0, gt=0, le=120)`.
   - `prefill_chunk_max_attempts: int = Field(3, ge=1, le=10)`.

6. **F7 change** — `validator/disk_stat.py` `_stat_batch`: cached requires
   `st.st_size > 0 AND (st.st_mode & 0o400)` (owner-read), in addition to the
   existing not-a-symlink check.

---

## 3. Data flow (a prefill run)

```
POST /api/v1/games/{id}/prefill
  → INSERT jobs(kind='prefill', game_id, platform='steam', state='queued', source='api')
  → 202 {job_id}

worker_loop claims → prefill_handler(job, deps)
  → UPDATE games SET status='downloading'
  → ensure manifests (fetch if none) ; load latest manifest per depot
  → for each: deps.steam_client.manifest_expand(raw) → chunk_shas
     dedup (depot_id, sha) → uris = [/depot/{d}/chunk/{sha}]
  → prefill_chunks(uris, settings):
       AsyncClient; Semaphore(prefill_concurrency)
       per uri: GET lancache_base_url+uri, Host=steam_cdn_host, UA=…,
                stream+discard; retry backoffs on timeout/5xx/conn-error
       tally ok/failed; periodic job progress update
  → if chunks_failed == 0:
        enqueue jobs(kind='validate', game_id, ...)   # ID5
        (status left to the validate run)
     else:
        UPDATE games SET status='failed', last_error=…
        raise → job state=failed
  → mark job succeeded (if no failures)
```

`games.status` transitions: `downloading` at start; on full success the
follow-up validate sets `up_to_date`/`validation_failed`; on chunk failures
`failed`. A game with **zero chunks** (empty manifest set) → success, enqueue
validate (which classifies it cached).

---

## 4. Locked decisions

- **D1 — request shape** (spike A5): `GET {lancache_base_url}/depot/{id}/chunk/
  {sha}`, `User-Agent: Valve/Steam HTTP Client 1.0`, `Host:
  lancache.steamcontent.com`, no Range. Stream + discard. Caches under F7's key.
- **D2 — orchestrator-side** httpx download (no worker/steam-next for the
  download). Manifest fetch (if needed) still uses the worker.
- **D3 — chunk list** = deduped `(depot_id, sha)` from latest-manifest-per-depot
  + `manifest.expand` (reuse F7's query + worker op).
- **D4 — no manifest request code** for chunk downloads (unauthenticated URLs).
- **D5 — concurrency** bounded by `Semaphore(prefill_concurrency=32)`; one game
  at a time is already guaranteed by the single jobs worker loop.
- **D6 — retry** per chunk: up to `prefill_chunk_max_attempts` with backoffs
  `[1,4,16]s` on timeout / connection error / 5xx. Persistent failure →
  recorded; any failed chunk → job failed (retry next cycle).
- **D7 — no pre-skip of cached chunks in v1.** Prefill requests every chunk;
  lancache HITs are cheap (served from cache, no upstream). Skipping
  already-cached chunks (via an F7 pre-pass) is a post-MVP optimization.
- **D8 — ID5:** success → enqueue a `validate` job (don't inline-validate;
  keep handlers single-purpose, let the worker pick it up).
- **D9 — F7 readability:** `cached` requires `st_mode & 0o400` (owner-read).
- **D10 — Steam-only.** Non-steam game → 400 / handler `ValueError`.

---

## 5. Error handling

- Cache/lancache unreachable (connection refused) → chunks fail after retries
  → job failed, `games.status='failed'`, error summarized (first N failures).
- A single chunk's persistent failure fails the job (the game isn't fully
  prefilled); next cycle retries. (No partial-success "succeeded with
  warnings" state in v1.)
- Manifest fetch needed but `steam_client` is None / `NotAuthenticated` →
  propagate (job failed); operator must (re)auth. Mirrors BL12.
- `httpx` per-chunk read timeout = `prefill_chunk_timeout_sec`; the shared
  client uses sane connect/pool timeouts.
- Event-loop friendliness: all I/O is async httpx; no blocking calls. The
  `Semaphore` caps in-flight requests so `/health` stays responsive
  (FRD p99 target).
- Structured logs: `prefill.started`, `prefill.manifests_ready`,
  `prefill.progress` (throttled), `prefill.chunk_failed` (capped),
  `prefill.completed` (ok/failed counts), `prefill.validate_enqueued`. No
  secrets, no chunk bytes.

---

## 6. Testing strategy (test-first)

- **downloader** (`tests/prefill/test_downloader.py`): drive
  `prefill_chunks` against an in-process stub (httpx `MockTransport`/
  `ASGITransport`): all-ok; some 5xx-then-ok (retry succeeds); a chunk that
  always fails (recorded, counts failed); verify the request carries the
  Host + User-Agent headers and the right path; concurrency cap respected
  (semaphore); body is streamed/discarded (no full-buffer assumption).
- **chunk_uris**: `/depot/{id}/chunk/{sha}` formatting + shape validation
  (reuse cache_key validation).
- **prefill_handler** (`tests/jobs/test_prefill_handler.py`): stub
  `steam_client.manifest_expand` + a stub downloader; seed manifests; assert
  `games.status='downloading'` set, validate job enqueued on success, status
  `failed` on chunk failures, non-steam/unknown raise, no-manifests path
  triggers a fetch.
- **prefill_trigger** (`tests/api/test_prefill_trigger_router.py`): 202/dedup/
  404/400/auth-401/503 (mirror validate trigger tests).
- **settings**: new fields' defaults + bounds.
- **F7 readability** (`tests/validator/test_disk_stat.py`): a mode-000 file is
  NOT counted cached; a mode-600/644 readable file IS (use `os.chmod` on a
  tmp file).
- Full suite green; ruff/format/mypy/gitleaks/semgrep clean.

---

## 7. Security review focus (Phase 2.4)

- **SSRF surface:** the download URL is built from validated `depot_id` (int)
  + `sha` (`^[0-9a-f]{40}$`) onto a fixed `lancache_base_url` — no
  user-controlled host. `game_id` is an int path param. No injection.
- **No new auth surface** beyond the bearer-gated trigger.
- **Resource use:** bounded concurrency + per-chunk timeout; chunk count
  bounded by the operator's own owned game. Stream-and-discard caps memory.
- **No secrets** in URLs/logs; chunk URLs are unauthenticated content paths.
- The F7 readability check uses `stat` only (no `open()`), so it adds no new
  file-read surface.

---

## 8. Out-of-scope confirmations

No Epic (F6). No scheduled/bulk prefill (F13). No cached-chunk pre-skip
optimization. No CLI. These keep F5 to one reviewable build loop.
