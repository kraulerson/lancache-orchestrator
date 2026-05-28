# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/) with extended categories
for handoff clarity. Categories are ordered by impact severity.

<!--
  Category definitions:
  - Security: Vulnerability fixes, dependency patches for CVEs, auth changes
  - Data Model: Schema migrations, data format changes, rollback notes
  - Added: New features, new endpoints, new commands
  - Changed: Modifications to existing behavior
  - Fixed: Bug fixes (reference BUGS.md entry if applicable)
  - Removed: Removed features, deprecated endpoints
  - Infrastructure: CI/CD changes, dependency updates, configuration changes, tooling
  - Documentation: Significant doc updates (new ADRs, updated threat model, revised user guide)
-->

## [Unreleased]

### Added — F12 Scheduler subsystem — 2026-05-28

Implements F12 from PROJECT_BIBLE §1.2 MVP cutline and JQ3 (`/health.scheduler_running` is now real). APScheduler 3.11.2 `AsyncIOScheduler` integrated into FastAPI lifespan; periodically enqueues `library_sync` jobs for the BL11 jobs worker to execute.

- New `src/orchestrator/scheduler/` package with `SchedulerManager` (wraps `AsyncIOScheduler`) + `enqueue_library_sync` cron callback. `replace_existing=True` jobs registered at boot; in-memory `MemoryJobStore` (default) means cron config re-renders on every restart.
- Settings:
  - `scheduler_enabled: bool = True` — diagnostic / dev escape hatch
  - `scheduler_library_sync_interval_sec: int = 21600` (6h, range 60..86400)
- Lifespan ordering: scheduler starts at step 4b (between jobs worker spawn and lancache probe init); shuts down FIRST in teardown so it can't enqueue work during shutdown.
- `/health.scheduler_running` reads `app.state.scheduler_manager.running`. No-lifespan test fixtures fall back to `False` via `getattr(..., None)` guard.
- Cron callbacks include dedup against existing queued/running library_sync rows (mirrors the manual sync endpoint) so cron + operator triggers don't race onto duplicate rows.
- 25 new tests: 7 in `tests/scheduler/test_jobs.py` (enqueue + dedup + error swallow), 7 in `tests/scheduler/test_manager.py` (start/stop/running/jobs), 3 in `tests/api/test_lifespan_scheduler.py` (integration), 5 in `tests/core/test_settings.py` (defaults + bounds), 3 in `tests/api/test_health_endpoint.py` (probe wiring). Full suite: 830 pass.
- Updated `tests/api/test_lifespan.py::test_lifespan_returns_503_through_handler_when_unhealthy` — `scheduler_running` is now `True` post-F12; `validator_healthy` is the remaining stub-false subsystem keeping /health at 503.

### Fixed — ID2 lancache probe: real lancache returns 204 with header identifier — 2026-05-28

Surfaced by post-PR-#113 deployment testing against the running `lancachenet/monolithic` image: lancache's `/lancache-heartbeat` endpoint returns **HTTP 204 No Content** (not 200) and identifies itself via the **`X-LanCache-Processed-By`** response header. PR #113's `LancacheProbe._refresh()` strictly checked for 200 → would have reported `lancache_reachable: false` even against a healthy lancache.

- `LancacheProbe` now accepts any 2xx status code AND requires the `X-LanCache-Processed-By` header. The header check is positive identification — defends against misconfigured DNS bypass / wrong target where some other 2xx-responding service would otherwise pass the probe.
- Headers checked case-insensitively (httpx normalizes lookup).
- New structured log event: `lancache.probe.missing_identifier_header` (WARN) fires when a 2xx response arrives without the lancache header — helps operators diagnose "responsive endpoint, wrong target."
- 5 new tests in `tests/lancache/test_heartbeat.py` (204 with header, 200 with header, all-2xx-with-header, 2xx-without-header-rejected, case-insensitive header match). Renamed `test_non_200_returns_false` → `test_non_2xx_returns_false`.

### Added — ID6 Startup Job Reaper — 2026-05-27

Implements `docs/phase-0/frd.md:649` ID6 — the "startup reaper for abandoned jobs" requirement. Closes the SEV-3 deployment-shape finding F-UAT6-8 (stale `library_sync` jobs surviving container restarts forever).

- New `src/orchestrator/jobs/reaper.py` with `reap_running_jobs(pool) -> int`. Single atomic `UPDATE jobs SET state='failed', error=..., finished_at=CURRENT_TIMESTAMP WHERE state='running'`.
- FastAPI lifespan calls the reaper after pool init but before the jobs worker task spawns — orphans get cleaned before the new worker could conceivably mis-claim them. Defensive try/except around the call so a failed reap doesn't abort boot.
- Tests: 7 unit tests in `tests/jobs/test_reaper.py` (empty table, no-running, single, multiple, mixed-states, idempotency, error-message length contract). 3 integration tests in `tests/api/test_lifespan_reaper.py` driving the real lifespan via `asgi_lifespan.LifespanManager` — seeds an orphaned `running` job pre-boot, verifies it's flipped to `failed` post-boot.

### Added — ID2 Lancache self-test — 2026-05-27

Implements the ID2 implicit-dependency feature from `docs/phase-0/frd.md:645` and `docs/phase-1/architecture-proposal.md` — the operator-facing `/api/v1/health` endpoint now surfaces a real `lancache_reachable` boolean derived from an HTTP probe to `<lancache>/lancache-heartbeat`, replacing the BL5 stub-false.

