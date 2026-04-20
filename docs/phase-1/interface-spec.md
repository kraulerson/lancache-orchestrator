# Interface Specification — lancache_orchestrator

**Phase:** 1
**Step:** 1.5
**Generated from:** Manifesto §2 (F9–F11, F14–F17) + Data Contract §2, §4 + ADR-0001 + Threat Model §2 + Intake §9 (colorblind-safe hard constraint)
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

---

## 0. Scope Statement

The orchestrator has **no user-facing UI of its own.** Per Builder's Guide §1.5, for CLI/API/background-service projects we document interface specifications instead of screen layouts. Three interface surfaces are in MVP scope:

1. **CLI (`orchestrator-cli`)** — F11. Click-based, bundled in the container image.
2. **REST API (`/api/v1/*`)** — F9. FastAPI on port 8765, bearer-auth, consumed by Game_shelf + CLI + status page.
3. **Status page (`GET /`)** — F10. Single-file HTML diagnostic dashboard.

A fourth surface — **Game_shelf's cache UI** (F14 proxy + F15 badge/panel + F16 dashboard) — lives in the `kraulerson/Game_shelf` repo and consumes the orchestrator's REST API. Its contract with us is covered in §5 but its component specifications are Game_shelf-repo deliverables, not ours.

**Four-state discipline (Empty / Loading / Error / Success)** applies to every interactive component in every surface. Stated per component below.

**Colorblind-safe (Intake §9 hard constraint)** applies to every status indicator everywhere: **color + icon + text label** on every state, in every surface, always. Color alone is never the signal.

---

## 1. CLI Specification

### 1.1 Invocation

```
docker compose exec orchestrator orchestrator-cli <subcommand> [options]
```

The CLI binary is on `$PATH` inside the container image. Runs as the non-root container user. Reads the same `/run/secrets/orchestrator_token` at startup to authenticate against `http://127.0.0.1:8765/api/v1/*` (loopback; bypasses pfSense rules).

**Connection target:** `http://127.0.0.1:8765/api/v1`. Hard-coded — CLI does not accept `--url` (security: CLI must not be pointed at a different orchestrator by mistake).

**Transport:** `httpx.Client` (sync, not async — this is a CLI). 5 s connect timeout, 30 s read timeout.

### 1.2 Exit Codes (consistent across all subcommands)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad input, unknown argument, validation failure) |
| 2 | Environment error (API unreachable, secret missing, DB locked) |
| 3 | Authentication error (401 from API, or Steam/Epic rejects auth) |
| 4 | Upstream error (Steam CM down, Epic services degraded, Lancache unreachable) |
| 130 | SIGINT from user (Ctrl+C) — clean exit, partial state logged |

Documented in `orchestrator-cli --help` footer and in Phase 4 HANDOFF.md.

### 1.3 Command Tree

```
orchestrator-cli
├── auth
│   ├── steam                 Interactive Steam login (F1)
│   ├── epic                  Interactive Epic auth-code exchange (F2)
│   └── status                Show per-platform auth state
│
├── library
│   └── sync [--platform X]   Force library enumeration now (F3/F4)
│
├── game <PLATFORM/APP_ID>
│   ├── (default: show)       Show full state for this game
│   ├── validate              Enqueue F7 validation
│   ├── prefill [--force]     Enqueue F5/F6 prefill (respects block list unless --force)
│   ├── block [--reason TXT]  Add to block_list (F8)
│   └── unblock               Remove from block_list
│
├── jobs
│   ├── (default: list)       List recent jobs (default 20, --limit to adjust)
│   ├── --active              List only queued + running
│   ├── <JOB_ID>              Show single job detail
│   └── cancel <JOB_ID>       Request cancellation of a running job (Post-MVP)
│
├── db
│   ├── migrate               Apply pending migrations (no-op if at head)
│   ├── rollback <VERSION>    Destructive — requires --yes-i-know-this-is-destructive
│   └── vacuum                Manual SQLite VACUUM (maintenance)
│
├── config show               Print effective config (env-derived; secrets redacted)
│
└── --help / -h               Click auto-generated help
```

### 1.4 Component states per interactive subcommand

Interactive subcommands (`auth steam`, `auth epic`, `db rollback`) have four states each.

#### `orchestrator-cli auth steam`

| State | What the user sees | Exit code |
|---|---|---|
| **Empty** (no prior session, first run) | `No existing Steam session found.` followed by username/password/Steam-Guard prompts (JQ1-conditional — discriminates mobile vs email type if steam-next exposes it). | 0 on success |
| **Loading** | `[INFO] Connecting to Steam CM...` · `[INFO] Steam Guard validated.` · `[INFO] Persisting refresh token to /var/lib/orchestrator/steam_session.json`. Printed line-by-line as events happen. | — |
| **Error** | Distinguishes: bad password (`Steam rejected credentials — check username/password.`) → 3; Steam Guard expired (`Steam Guard code expired — run the command again and paste the code within 30 seconds.`) → 3; Steam CM unreachable (`Cannot reach Steam CM servers.`) → 4; mobile approval timeout (`Steam is waiting for mobile approval. Approve in your Steam mobile app, then re-run this command.`) → 3. | 1, 3, or 4 |
| **Success** | `SUCCESS: Steam authenticated. Expires: 2027-04-20.` + `Note: Valve will send a "new device login" email to your registered address. This is expected. Do not change your password.` | 0 |

#### `orchestrator-cli auth epic`

| State | What the user sees | Exit code |
|---|---|---|
| **Empty** | Prints `Open this URL in a browser, log in to Epic, and paste the auth code below:` + `  https://legendary.gl/epiclogin` + prompt `Auth code: `. | 0 on success |
| **Loading** | `[INFO] Exchanging auth code...` · `[INFO] Persisting tokens...` · `[INFO] Token rotates silently after first use (no further re-login required unless revoked).` | — |
| **Error** | Bad code (`Epic rejected the code — it may have expired. Get a fresh code at https://legendary.gl/epiclogin.`) → 3; Epic unreachable → 4; accidental full-URL paste (`Detected URL; extracted code=...`) is non-error — auto-corrects and proceeds. | 1, 3, or 4 |
| **Success** | `SUCCESS: Epic authenticated.` | 0 |

#### `orchestrator-cli db rollback <VERSION>`

| State | What the user sees | Exit code |
|---|---|---|
| **Empty** (no `--yes-i-know-this-is-destructive` flag) | `ERROR: This operation is destructive. Add --yes-i-know-this-is-destructive to confirm.` | 1 |
| **Loading** | `[INFO] Rolling back from version N to M...` · `[INFO] Applying 000N_*_down.sql...` | — |
| **Error** | Target version doesn't exist / file missing → 1. DB locked (active F5 or F13) → 2 with `Stop the orchestrator before rolling back: docker compose stop orchestrator`. | 1 or 2 |
| **Success** | `SUCCESS: Schema rolled back to version M. You may now start the orchestrator at an older image version if needed.` | 0 |

### 1.5 Non-interactive subcommands (single state each)

Non-interactive subcommands (`jobs`, `game`, `library sync`, `auth status`, `config show`, `db migrate`, `db vacuum`) emit either a formatted success output or a single-line error.

Example — `orchestrator-cli game steam/570 validate`:

```
$ orchestrator-cli game steam/570 validate
Enqueued validation job #241 for steam/570 (Dota 2).
```

On API unreachable:

```
$ orchestrator-cli game steam/570 validate
ERROR: Orchestrator API unreachable — is the service running? (docker compose ps)
```
Exit 2.

On 401:

```
$ orchestrator-cli game steam/570 validate
ERROR: Token mismatch — the orchestrator_token file has changed. Restart the container.
```
Exit 3.

**`--json` flag is NOT in MVP** (OQ6 resolution).

### 1.6 CLI Output Conventions (colorblind-safe)

- **ANSI colors ONLY as additive signal**, never as primary meaning.
- Every status line uses a text prefix:
  - `[OK] ...` for success (optional green).
  - `[WARN] ...` for warnings (optional amber).
  - `[ERROR] ...` for errors (optional red).
  - `[INFO] ...` for progress (optional default/white).