- New `src/orchestrator/lancache/` package with `LancacheProbe` (async, cache-TTL'd, concurrency-safe via `asyncio.Lock`). 16 tests cover happy path, every documented httpx failure mode, TTL cache hit/miss/refresh, concurrent-probe collapse, and URL validation.
- New Settings fields:
  - `lancache_heartbeat_url` (default `http://lancache/lancache-heartbeat`)
  - `lancache_probe_timeout_sec` (default 5.0, range 0–60)
  - `lancache_probe_cache_ttl_sec` (default 30.0, range 0–600)
- FastAPI lifespan startup constructs the singleton probe and stashes it on `app.state.lancache_probe`. The `/health` router calls `await probe.probe()` per request (cache-fast — usually no IO).
- Tests in `tests/api/test_health_endpoint.py` verify the wiring through stub probes; the no-lifespan `unit_app` fixture falls back to `False` instead of crashing.

### Fixed — Post-UAT-6 SEV-2 batch — 2026-05-27

Closes #107 (licenses enumeration) and #109 (`get_product_info` timeout) — both surfaced by the UAT-6 live operator session against a real Steam account.

- **#107** — Worker library enumeration extracted from `worker.py` into a pure, unit-testable `enumerate.py` module:
  - `wait_for_licenses(client, timeout=10s)` polls `client.licenses` (which is `dict[int, License]`, populated asynchronously by `EMsg.ClientLicenseList`) until non-empty or deadline
  - `enumerate_apps(client, batch_size=50)` iterates `licenses.values()` (the previous code iterated the dict yielding keys, then `getattr(int, "package_id")` returned None — explaining the "0 apps for every real account" symptom)
  - Skips `auto_access_tokens=True` for the package call — `licenses[pid].access_token` is already known, saving one Steam round-trip per batch
- **#109** — Chunks `get_product_info(packages=...)` and `get_product_info(apps=...)` into batches of 50. New `Settings.steam_worker_library_enumerate_timeout_sec` (default 300, range 30–3600) drives a per-op timeout override in `SteamWorkerClient._send_and_await` so library_enumerate gets a 5-minute budget while other ops keep the 30s default.

Test coverage: 30 new tests in `tests/platform/steam/test_enumerate.py` covering the chunking, wait, build-package-request, extract-app-ids, extract-app-metadata, and end-to-end enumeration paths; 3 new tests in `tests/platform/steam/test_client_unit.py` for the per-op timeout override. Full suite: 760 tests pass.

### Documentation

- New `spikes/spike_a2_steam_modern.md` — full steam-next 1.4.4 API investigation documenting the actual licenses/get_product_info/login surfaces that BL10/BL11 had wrong.
- New `docs/known-limitations.md` — operator-facing note explaining the steam-next-driven container-restart re-auth requirement.
- Closed #108 (session persistence) with detailed won't-fix-at-current-scope rationale referencing the spike doc. Opened strategic follow-up #111 for future Steam-library evaluation.

### Security — UAT-6 SEV-2 remediation — 2026-05-26

Three production-blocking findings from the UAT-6 agent sweep, all fixed test-first:

- **F-UAT6-1 [SEV-2]** (`src/orchestrator/platform/steam/client.py`) — `SteamWorkerClient.start()` now passes `limit=MAX_IPC_LINE_BYTES + 1 KiB` to `asyncio.create_subprocess_exec`, sizing the StreamReader's internal buffer above asyncio's default 64 KiB. Pre-fix, any Steam library response > 64 KiB (true for any account with ~600+ apps) would have crashed the reader task on a raw `ValueError` from `readline()`, leaked the worker subprocess, and prevented the restart-storm guard from firing. The `_read_loop` now also catches `ValueError` and `LimitOverrunError`, emitting `steam_worker.ipc_response_overflow` and calling `_on_worker_died(reason='response_too_large')`.
- **F-UAT6-2 [SEV-2]** (`src/orchestrator/platform/steam/{client,worker}.py`) — Worker now reads its credential-location directory from `os.environ["ORCH_STEAM_SESSION_DIR"]` (falling back to the historical default) instead of the hardcoded `/var/lib/orchestrator/steam_session`. `SteamWorkerClient.start()` forwards `Settings.steam_session_dir` into the subprocess env. Pre-fix, operators with a customized volume mount silently lost refresh-token persistence across restarts.
- **F-UAT6-3 [SEV-2]** (`src/orchestrator/jobs/handlers/library_sync.py`) — `library_sync_handler` now catches `SteamWorkerError(kind='NotAuthenticated')`, updates `platforms.auth_status='expired'` and `last_error`, then re-raises so the job is still marked failed. Other `SteamWorkerError` kinds (e.g. `SteamAPIError`) leave `auth_status` unchanged — those represent transient failures, not session expiry. Pre-fix, `GET /platforms` would show `auth_status='ok'` while `GET /platforms/steam/auth/status` simultaneously returned `authenticated=false`.

### Added — BL11 Steam Library Sync (F1 milestone 2/3) — 2026-05-25
- `src/orchestrator/jobs/` package — generic asyncio job dispatcher
  (`worker.py`, `handlers/__init__.py` registry). Single-loop topology
  (spec D10) with atomic SELECT-then-UPDATE claim under `BEGIN IMMEDIATE`
  so concurrent claims serialize.
- `library_sync` handler (`src/orchestrator/jobs/handlers/library_sync.py`)
  calls `library.enumerate` on the steam worker subprocess and upserts the
  `games` table via `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE` —
  re-sync is idempotent; downstream lifecycle columns (status,
  cached_version, last_validated_at) are preserved.
- `POST /api/v1/platforms/steam/library/sync` — manual sync trigger with
  handler-side dedup of queued/running jobs (existing in-flight job_id
  returned instead of creating a duplicate).
- `library.enumerate` IPC op on the steam worker subprocess. Walks
  `_client.licenses` → `get_product_info(packages=...)` → `get_product_info(apps=...)`
  to assemble owned-app metadata; live Steam validation deferred to UAT-6.
- `SteamWorkerClient.library_enumerate()` async method.
- Auto-queue `library_sync` job after BOTH Steam auth-success paths
  (no-2FA and 2FA), best-effort — DB failure during enqueue is logged
  but does NOT fail the auth response.

### Changed — BL11
- FastAPI lifespan now spawns the jobs worker asyncio task at startup
  and cleanly stops it (5 s shutdown timeout, then cancel) ahead of
  steam-client + pool shutdown.

### Infrastructure — BL11
- Settings field `jobs_worker_poll_interval_sec` (range 0.05–60.0,
  default 1.0) governs the empty-queue poll cadence.

### Documentation — BL11
- New BL11 feature entry in `FEATURES.md`.
- Plan: `docs/superpowers/plans/2026-05-25-bl11-library-sync.md`.

### Security
- **UAT-5 remediation (7 findings)** hardening the BL5-BL9 API surface:
  - **U5-1 [SEV-2]** (`middleware.py`) — bearer-auth Authorization header now
    decoded with `errors="strict"` (was `errors="ignore"`, which silently
    dropped non-ASCII bytes); added 4096-byte header-size cap. Non-conforming
    HTTP clients can no longer send byte sequences that decode to the same
    token via silent normalization.
  - **U5-2 [SEV-2]** (`routers/games.py`, `routers/jobs.py`, `routers/platforms.py`) —
    per-row response-model construction wrapped in `try/except ValidationError`.
    Out-of-Literal DB values (CHECK-constraint drift, raw SQL writes) now drop
    the offending row with a structured `api.{entity}.row_dropped` log instead
    of crashing the whole request to 500.
  - **U5-3 [SEV-2]** (`routers/games.py`, `routers/jobs.py`) — defensive
    `isinstance(raw_meta, (str, bytes, bytearray))` guard before `len()` on
    metadata/payload bytes. Future pool drivers that return non-buffer types
    (dict, int) no longer raise unhandled TypeError to 500.
  - **U5-4 [SEV-2]** (`_query_helpers.py`) — `_coerce_value` rejects
    non-finite floats (`NaN`, `Infinity`, `-Infinity`). Previously these
    flowed through to `json.dumps` and crashed to 500; now they 400 with a
    clear `value must be finite` message.
  - **U5-5 [SEV-2]** (`routers/platforms.py`) — platforms now rejects any
    query parameter with 400 for cross-router consistency. Previously
    `?password=foo` silently returned 200 (the other 3 F9 endpoints all 400).
  - **U5-6 [SEV-2]** (`routers/platforms.py`) — added `PlatformsMeta` to the
    response envelope (`{platforms, meta}`). Envelope shape now matches
    games/jobs/manifests; meta carries `total` plus empty
    `applied_filters`/`applied_sort` (platforms doesn't paginate or filter).
  - **U5-8 [SEV-3]** (`routers/games.py`, `routers/jobs.py`) — both routers
    declare an empty `IncludeAllowList` and call `parse_includes`. Any
    `?include=foo` value now rejects with 400; previously silently ignored.
    Locks in the BL9 convention so future typos surface.

  See [UAT-5 session](tests/uat/sessions/2026-05-20-session-5/) for full
  consolidated findings + 4 individual + 2 umbrella issues filed (#78-#87).

### Added
- **BL10 — Steam authentication substrate** (F1 milestone, BL10/3). First
  real data-ingestion feature substrate. Subprocess-isolated steam-next
  worker (gevent-patched, separate venv) communicates with the asyncio
  orchestrator via newline-delimited JSON over stdin/stdout pipes.
  New endpoints:
  - `POST /api/v1/platforms/steam/auth` (loopback-only) — initiates
    Steam login; returns `200` (no 2FA) or `202 + challenge_id` (2FA
    required).
  - `POST /api/v1/platforms/steam/auth/{challenge_id}` (loopback-only) —
    completes 2FA with a code; 5-min TTL on challenges.
  - `GET /api/v1/platforms/steam/auth/status` (bearer; NOT loopback-only;
    Game_shelf reads it).

  Session persistence: steam-next manages its own credential dir at
  `/var/lib/orchestrator/steam_session/` (mode 0700); the orchestrator
  writes a metadata JSON at `/var/lib/orchestrator/steam_session.json`
  (mode 0600) — NEVER contains tokens, only `{steam_id, username,
  last_refreshed_at, sha256_prefix, auth_method_version}`. Atomic write
  via `os.replace` from a tempfile.

  `platforms` table updates: `auth_status` transitions `never → ok` or
  `→ error`; `last_sync_at` updated on success; `last_error` populated
  on failure (truncated to 200 chars); `config` JSON has `{steam_id,
  username, last_refreshed_at}` — NEVER tokens (D12).

  Settings additions: `steam_worker_python_path`,
  `steam_worker_ipc_timeout_sec` (default 30), `steam_worker_max_restart_attempts`
  (default 3), `steam_session_dir`, `jobs_worker_poll_interval_sec`
  (used in BL11; pinned here for venv-shape stability).

  See [F1 spec](docs/superpowers/specs/2026-05-24-f1-steam-credentials-fetcher-design.md)
  and [ADR-0013](docs/ADR%20documentation/0013-steam-subprocess-isolation.md)
  for full architecture.
- **`GET /api/v1/manifests`** (BL9 / Feature 9 partial) — third paginated F9
  read endpoint, introduces the **`?include=` opt-in expansion convention**.
  Default sort `fetched_at:desc` (matches `idx_manifests_game_fetched`).
  Per-endpoint filterable: `game_id` (eq, `_in`), `version` (eq, `_in`),
  `fetched_at` (range), `chunk_count` (range), `total_bytes` (range).
  Sortable: `id`, `game_id`, `version`, `fetched_at`, `chunk_count`,
  `total_bytes`. `raw` BLOB column intentionally excluded. With
  `?include=game`, the response embeds a `game: {title, platform, app_id}`
  summary via a follow-up `WHERE id IN (...)` games lookup keyed by the
  distinct game_ids on the page (switched from the LEFT JOIN spec'd in D7
  to avoid an ambiguous-`id` issue with the unqualified ORDER BY tie-breaker
  — same wire behavior, cleaner SQL). Adds `IncludeAllowList` +
  `parse_includes` to `_query_helpers.py` (+~30 LoC, identifier-validated
  + `"include"` reserved) — future endpoints can opt-in to FK expansion
  cheaply. See
  [spec](docs/superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md)
  and [audit](docs/security-audits/bl9-f9-manifests-readonly-security-audit.md).
- **`GET /api/v1/jobs`** (BL8 / Feature 9 partial) — second paginated F9
  read endpoint. Returns the orchestrator jobs feed with filter, sort,
  and pagination. Default sort `id:desc` (most-recently-created first);
  active jobs surface via `?state_in=queued,running`. Per-endpoint
  filterable: `kind`, `game_id`, `platform`, `state`, `progress` (range),
  `source`, `started_at`/`finished_at` (range). Sortable: `id`, `kind`,
  `state`, `progress`, `started_at`, `finished_at`. `payload` JSON column
  included as parsed dict (UAT-4 hardening: 64 KiB cap + RecursionError
  catch + null on parse failure); `error` truncated to 200 chars.
  Validates the proposition that BL7+UAT-4-hardened `_query_helpers.py`
  conventions propagate cheaply — **zero changes to the shared module**.
  See [spec](docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md)
  and [audit](docs/security-audits/bl8-f9-jobs-readonly-security-audit.md).
- **`GET /api/v1/games`** (BL7 / Feature 9 partial) — first paginated F9
  read endpoint. Returns the games library with filter (operator-suffix
  syntax: `field`, `field_in`, `field_gte`, `field_lte`), sort (multi-field
  with `:asc`/`:desc` + server-appended `id:asc` tie-breaker with
  de-duplication), and offset-based pagination (default 50, max 500,
  reject 400 above max). Rich meta envelope: `total`, `limit`, `offset`,
  `has_more`, `applied_filters`, `applied_sort`. New shared module
  `src/orchestrator/api/_query_helpers.py` provides parser/validator/SQL
  builder primitives reusable by every future paginated F9 endpoint
  (`/jobs`, `/manifests`, etc.). `metadata` column included as parsed JSON
  (null on parse failure); `last_error` truncated to 200 chars (BL6
  pattern). Pool failures translate to 503 with structured
  `api.games.read_failed` log. SQL injection resistance pinned by both
  unit tests and a Hypothesis property test. See
  [spec](docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md)
  and [audit](docs/security-audits/bl7-f9-games-readonly-security-audit.md).
- **`GET /api/v1/platforms`** (BL6 / Feature 9 partial) — first real
  domain endpoint on the BL5 substrate. Returns the auth + sync status
  of every configured platform, with Steam pinned first in the response
  order. Six fields per platform (name, auth_status, auth_method,
  auth_expires_at, last_sync_at, last_error); `config` column
  intentionally excluded from the response surface. `last_error`
  truncated to 200 chars at the API layer (defense-in-depth on top of
  upstream redaction). Pool failures translate to HTTP 503 with a
  structured `api.platforms.read_failed` log event. Locks the wrapped
  envelope shape `{"<resource>": [...]}` that every future F9 read
  endpoint will inherit. See
  [spec](docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md)
  and [audit](docs/security-audits/bl6-f9-platforms-readonly-security-audit.md).
- **FastAPI app skeleton** (BL5 / Feature 5) — `create_app()` factory at
  `src/orchestrator/api/main.py`. Lifespan runs migrations + initializes
  the BL4 pool singleton on startup; closes the pool with the BL4 30 s
  hard timeout on shutdown. Run with
  `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`.
  See [ADR-0012](docs/ADR%20documentation/0012-fastapi-skeleton-architecture.md).
- **`GET /api/v1/health`** endpoint per Bible §8.4. Returns the 7-field
  response (status / version / uptime_sec / scheduler_running /
  lancache_reachable / cache_volume_mounted / validator_healthy /
  git_sha) with HTTP 200 if all subsystems healthy, 503 otherwise.
  **Note:** BL5 ship state intentionally returns 503 because three
  subsystems (`scheduler_running`, `lancache_reachable`,
  `validator_healthy`) are stub-false until BL6+ flips them as features
  land. Container HEALTHCHECK and k8s liveness probes should expect 503
  during this transition.
- **OpenAPI schema** at `/api/v1/openapi.json`, **Swagger UI** at
  `/api/v1/docs`, **ReDoc** at `/api/v1/redoc`. `bearerAuth`
  security_scheme registered so Swagger UI's Authorize button works.
- **`asgi-lifespan==2.1.0`** added to `requirements-dev.txt` for
  test-time lifespan integration.

### Security
- **TM-013 fingerprinting defense:** bearer-auth implemented as pure
  ASGI middleware (not FastAPI Depends), so 404s on non-exempt paths
  also require auth. Returns 401 with timing-safe `hmac.compare_digest`
  comparison (UTF-8-encoded bytes, length-tolerant).
- **OQ2 loopback enforcement:** path pattern
  `^/api/v1/platforms/[^/]+/auth$` additionally requires
  `request.client.host == "127.0.0.1"`. The route is reserved in BL5
  (the actual handler lands in F1/F2); the middleware logic is in
  place so BL6+ inherits enforcement automatically.
- **TM-012 log redaction:** rejected bearer tokens logged with
  `rejection_fingerprint` (8 hex of SHA-256, non-reversible). Field
  name avoids the "token"/"auth"/"bearer"/"secret" keywords because
  ID3's `_redact_sensitive_values` would auto-redact them. Verified by
  `test_no_raw_token_in_logs` and `test_auth_rejected_event_emits_with_sha256_prefix`.
- **TM-018 memory bomb defense:** ASGI middleware enforces 32 KiB
  request-body cap (Bible §9.2). Two paths: Content-Length proactive
  check (immediate 413 before any read); streaming check via
  `receive()` interception (interrupts mid-stream when accumulated
  bytes exceed cap). Streaming variant verified by direct middleware
  unit test against a fake downstream app.
- **CORS hardened:** `allow_credentials=False`. Bearer-token auth flows
  in `Authorization` header, not cookies — closes the
  `allow_origins=*` + `allow_credentials=true` footgun by constraint.
  `allow_headers` whitelist: `Authorization`, `Content-Type`,
  `X-Correlation-ID`. `expose_headers`: `X-Correlation-ID` (for
  Game_shelf to log + correlate API calls).
- **Correlation-ID propagation:** outermost middleware enters ID3's
  `request_context()` per-request. Echoed in response header. Every
  log line during request processing carries the correlation_id via
  structlog contextvar — downstream debugging can grep one CID for
  the full request trace.

#### UAT-3 remediation (2026-04-30)

Empirical UAT pass plus 5 parallel agents surfaced 11 SEV-2 + 4 SEV-3
items against the BL5 surface; all live + queued items fixed test-first
in this revision. See
`tests/uat/sessions/2026-04-27-session-3/agent-results/_consolidated.md`
and `tests/api/test_uat3_remediation.py` (28 new regression tests).

- **S2-A — exempt-prefix exact-match.** `BearerAuthMiddleware` now uses
  exact-or-subpath matching keyed on a per-path `allow_subpaths` flag
  (`AUTH_EXEMPT_PATHS` in `dependencies.py`) instead of unanchored
  `startswith`. Closes the latent foot-gun where a future route like
  `/api/v1/healthcheck` would silently bypass auth.
- **S2-B — `git_sha` recon defense.** `/api/v1/health` truncates the
  `git_sha` field to 8 chars before returning it. Operators with CI
  pipelines that set `GIT_SHA` to the full 40-char commit hash no
  longer leak it pre-auth.
- **S2-C + S3-h — schema/UI loopback restriction.** `/api/v1/openapi.json`,
  `/api/v1/docs` (+ `/oauth2-redirect`), and `/api/v1/redoc` are now
  gated behind the OQ2 loopback check. Loopback access works (developers
  can browse Swagger), LAN access returns 403. IPv6 forms (`::1`,
  `::ffff:127.0.0.1`) are honored alongside IPv4 `127.0.0.1`.
- **S2-D — non-loopback bind warning.** Lifespan emits
  `api.boot.non_loopback_bind_warning` at WARNING when `api_host !=
  "127.0.0.1"`, with explicit hint about reverse-proxy OQ2 bypass risk.
  Phase 3 backlog item: optional `OQ2_TRUSTED_PROXIES` allowlist.
- **S2-F — CORS outermost.** Middleware order revised: CORS now wraps
  CorrelationId/BodySizeCap/BearerAuth so 401/413 short-circuit
  responses include `Access-Control-Allow-Origin` headers. Operators
  see real status codes in the browser instead of the misleading
  "CORS error" mask. Trade: CORS-rejected preflights lack a
  correlation_id (those rejections are rare and client-misconfigured).
- **S2-G — no duplicate `http.response.start`.** `BodySizeCapMiddleware`
  tracks a `response_started` flag in the wrapped send; if the cap
  trips after the downstream handler has begun streaming a response,
  the middleware logs and lets the connection close naturally instead
  of emitting a protocol-violating second start frame.
- **S2-I — module-level `app`.** `orchestrator.api.main` exposes a
  lazy `app` attribute via PEP 562 `__getattr__`. Standard
  `uvicorn orchestrator.api.main:app` and Dockerfile `CMD ["uvicorn",
  "orchestrator.api.main:app", ...]` patterns now work without the
  `--factory` flag. Lazy construction means just importing the module
  (e.g. for `create_app` in tests) doesn't load settings.
- **S3-m — RFC 7235 case-insensitive Bearer scheme.** Middleware
  accepts `bearer`, `BEARER`, `BeArEr`, etc. — HTTP scheme is
  case-insensitive per RFC 7235 §2.1.

### Changed
- **Middleware ordering revised** (UAT-3 S2-F). New outermost-→innermost
  order: `CORS → CorrelationId → BodySizeCap → BearerAuth`. Spec §5.1
  language updated; ADR-0012 D5 superseded by ADR-0012 addendum.
- **`AUTH_EXEMPT_PREFIXES` → `AUTH_EXEMPT_PATHS`.** Now a tuple of
  `(path, allow_subpaths)` pairs. Backwards-compatibility shim
  `AUTH_EXEMPT_PREFIXES = tuple(p for p, _ in AUTH_EXEMPT_PATHS)` kept
  for any external import.

### Fixed
- **S2-J — migration runner wraps `sqlite3.OperationalError`** as
  `MigrationError` so the lifespan's catch-and-`SystemExit(1)` contract
  holds for the most common operator failures (bad path, permission
  denied, read-only filesystem). Without this, raw sqlite3 errors
  produced a 50-line traceback instead of the documented structured
  `api.boot.migrations_failed` event.
- **S3-a — lifespan partial-init cleanup.** Post-init steps run inside
  `try/finally`; if any step after `init_pool()` raises, `close_pool()`
  still executes so writer/reader connections aren't leaked at process
  death.
- **S3-k — ASGI-headers redaction.** `_redact_sensitive_values` now
  detects the list-of-(bytes,bytes)-tuples shape used by `scope["headers"]`
  and applies the sensitive-key regex per pair. Eliminates the latent
  bypass if any future code logs `scope=scope`.

- **Async DB pool** (`src/orchestrator/db/pool.py`, BL4 / Feature 4) —
  hybrid 1-writer-N-reader topology on top of `aiosqlite`. Defense-in-depth
  write serialization (`asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout`).
  Comprehensive API: `read_one`/`read_all`/`read_one_as`/`read_all_as`/
  `read_stream`/`execute_write`/`execute_many_write` single-statement
  helpers, `read_transaction`/`write_transaction` multi-statement contexts,
  `acquire_reader`/`acquire_writer` raw-connection escape hatches. Module-
  level singleton (`init_pool`/`get_pool`/`reload_pool`/`close_pool`).
  See [ADR-0011](docs/ADR%20documentation/0011-db-pool-architecture.md).
- **`migrate.verify_schema_current()`** — async helper that asserts the
  applied migration set matches the packaged manifest. Called by
  `Pool.create()` unless `skip_schema_verify=True` (which logs
  `pool.schema_verification_skipped` at WARNING).
- **`pool.schema_status()`** — read-only introspection surface for
  `/api/v1/health` consumers; returns `{applied, available, pending,
  unknown, current}`.
- **`pool.health_check()`** — concurrent per-connection probe with 1 s
  per-probe timeout. Reports writer + reader health, replacement counts,
  uptime.
- **5 new typed Settings fields** (`pool_readers`, `pool_busy_timeout_ms`,
  `db_cache_size_kib`, `db_mmap_size_bytes`, `db_journal_size_limit_bytes`)
  driving pool sizing and SQLite PRAGMA tunables. See ADR-0010 addendum.
- **`config.pool_readers_over_provisioned` diagnostic warning** — fires
  when `pool_readers > chunk_concurrency` (readers will idle).

### Security
- **No raw SQL or parameter values reach log output** (TM-012). Every
  `pool.*` log emission uses `_template_only(sql)` (literals replaced
  with `?`) and `_shape(params)` (parameter type names only, never values).
  Hypothesis property tests in `tests/db/test_pool_property.py` exercise
  the scrubbers across arbitrary value shapes; capsys-based regression
  tests verify end-to-end log scrubbing through the structlog JSONRenderer.
- **Reader connections are read-only at the SQLite layer.** `PRAGMA
  query_only=ON` applied after open; writes through a reader handle fail
  with `OperationalError("readonly database")`. Defense-in-depth alongside
  the application-level reader/writer split.
- **PRAGMA verification at boot.** Each of 9 PRAGMAs (busy_timeout,
  foreign_keys, synchronous, temp_store, cache_size, mmap_size,
  journal_size_limit, plus reader-only query_only) is set then read back;
  mismatch raises `PoolInitError(role=...)` and aborts pool startup.
  Defends against silent SQLite ABI changes that could drop a PRAGMA.
- **Connection-replacement storm guard.** Per-role 60-second sliding
  window; >3 replacements trips the guard and refuses further auto-recovery
  (pool transitions to degraded; operator must `reload_pool()`).
  Prevents disk-failure storms from amplifying into infinite-reconnect
  CPU/IO loops.
- **Background-task error logging** (SEV-3 finding from Phase 2.4 audit,
  fixed inline). Replacement and safe-close tasks now register a done
  callback that logs `pool.background_task_failed` at ERROR with task
  name, error message, and error type. Without this, replacement
  failures would have been silently swallowed by asyncio defaults.
- Correlation-ID leak fix: `request_context()` now uses structlog's
  token-based reset, so nested context managers restore the outer block's
  CID rather than wiping all contextvars. Eliminates cross-request bleed
  via pooled workers that was the core risk behind issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9).
- User kwargs that collide with framework-owned reserved keys
  (`correlation_id`, `level`, `timestamp`, `event`, `logger`, `logger_name`)
  are now rescued to `user_<key>` (with numbered-slot collision handling)
  rather than silently overriding. Protects audit-trail integrity against
  attacker-controlled input reaching `log.info(**user_dict)`. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Recursive secret-value redaction: any log-event key matching the
  sensitive-key regex (password, passwd, passphrase, token, jwt, secret,
  authorization, bearer, cookie, session, api_key, apikey, credential,
  private_key, privkey, signature, plus letter-bounded pwd/pin/otp/mfa/
  tfa/sid/creds/salt/nonce) has its value replaced with `<redacted>`
  before the JSONRenderer sees it. Walks nested dicts and lists.
  Cycle-safe — a self-referential structure is substituted with
  `<cyclic>` rather than blowing the stack. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14),
  re-audit N3+N4)