- The text prefix is the sole signal. Stripping ANSI (e.g., piping to a file) preserves full meaning.
- No Unicode-icon-only indicators (✓, ✗, ⚠). Text-only.
- Tables use `rich` library with `box=SIMPLE` for unambiguous whitespace alignment.

### 1.7 CLI Error Handling Patterns

- **No stack traces in user-facing output** — exceptions caught at the top level, rendered as single-line `ERROR: ...` messages. Full traceback logged to stderr only when `--debug` flag is set.
- **Never `shell=True`** — no subprocess shell invocation anywhere (threat-model TM-021 mitigation).
- **Click structured arguments only** — `click.argument('game_ref')` with custom validator that splits on `/` and validates each part.
- **Credential handling** — `getpass.getpass()` for password input (no echo). Never written to terminal history, never logged.
- **SIGINT handling** — `click.exceptions.Abort` caught; partial state logged with correlation ID; exit 130.

---

## 2. REST API Specification

### 2.1 Mount

FastAPI app mounted on `/api/v1/*`; aliased under `/api/*` for MVP to simplify Game_shelf migration. Versioning under `/api/v1/` is the stable contract — breaking changes require `/api/v2/`.

Listener: `uvicorn --host 0.0.0.0 --port 8765 --proxy-headers=false`. `proxy-headers=false` because nothing sits in front of us (pfSense is network-layer, not application-layer).

### 2.2 Authentication

- Bearer token required on every endpoint except `GET /api/v1/health`.
- Token loaded at startup from `/run/secrets/orchestrator_token`, stripped of whitespace, minimum 32 chars.
- Comparison: `hmac.compare_digest` (timing-safe — threat-model TM-001 mitigation).
- Missing / malformed `Authorization` header → 401 `{"error": "unauthorized"}`.
- Wrong token → 401 same response (no enumeration leak).
- `POST /api/v1/platforms/{name}/auth` additionally requires `request.client.host == '127.0.0.1'`; remote origins → 403 `{"error": "forbidden_non_local"}` (OQ2 enforcement).

### 2.3 Request / Response Conventions

- **Request size caps:** 32 KiB body, 8 KiB headers (explicit Starlette config).
- **Pydantic `extra='forbid'`** on every request model — unknown fields → 422.
- **Response envelopes:**
  - Read endpoints return the object / array directly.
  - Mutation endpoints return the normalized envelope `{"ok": bool, "job_id": int|null, "message": str|null}` (DQ6).
  - Error responses: `{"error": "<code>", "correlation_id": "<uuid>", "details": "<str|null>"}`.
- **Correlation IDs**: generated at request entry via FastAPI middleware, logged in every structlog entry within the request scope, returned in response headers as `X-Correlation-ID` and in every error body.
- **Timestamps**: ISO 8601 with `Z` suffix (`2026-04-20T13:14:15Z`). Stored as UTC in SQLite, rendered in UTC in responses. Client renders local time.
- **Content-Type**: `application/json; charset=utf-8` for all responses. No HTML, no XML.

### 2.4 Endpoint Inventory with Component States

For each endpoint, the four states (Empty / Loading / Error / Success) map to HTTP-response patterns.

#### `GET /api/v1/health`

Unauthenticated liveness + diagnostic boolean flags.

| State | HTTP | Body example |
|---|---|---|
| Empty | N/A | (health is always defined) |
| Loading | N/A | (synchronous) |
| Error | 503 | `{"status": "degraded", "scheduler_running": false, "lancache_reachable": true, "cache_volume_mounted": true, "validator_healthy": true, "scheduler_last_error": "APSchedulerException: ..."}` |
| Success | 200 | `{"status": "ok", "version": "0.1.0", "uptime_sec": 3847, "scheduler_running": true, "lancache_reachable": true, "cache_volume_mounted": true, "validator_healthy": true, "git_sha": "abc1234"}` (JQ3) |

**Latency SLO:** p99 < 50 ms idle, p99 < 100 ms under Spike F load.

#### `GET /api/v1/games`

Library list. Supports `?platform=steam|epic`, `?status=<status>`, `?limit=N` (default 500, max 5000), `?offset=N`.

| State | HTTP | Body |
|---|---|---|
| Empty | 200 | `[]` (no games yet — first-run, pre-sync) |
| Loading | N/A | (synchronous; DB query) |
| Error | 401 / 503 | Standard error envelope |
| Success | 200 | `[{platform, app_id, title, owned, blocked, status, current_version, cached_version, size_bytes, last_validated_at, last_prefilled_at, last_error}, ...]` |

**Latency SLO:** p99 < 500 ms for 2,200-game library (Intake §2.3 SC).

#### `GET /api/v1/games/{platform}/{app_id}`

Single game detail with recent jobs and validation history embedded.

| State | HTTP | Body |
|---|---|---|
| Empty | 404 | `{"error": "not_found", "correlation_id": "...", "details": "No game with platform=steam app_id=99999"}` |
| Loading | N/A | — |
| Error | 401 / 400 / 500 | Standard envelope |
| Success | 200 | Full game object + `recent_jobs: [...]` (last 5) + `validation_history_latest: {...}` |

#### `POST /api/v1/games/{platform}/{app_id}/validate`

Enqueue validation. Empty body or `{}`.

| State | HTTP | Body |
|---|---|---|
| Empty | 404 | Game not found |
| Loading | N/A | — |
| Error | 401 / 400 / 503 (validator unhealthy) | Standard envelope |
| Success | 202 | `{"ok": true, "job_id": 421, "message": "Validation enqueued"}` |

#### `POST /api/v1/games/{platform}/{app_id}/prefill`

Enqueue prefill. Body `{"force": bool?}` (optional).

| State | HTTP | Body |
|---|---|---|
| Empty | 404 | Game not found |
| Loading | N/A | — |
| Error | 401 / 409 (blocked without force; or already running) / 503 | `{"ok": false, "job_id": 389, "message": "Prefill already running for this game as job #389"}` on 409 |
| Success | 202 | `{"ok": true, "job_id": 422, "message": "Prefill enqueued"}` |

#### `POST /api/v1/games/{platform}/{app_id}/block`

Body `{"reason": str?, "source": enum?}` (both optional).

| State | HTTP | Body |
|---|---|---|
| Empty | N/A (pre-blocking unknown app_ids is allowed per F8) | — |
| Loading | N/A | — |
| Error | 401 / 422 | Standard envelope |
| Success | 201 | `{"ok": true, "job_id": null, "message": "Blocked steam/570"}` |

#### `DELETE /api/v1/games/{platform}/{app_id}/block`

| State | HTTP | Body |
|---|---|---|
| Empty | 200 | `{"ok": true, "job_id": null, "message": "No block found — nothing to remove"}` (idempotent) |
| Loading | N/A | — |
| Error | 401 | Standard envelope |
| Success | 200 | `{"ok": true, "job_id": null, "message": "Unblocked steam/570"}` |

#### `GET /api/v1/platforms`

Returns both platform rows.

| State | HTTP | Body |
|---|---|---|
| Empty | N/A | (seeded at migration time — always 2 rows) |
| Loading | N/A | — |
| Error | 401 / 500 | Standard envelope |
| Success | 200 | `[{"name": "steam", "auth_status": "ok", "auth_method": "steam_cm", "auth_expires_at": "...", "last_sync_at": "...", "last_error": null}, {...}]` |

#### `POST /api/v1/platforms/{name}/auth` (localhost-only — OQ2)

Submit credentials from CLI (which runs inside the container → 127.0.0.1).

| State | HTTP | Body |
|---|---|---|
| Empty | 404 | Unknown platform name |
| Loading | N/A | (synchronous; blocking Steam/Epic auth call) |
| Error | 403 (non-127.0.0.1) / 401 (bearer bad) / 422 (body malformed) / 400 (auth rejected by upstream) | Standard envelope |
| Success | 200 | `{"ok": true, "job_id": null, "message": "Steam auth succeeded. Session persisted. Expires 2027-04-20."}` |

#### `GET /api/v1/jobs`

Active + recent. Supports `?state=queued|running|...`, `?kind=...`, `?limit=N` (default 50, max 500).