- Migrations runner now refuses to boot on network filesystems (NFS, CIFS,
  SMB, GlusterFS, Ceph, Lustre, BeeGFS, GPFS, OCFS2, GFS2, MooseFS, plus
  FUSE-backed `sshfs`/`cifs`/`smb`/`glusterfs`/`s3fs`/`gcsfuse`/`goofys`).
  Opt-in `ORCH_REQUIRE_LOCAL_FS=strict` upgrades unknown-fs to hard failure
  for deployments where silent WAL corruption is worse than refusing to
  start. (Issues [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12), re-audit F1+F2)
- Pinned SHA-256 checksums for every packaged migration in a new
  `CHECKSUMS` manifest. Tamper of an unapplied migration is now detected
  before apply. Supply-chain defense: an attacker modifying a migration
  file must also modify the manifest in the same commit. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- Post-apply schema-object sanity check derived from each migration's SQL
  now runs inside the transaction before COMMIT, so a failure triggers
  ROLLBACK. Prevents the boot-loop failure mode where `schema_migrations`
  claims migrations are applied but the expected tables are missing. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6), re-audit F6)

### Data Model
- `0001_initial.sql` relocated to the `orchestrator.db.migrations` Python
  subpackage. Runner now loads migrations via `importlib.resources.files()`
  rather than a `__file__`-relative filesystem path — mitigates the
  "attacker-writable app dir → arbitrary DDL on restart" class of risk.
  (Issue [#13](https://github.com/kraulerson/lancache-orchestrator/issues/13))
- Header comment in `0001_initial.sql` corrected — previous version
  falsely claimed atomicity that the implementation didn't deliver.

### Added
- `MigrationError` typed exception for all migrations-framework failures.
- `tests/db/test_migrate.py` (42 tests) covering every UAT-1 finding and
  every re-audit hardening item.
- `docs/security-audits/id1-sqlite-migrations-security-audit.md` records
  the full pre- and post-fix audit trail.
- ADR-0008 documents the atomicity / checksum / packaging decisions.
- `orchestrator.core.logging.request_context()` context manager for
  scoped correlation-ID binding. Supersedes the raw `bind_correlation_id()`
  + `clear_request_context()` pair, which remain as low-level primitives.
- Public `RESERVED_KEYS` constant exported from `orchestrator.core.logging`.
- `tests/core/test_logging.py` (55 tests) covering every UAT-1 + re-audit
  logging finding.
- `docs/security-audits/id3-structured-logging-security-audit.md` records
  the logging audit trail.
- ADR-0009 documents the scoped-context / reserved-key / redaction /
  log-level-validation decisions.
- `src/orchestrator/core/settings.py` (ID4) — typed application configuration
  via pydantic-settings `BaseSettings`. 16 fields covering API (`api_host`,
  `api_port`, `cors_origins`, `log_level`, `orchestrator_token`), database
  (`database_path`, `require_local_fs`), platform sessions
  (`steam_session_path`, `epic_session_path`), Lancache cache topology
  (`lancache_nginx_cache_path`, `cache_slice_size_bytes`, `cache_levels`,
  `chunk_concurrency`), and miscellaneous (`manifest_size_cap_bytes`,
  `epic_refresh_buffer_sec`, `steam_upstream_silent_days`). Defaults
  sourced from Bible §7.2/§7.3/§9, Spike F, and the Lancache deployment
  params memory.
- `orchestrator.core.settings.get_settings()` — `@lru_cache` singleton
  accessor. `reload_settings()` provided as a test / SIGHUP escape hatch.
- Four diagnostic `@model_validator(mode="after")` warnings:
  `config.secret_shadowed_by_env` (env and `/run/secrets` both set),
  `config.api_bound_non_loopback` (`api_host` isn't loopback),
  `config.cors_wildcard` (`"*"` in `cors_origins`),
  `config.chunk_concurrency_unvalidated` (`chunk_concurrency > 32`, the
  Spike F gate ceiling).
- `tests/core/conftest.py` — shared autouse `_isolated_env` fixture that
  scrubs `ORCH_*` env vars, chdirs to `tmp_path` (blocks host `.env`
  discovery), resets structlog defaults + contextvars (matching the ID3
  test pattern), and clears the `get_settings()` cache before and after
  every test in `tests/core/`.
- `tests/core/test_settings.py` (67 tests) — full coverage of required
  fields, the 15 optional defaults, field validators, source precedence,
  secret-loading paths, 5-shape × 3-serialization redaction parametrize,
  4 warnings + 1 negative case, singleton behavior, and 2 SEV-2
  regression tests (pickle-block, ValidationError scrubbing).
- `docs/security-audits/id4-settings-security-audit.md` records the
  audit trail.
- ADR-0010 documents the flat-layout / source-order / singleton /
  redaction-layer / validation-scope decisions.

### Changed
- `run_migrations()` rewritten: explicit `BEGIN IMMEDIATE` wraps the whole
  read+apply pass; PRAGMAs run outside any transaction; per-statement
  `conn.execute()` inside the transaction (instead of `executescript()`,
  which auto-commits and defeated atomicity). (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Gap migrations are now rejected with a hard error naming the missing ID,
  instead of being silently skipped. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Concurrent runners serialize cleanly via `PRAGMA busy_timeout = 5000`
  combined with the single `BEGIN IMMEDIATE`; the losing runner no-ops
  after re-reading `applied_map`. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- `configure_logging(log_level=...)` now validates input against
  `{DEBUG, INFO, WARNING, ERROR, CRITICAL}` (case-insensitive, stripped).
  Raises `ValueError` on anything else instead of silently falling back
  to INFO — operator typos in `LOG_LEVEL` surface at startup rather than
  at incident time. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))

### Fixed
- Migration atomicity — see Security/Changed entries above. (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Silent-skip of gap / out-of-order migrations. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Drift detection on unapplied migrations. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- `schema_migrations` tamper bypass. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6))
- Concurrent-runner race. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- WAL journal-mode unconditionally set without FS probe. (Issue [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12))
- Correlation-ID context bleed across pooled workers. (Issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9))
- Reserved-key clobber from user kwargs. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Missing PII/secret redaction in log values. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14))
- `log_level` silent fallback to INFO on typo. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))
- Short-token redaction regex silently failed on `user_pwd` / `my_pin` /
  `otp_code` / `creds_list` etc. shapes because Python `\b` uses `\w`
  boundaries and `_` is `\w`. Replaced with letter-class boundaries.
  **Caught and fixed before ship** by the BL2 re-audit pass. (Re-audit N3)