| State | HTTP | Body |
|---|---|---|
| Empty | 200 | `[]` (before first cycle) |
| Loading | N/A | — |
| Error | 401 / 422 (bad filter values) | Standard envelope |
| Success | 200 | `[{"id": 421, "kind": "prefill", "game_id": 92, "platform": "steam", "app_id": "570", "title": "Dota 2", "state": "running", "progress": 0.47, "source": "scheduler", "started_at": "...", "finished_at": null, "error": null}, ...]` |

#### `GET /api/v1/jobs/{id}`

| State | HTTP | Body |
|---|---|---|
| Empty | 404 | Unknown job_id |
| Loading | N/A | — |
| Error | 401 / 500 | Standard envelope |
| Success | 200 | Full job object with `payload` JSON |

#### `GET /api/v1/stats`

Aggregate dashboard metrics.

| State | HTTP | Body |
|---|---|---|
| Empty | 200 | `{"cache_disk_free_bytes": 45000000000000, "cache_disk_used_bytes": 12000000000000, "lru_headroom_bytes": 33000000000000, "queue_depth": 0, "active_prefills": 0, "games_total": 2617, "games_cached": 2533, "games_validation_failed": 0, "games_blocked": 12}` |
| Loading | N/A | — |
| Error | 401 / 503 (cache_volume_mounted=false) | Standard envelope |
| Success | 200 | Same shape as Empty, with non-zero values |

**Latency SLO:** p99 < 200 ms.

### 2.5 Pydantic Model Inventory

Summary table; full definitions in Phase 2 code.

| Model | Endpoint(s) | Key fields |
|---|---|---|
| `HealthResponse` | GET /health | `status`, `version`, `uptime_sec`, 4 health booleans, optional `scheduler_last_error` |
| `Game` | GET /games | 14 fields per §2.4 |
| `GameDetail` | GET /games/{p}/{a} | Game + `recent_jobs`, `validation_history_latest` |
| `MutationResponse` | all POST/DELETE | `{ok, job_id, message}` — normalized envelope per DQ6 |
| `BlockRequest` | POST /block | `{reason: str?, source: Literal['cli','gameshelf','api','config']?}` |
| `PrefillRequest` | POST /prefill | `{force: bool?}` |
| `SteamAuthRequest` | POST /platforms/steam/auth | `{username, password, steam_guard?}` |
| `EpicAuthRequest` | POST /platforms/epic/auth | `{auth_code}` |
| `Platform` | GET /platforms | `{name, auth_status, auth_method, auth_expires_at, last_sync_at, last_error}` |
| `Job` | GET /jobs | 12 fields per §2.4 |
| `Stats` | GET /stats | 8 aggregate counters |
| `ErrorResponse` | all error paths | `{error, correlation_id, details?}` |

### 2.6 OpenAPI

FastAPI auto-generates OpenAPI 3.1 at `/api/v1/openapi.json`. Swagger UI at `/api/v1/docs` (bearer-authenticated). Used by Game_shelf's backend codegen in Phase 2 Milestone D.

---

## 3. Status Page Specification (F10)

Single static HTML file served by FastAPI at `GET /`. Polls `/api/v1/*` every 2 s via vanilla JS `fetch()`. No framework, no build step, no npm. File size target < 20 KB gzipped.

### 3.1 Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│  lancache_orchestrator                                 v0.1.0 · 3h 14m │
├────────────────────────────────────────────────────────────────────────┤
│  [Health Panel]                                                         │
│  ─ API: [OK] Scheduler: [OK] Lancache: [OK] Cache Volume: [OK]          │
│  ─ Validator: [OK]                                                      │
├────────────────────────────────────────────────────────────────────────┤
│  [Platforms Panel]                                                      │
│  ─ Steam:  [OK]  last sync 2h 14m ago  · next sync in 3h 46m            │
│  ─ Epic:   [EXPIRED ⚠ Run `orchestrator-cli auth epic` on DXP4800]      │
├────────────────────────────────────────────────────────────────────────┤
│  [Active Jobs Panel]                                                    │
│  ─ #422 [RUNNING] prefill steam/570 Dota 2            [███████░░░] 73%  │
│  ─ #423 [QUEUED]  prefill steam/730 Counter-Strike 2                     │
├────────────────────────────────────────────────────────────────────────┤
│  [Stats Panel]                                                          │
│  ─ Cache: 12.0 TB used of 57.0 TB (21%) · LRU headroom 33 TB             │
│  ─ Games: 2617 total · 2533 cached · 0 validation-failed · 12 blocked   │
├────────────────────────────────────────────────────────────────────────┤
│  [Recent Errors Panel] (last 5)                                         │
│  ─ 2026-04-20 14:10:07  [ERROR]  prefill epic/fortnite_beta  manifest   │
│                                   fetch 403 (retried; gave up)          │
└────────────────────────────────────────────────────────────────────────┘
```

All labels are text. No icons-only, no color-only. Every status indicator has the form `[TEXT_LABEL] optional_icon optional_color`. Screen-reader order: top to bottom, left to right.

### 3.2 Bearer token prompt

On first page load, JS checks `sessionStorage['orchestrator_token']`. If missing:

```
┌─────────────────────────────────────┐
│  Enter orchestrator bearer token:   │
│                                     │
│  [________________________]         │
│                                     │
│  Token is in the Docker secret      │
│  on the DXP4800 host. Retrieve with:│
│  cat secrets/orchestrator_token.txt │
│                                     │
│  [Submit]                           │
└─────────────────────────────────────┘
```

(Browser `prompt()` per F10 MVP design — OQ5; the multi-line helper is rendered as the prompt's default argument, truncated in browsers that don't support long prompts. FG1 tracks the Post-MVP replacement with a minimal HTML login form.)

On 401, clear `sessionStorage` and re-prompt.

### 3.3 Component states per panel

Every panel has four states. Colorblind-safe throughout — every state has a unique text label in addition to any color.

#### Health Panel

| State | What renders |
|---|---|
| Empty | N/A (health endpoint always returns at least `{status: "ok"}`) |
| Loading | `API: [CHECKING…]` with spinner (first load) |
| Error | `API: [UNREACHABLE]` — single line, retry button. Panel also appears if `/api/v1/health` returns 503 → surface which sub-flag is false. |
| Success | `API: [OK] Scheduler: [OK] Lancache: [OK] Cache Volume: [OK] Validator: [OK]` — each as a distinct text-labeled indicator |

Degraded states: if any sub-flag is false, show `Scheduler: [STOPPED — restart required]` etc. in prominent amber with a ⚠ icon **and** the [STOPPED] text label (colorblind-safe).

#### Platforms Panel

| State | What renders |
|---|---|
| Empty | `No platforms configured.` — should never happen (migration seeds both rows) but the empty-state is defined defensively |
| Loading | `Checking platforms…` |
| Error | Per-row, if fetch fails: `Steam: [UNKNOWN — API unreachable]` |
| Success | Per-row: `Steam: [OK] last sync 2h ago · next sync in 4h` OR `Epic: [EXPIRED] Run orchestrator-cli auth epic on DXP4800` (copyable). Expired state is distinguished by text label `[EXPIRED]` plus amber color plus ⚠ icon. |

#### Active Jobs Panel

| State | What renders |
|---|---|
| Empty | `No active jobs. Next scheduled sync in 3h 46m.` |
| Loading | `Loading jobs…` (first load) |
| Error | `Unable to fetch jobs.` + retry button |
| Success | List of jobs with `[RUNNING 73%]`, `[QUEUED]`, `[SUCCEEDED]`, `[FAILED]` text labels. Progress bars use text percentage + ASCII bar (not color-only). |

#### Stats Panel

| State | What renders |
|---|---|
| Empty | `No data yet — first sync has not completed.` |
| Loading | `…` |
| Error | `Stats unavailable: cache volume not mounted` (matches `/api/v1/stats` 503 path) |
| Success | 2 lines of aggregate counters as per §2.4 |

#### Recent Errors Panel

| State | What renders |
|---|---|
| Empty | `No errors in the last 24 hours.` |
| Loading | `…` |
| Error | `Unable to fetch errors.` |
| Success | Up to 5 recent errors with timestamp + `[ERROR]` label + kind + short error text. Click-to-expand for full error body. |

### 3.4 Polling strategy

- `/api/v1/health` every 2 s.
- `/api/v1/platforms` + `/api/v1/jobs?state=queued&state=running` every 2 s.
- `/api/v1/stats` every 10 s (less volatile).
- `/api/v1/jobs?state=failed&limit=5` every 10 s.

On 401: clear token + re-prompt. On 5xx: display error state; back off polling to 10 s until success.

---

## 4. Game_shelf Integration Contract (F14–F17, Game_shelf repo)

The orchestrator commits to a stable contract. Implementation lives in `kraulerson/Game_shelf`. Full component specs there.

**Orchestrator's commitments:**
1. `/api/v1/*` endpoints in §2 are stable within the v1 major version.
2. `/api/v1/health` always returns a body with `version` and `status` fields so Game_shelf can detect degraded state and version skew (F17).
3. Mutation-response envelope `{ok, job_id, message}` is stable (DQ6).
4. CORS allowlist configurable via `CORS_ORIGINS` env var; default deny.
5. Bearer token rotation procedure documented in HANDOFF.md; rotation does not require any schema change on Game_shelf's side.
6. Breaking changes require `/api/v2/` prefix; `/api/v1/` remains for at least one minor version after v2 introduction.

**What Game_shelf commits to:**
1. Bearer token stays in Express backend env (`ORCHESTRATOR_TOKEN`); NEVER reaches frontend bundle (F17 CI grep).
2. Frontend calls go to `/api/cache/*` on the Game_shelf origin; backend proxies with injected Authorization header.
3. On orchestrator 503 / timeout / connection refused → Game_shelf backend returns 503 with `{"status": "orchestrator_offline"}`; frontend shows dismissible banner, renders `—` badges, disables mutations.
4. Tolerant merging of response shapes — extra fields ignored, missing fields rendered as `—`.

Game_shelf's own four-state specs for `CacheBadge.jsx`, `CachePanel.jsx`, and `pages/Cache.jsx` are deliverables of the Game_shelf PR in Build Milestone D.

---

## 5. Accessibility Baseline (Intake §9 — colorblind-safe hard constraint)

**Global rule:** No status indicator, anywhere, relies on color alone.

- **CLI output:** text prefixes (`[OK]`, `[WARN]`, `[ERROR]`, `[INFO]`) are the primary signal; color is additive.
- **REST API:** N/A (machine-consumed; no color).
- **Status page:** every state pairs color with an icon + a text label. Verified by:
  - Grayscale screenshot review during Phase 3.4 accessibility audit.
  - Browser devtools "Emulate vision deficiencies" set to Deuteranopia → all states distinguishable.
  - Unit tests on the HTML template assert that every `.status-indicator` element contains both a text node matching an allowed label and an aria-label attribute.
- **Game_shelf integration (F15/F16):** same rule; `CacheBadge.jsx` states include text label + icon + color (Phase 2 Milestone D).

**Keyboard and screen-reader:** status page is a simple single-page document; tab order is natural document order; every interactive element (Retry button, "expand error" clicks) has a text label; no custom focus management.

**WCAG AA is not targeted** for the orchestrator's own UI (Light track, Intake §9 "minimal"). Colorblind-safety is the single hard constraint and is verified above.

---

## 6. Review Checklist (per Builder's Guide §1.5)

- [x] Layout defined for each core interface surface — ✅ CLI command tree §1.3, REST endpoints §2.4, status page §3.1
- [x] Component responsibilities are clear — ✅ CLI subcommand purposes, endpoint purposes, panel purposes tabled
- [x] All interactive elements have text labels — ✅ CLI text prefixes, status-page `[TEXT_LABEL]` on every indicator
- [x] All four states defined (Empty / Loading / Error / Success) for every interactive component — ✅ §1.4 (CLI interactive), §2.4 (each endpoint), §3.3 (each panel)
- [x] Output format is text-based component specifications — ✅ no mockups; ASCII and tables throughout
- [x] CLI/API/background-service mode applied — ✅ no UI scaffolding, interface-spec instead
- [x] Accessibility baseline — ✅ Intake §9 colorblind-safe invariant documented and verification strategy specified

---

## 7. Sign-off

**Orchestrator review required.**

**Next Phase 1 step:** 1.6 — Project Bible synthesis (the final Phase 1 artifact — governs Phase 2 onward).