- Settings module redaction primitives: `SecretStr` is supplemented by
  a `__reduce__` override that blocks pickling (pydantic's default
  pickler serialises `_secret_value` cleartext, which any future DX
  sugar like multiprocessing task args or Celery would write to an
  attacker-readable queue). `Settings.__init__` intercepts pydantic's
  `ValidationError` for token-field failures and re-raises as
  `ValueError` with a scrubbed message — pydantic core otherwise
  echoes the raw rejected token in `input_value`, which a rotation-
  failure startup would land in the systemd journal. **Caught and
  fixed before ship** by the BL3 re-audit pass. (Audit A1 + A2)

### Removed
- `migrations/0001_initial_down.sql` and all doc references to
  `orchestrator-cli db rollback`. Rollback is intentionally out of MVP
  scope; re-introducing it will require a dedicated ADR covering
  versioning and data-preservation policy. (Issue [#7](https://github.com/kraulerson/lancache-orchestrator/issues/7))
- Top-level `migrations/` directory (contents moved into the package).

### Infrastructure
- `pyproject.toml`: added `[tool.setuptools.package-data]` to ship
  `*.sql` + `CHECKSUMS` inside the `orchestrator.db.migrations` package.
  Per-file ruff `S101/S105/S106` ignore for `tests/core/test_logging.py`
  (redaction tests necessarily include fake credential literals as inputs).
- `Dockerfile`: removed the `COPY migrations/ /app/migrations/` step
  (migrations now ride along inside the installed wheel).
- `.semgrep/orchestrator-rules.yaml`: `no-sync-sqlite` rule now excludes
  `tests/db/test_migrate.py`. `no-credential-log` rule now excludes
  `tests/core/test_logging.py` — redaction tests verify the processor by
  logging literal credential-named kwargs and asserting the value becomes
  `<redacted>`.

### Documentation
- New ADR: [`ADR-0008 — Migration Runner Architecture`](docs/ADR%20documentation/0008-migration-runner-architecture.md).
- New ADR: [`ADR-0009 — Logging Framework Architecture`](docs/ADR%20documentation/0009-logging-framework-architecture.md).
- New audit artifacts:
  `docs/security-audits/id1-sqlite-migrations-security-audit.md`,
  `docs/security-audits/id3-structured-logging-security-audit.md`, and
  `docs/security-audits/id4-settings-security-audit.md`.
- FEATURES.md now documents Feature 1 (ID1 migrations), Feature 2
  (ID3 structured logging), and Feature 3 (ID4 settings module) with
  links, known limitations, and test-coverage summaries.
- New ADR: [`ADR-0010 — Settings Module Design`](docs/ADR%20documentation/0010-settings-module-design.md).
- Design spec at `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
  and implementation plan at `docs/superpowers/plans/2026-04-23-id4-settings-module.md`
  record the 14-decision brainstorm and 11-task execution trail for BL3.
