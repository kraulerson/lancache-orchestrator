# F1 — Steam credentials + minimal manifest fetcher (design)

**Date:** 2026-05-24
**Scope:** End-to-end Steam data ingestion: subprocess-isolated steam-next worker, two-step auth (with 2FA), library enumeration, on-demand manifest fetching with zstd-compressed BLOB persistence.
**Status:** Design approved 2026-05-24 (brainstorming session).
**Decomposition:** 3 Build Loops — BL10 (auth substrate) → BL11 (library sync) → BL12 (manifest fetcher).

<!-- Last Updated: 2026-05-24 -->

## 1. Goals + Non-Goals

### Goals
- Operationalize Spike A's validated Steam auth + manifest fetch flow into production code.
- Persist Steam sessions across container restarts so re-auth + 2FA isn't required on every boot.
- Populate the `games` table from the operator's owned Steam library.
- Land at least one real manifest in the `manifests` table — demonstrating end-to-end data flow through every layer built in BL1-BL9.
- Establish the **subprocess-isolation pattern** that F2 (Epic) and any future platform integration will reuse.

### Non-Goals
- F5/F6 CDN prefill (downloading chunks through Lancache).
- F12 scheduled sync cycle (manual + auto-on-auth triggers only).
- Multi-platform concurrent workers (single steam worker per orchestrator process).
- Epic platform support (F2 in a separate milestone).
- CLI subcommands (`orchestrator-cli auth steam`) — HTTP API only for now. CLI is post-MVP (F11).

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Subprocess worker** for steam-next | Isolates gevent's global monkey-patch from asyncio. ADR-0013 will codify. |
| D2 | **Newline-delimited JSON over stdin/stdout** for IPC | Simplest reliable bidirectional channel; survives `docker exec` debugging; no socket-path coordination. |
| D3 | **Two-step auth with `challenge_id`** for 2FA | RESTful, async-friendly. In-memory challenge state, 5-min TTL. |
| D4 | **steam-next dir + orchestrator metadata file** for session persistence | Matches steam-next's API (dir via `set_credential_location`) and Bible §7.2's intent (single canonical metadata file). |
| D5 | **Long-lived worker subprocess, lazy reconnect** | steam-next handles refresh internally inside its long-running process; no explicit refresh task needed. |
| D6 | **Jobs-based async manifest fetching** | POST returns 202 + job_id; client polls `GET /api/v1/jobs/{id}`. Decouples HTTP request lifetime from Steam-API latency. |
| D7 | **Auto-trigger library sync on first auth + manual re-sync endpoint** | New operators get a populated `games` table immediately; manual endpoint covers ongoing refresh until F12. |
| D8 | **BL10 → BL11 → BL12 split** (3 BLs, ~11-17 days) | Each BL is independently shippable + testable. UAT-6 fires between BL11 and BL12 (counter at 2/2). |
| D9 | **pickle + zstd for manifest BLOB** | steam-next manifests are protobuf-derived classes; pickle preserves structure; zstd-level-3 keeps wire+DB size reasonable. Deserialization stays inside the worker subprocess. |
| D10 | **Single-worker job loop** (no concurrent dispatch) | Sufficient for F1's minimal scope; concurrent multi-job execution deferred. |
| D11 | **In-memory challenge state with 5-min TTL** | Server restart invalidates in-flight 2FA; acceptable (auth is rare). |
| D12 | **NO tokens in `platforms.config`** — only `{steam_id, username}` | Per Bible §7.2; username is identifier, not credential. |
| D13 | **Dual-venv container layout** (orchestrator venv + steam-worker venv) | Keeps gevent out of the orchestrator process's import graph. Settings key `steam_worker_python_path`. |
| D14 | **NEVER deserialize manifest BLOB in orchestrator process** | Pickle's safety relies on trusted input; we trust our own worker, not anything else. |
| D15 | **Worker IPC contract** is the cross-BL stability surface | All 3 BLs use the same protocol message types. Protocol changes require ADR amendment. |
| D16 | **Mock-worker integration tests + DI-override unit tests** | Live Steam validation only happens during UAT-6 with the operator's real account. |
| D17 | **Steam IP throttling guard** — max 3 worker restart attempts before back-off | Prevents Spike A's "new device login" email storm. |
| D18 | **`platforms.config` JSON shape** locked: `{steam_id: str, username: str, last_refreshed_at: iso8601}` | Stable contract for BL10 onward; additions append-only. |
| D19 | **Worker spawned at FastAPI lifespan startup** when session file exists | Cold-start optimization: don't wait for first operator action to detect a broken session. |
| D20 | **No worker stdout buffering beyond 10 MiB** | Back-pressure guard against malformed JSON storms. Worker is killed + restarted if exceeded. |

## 3. Architecture

### 3.1 Subprocess pattern

```
┌─────────────────────────────────────────────────────────────┐
│ orchestrator process (asyncio, FastAPI)                     │
│                                                             │
│  ┌──────────────────┐    ┌────────────────────────────┐    │
│  │ FastAPI routers  │    │ SteamWorkerClient (async)  │    │
│  │ /platforms/...   │───▶│  - msg_id correlation      │    │
│  │ /games/.../fetch │    │  - asyncio.Future map      │    │
│  └──────────────────┘    └─────────┬──────────────────┘    │
│                                    │ stdin/stdout pipes     │
│  ┌──────────────────┐              │ (newline-delim JSON)   │
│  │ Jobs worker      │              │                        │
│  │ (asyncio task)   │              ▼                        │
│  └──────────────────┘    ┌─────────────────────────────┐   │
│                          │ subprocess.Popen            │   │
└──────────────────────────┴─────────┬───────────────────┘   │
                                     │                       │
┌────────────────────────────────────▼───────────────────────┐
│ steam worker process (gevent, steam-next)                  │
│                                                            │
│  - `from steam import monkey; monkey.patch_minimal()`      │
│    is the FIRST line of worker.py                          │
│  - Long-lived; one process per orchestrator boot           │
│  - Reads JSON request from stdin                           │
│  - Dispatches: auth.begin/complete/status, library.enum,   │
│    manifest.fetch, shutdown                                │
│  - Writes JSON response to stdout                          │
│  - All Steam I/O happens here; orchestrator never imports  │
│    steam, gevent, or any monkey-patched stdlib             │
└────────────────────────────────────────────────────────────┘
```

### 3.2 IPC protocol

**Request envelope:**
```json
{"msg_id": "550e8400-e29b-41d4-a716-446655440000", "op": "auth.begin",
 "params": {"username": "...", "password": "..."}}
```

**Response envelope (success):**
```json
{"msg_id": "550e8400-...", "ok": true,
 "result": {"challenge_id": "...", "challenge_type": "mobile_authenticator"}}
```

**Response envelope (error):**
```json
{"msg_id": "550e8400-...", "ok": false,
 "error": {"kind": "TwoFactorCodeMismatch", "message": "code did not match"}}
```

**Notes:**
- `msg_id` is a UUID; collisions rejected by the client side.
- Request `op` namespacing: `auth.*`, `library.*`, `manifest.*`, `shutdown`.
- Worker writes one JSON object per stdout line; each line is terminated by `\n`.
- 10 MiB cap on any single line (back-pressure guard, D20).
- Per-request timeout via `Settings.steam_worker_ipc_timeout_sec` (default 30s); on timeout, client raises `IPCTimeoutError` and the orchestrator-side jobs handler marks the job failed.

### 3.3 Operations catalog (locked surface)

| op | params | result on success | result on failure |
|---|---|---|---|
| `auth.begin` | `{username, password}` | `{authenticated: true, steam_id, licenses_count}` (no-2FA path) OR `{authenticated: false, challenge_id, challenge_type}` | `{kind: 'InvalidCredentials' | 'AccountLocked' | 'RateLimited' | ...}` |
| `auth.complete` | `{challenge_id, code}` | `{authenticated: true, steam_id, licenses_count}` | `{kind: 'TwoFactorCodeMismatch' | 'ChallengeExpired' | ...}` |
| `auth.status` | `{}` | `{authenticated: bool, steam_id?: int, last_check_at: iso8601}` | n/a (always returns) |
| `library.enumerate` | `{}` | `{apps: [{app_id: int, name: str, depots: [int, ...]}, ...]}` | `{kind: 'NotAuthenticated' | 'SteamAPIError'}` |
| `manifest.fetch` | `{app_id: int, depot_id?: int}` | `{depot_id, manifest_gid, version: str, chunk_count, total_bytes, raw_b64: str}` | `{kind: 'AppNotOwned' | 'DepotNotFound' | 'SteamAPIError'}` |
| `shutdown` | `{}` | `{ok: true}` | n/a |

### 3.4 Worker lifecycle

- **Spawn:** at FastAPI lifespan startup if `steam_session.json` exists (cold-start health probe); otherwise on first auth attempt.
- **Liveness probe:** `auth.status` IPC polled every 60s from a background task. This is a **process-liveness check** (does the worker respond to IPC?), NOT a Steam-session-validity check. If 3 consecutive polls *time out or raise IPC errors* → mark `platforms.auth_status='error'`, kill worker, attempt one respawn (subject to restart-storm guard).
- **Steam session expiry detection** is intentionally **lazy** (D5): the worker reports its last-known auth state on `auth.status`, but it doesn't ping Steam. A truly-expired Steam session only surfaces during a real operation (`library.enumerate`, `manifest.fetch`) — at which point the handler catches `NotAuthenticated` and marks the job + platform row accordingly.
- **Restart-storm guard:** `Settings.steam_worker_max_restart_attempts = 3` per orchestrator-process lifetime; after 3 deaths the worker is NOT auto-respawned. Operator must restart the orchestrator (or trigger explicit reset via an admin endpoint — deferred to F18 territory).
- **Shutdown:** at FastAPI lifespan shutdown, send `shutdown` IPC + wait 5s for clean exit + SIGTERM + wait 5s + SIGKILL.

## 4. BL10 — Steam auth substrate

**Scope:** subprocess scaffolding + IPC contract + auth two-step endpoint + session persistence + platforms-table integration. NO library or manifest functionality.

### 4.1 New files

| Path | Purpose | ~LoC |
|---|---|---|
| `src/orchestrator/platform/__init__.py` | new package | 5 |
| `src/orchestrator/platform/steam/__init__.py` | new sub-package | 5 |
| `src/orchestrator/platform/steam/worker.py` | the subprocess; gevent-patched; speaks JSON-IPC | 200 |
| `src/orchestrator/platform/steam/client.py` | asyncio-side `SteamWorkerClient` (lifecycle + IPC) | 250 |
| `src/orchestrator/platform/steam/protocol.py` | typed message dataclasses (shared by both sides) | 80 |
| `src/orchestrator/api/routers/auth.py` | the 3 auth endpoints | 150 |

### 4.2 Modified files

- `src/orchestrator/api/main.py` — lifespan starts/stops the worker; wires the new router
- `src/orchestrator/core/settings.py` — add 5 new keys (see §7.2)
- `requirements-steam-worker.in` (new) — `steam[client]`, `gevent`, `zstandard`, `httpx`

### 4.3 Endpoints

- `POST /api/v1/platforms/steam/auth` — loopback-only + bearer-required (UAT-3 enforcement already in place). Body: `{username: str, password: str}`. Response: `200 + {status: 'authenticated', steam_id}` OR `202 + {challenge_id, challenge_type, expires_at}`.
- `POST /api/v1/platforms/steam/auth/{challenge_id}` — loopback-only + bearer. Body: `{code: str}`. Response: `200 + {status: 'authenticated', steam_id}` OR `401` (bad code, expired challenge).
- `GET /api/v1/platforms/steam/auth/status` — bearer (NOT loopback-only; status check is fine from Game_shelf). Response: `200 + {authenticated, steam_id?, session_expires_at?, last_check_at}`.

### 4.4 Auth flow

1. `POST /auth {username, password}` → server calls `client.auth_begin(...)` → IPC `auth.begin` to worker.
2. Worker calls `steam-next.login()`. Branches:
   - `EResult.OK` → returns `{authenticated: true, steam_id, licenses_count}`. Server happy path:
     - UPDATE `platforms` row: `auth_status='ok'`, `last_sync_at=now`, `last_error=NULL`, `config={steam_id, username, last_refreshed_at=now}`.
     - Returns `200 + {status, steam_id}`.
   - `EResult.AccountLoginDeniedNeedTwoFactor` or `AccountLogonDenied` → returns `{authenticated: false, challenge_type}`. Server:
     - Generate `challenge_id = uuid4()`.
     - Store `_challenge_states[challenge_id] = (username, password_hash_for_idempotency_check_only, expires_at)` in-memory.
     - Returns `202 + {challenge_id, challenge_type, expires_at}`.
3. `POST /auth/{challenge_id} {code}` → server validates `challenge_id` exists + not expired → calls `client.auth_complete(challenge_id, code)`.
4. Worker calls `steam-next.login(..., two_factor_code|auth_code=code)`. On success: server clears challenge, happy-path updates per step 2. On failure: clears challenge, returns 401.

### 4.5 Database integration

- `platforms` row for `name='steam'`:
  - `auth_status` transitions: `never → ok` (success) or `never → error` (failure) or `ok → expired` (lapsed session) or `ok → error` (refresh failed).
  - `auth_expires_at` set from Steam's response (steam-next exposes session expiry).
  - `last_sync_at` updated on auth-success.
  - `last_error` populated with terse error class on failure (truncated to 200 chars per existing pattern); cleared on success.
  - `config` stores `{steam_id, username, last_refreshed_at}` as JSON. **NO tokens, ever** (D12).

### 4.6 Session persistence

- Directory: `Settings.steam_session_dir = /var/lib/orchestrator/steam_session/` (mode 0700) — managed by steam-next via `set_credential_location`.
- File: `Settings.steam_session_path = /var/lib/orchestrator/steam_session.json` (mode 0600) — orchestrator-owned metadata: `{steam_id, username, last_refreshed_at, sha256_prefix, auth_method_version}`. Written atomically (`os.replace` from a tempfile in the same dir).

### 4.7 Tests (~30)

- **IPC plumbing**: request/response correlation, msg_id collision rejection, oversized response (>10 MiB) → worker killed + client raises, subprocess crash recovery.
- **Auth happy path**: no-2FA login → 200; with-2FA → 202 + challenge_id → second POST → 200.
- **Auth failure**: bad password → 401, bad 2FA code → 401, expired challenge → 404, replay attack on consumed challenge → 404.
- **Security**: bearer required (existing middleware regression), loopback-only (existing middleware regression), 0 raw tokens in logs (capsys + gitleaks), `platforms.config` JSON has no token field, no creds in env on worker spawn (assertion via inspect of `subprocess.Popen` kwargs).
- **Lifecycle**: worker spawned on lifespan startup IF session file exists, terminated cleanly on shutdown, auto-restart on crash with structured `steam_worker.restarted` event, restart-storm guard fires at 3 deaths.

### 4.8 Days: 5-7

## 5. BL11 — Library sync

**Scope:** library enumeration job + games-table populate/update + manual sync endpoint + auto-trigger on first auth.

### 5.1 New files

| Path | Purpose | ~LoC |
|---|---|---|
| `src/orchestrator/jobs/__init__.py` | new package | 5 |
| `src/orchestrator/jobs/worker.py` | generic asyncio job dispatcher | 120 |
| `src/orchestrator/jobs/handlers/__init__.py` | handler registry | 30 |
| `src/orchestrator/jobs/handlers/library_sync.py` | Steam library sync handler | 150 |
| `src/orchestrator/api/routers/sync.py` | manual sync endpoint | 80 |

### 5.2 Modified files

- `src/orchestrator/api/main.py` — lifespan also spawns the jobs worker asyncio task.
- `src/orchestrator/api/routers/auth.py` (BL10) — on successful auth, insert a `library_sync` job (auto-trigger).
- `src/orchestrator/platform/steam/worker.py` (BL10) — implement `library.enumerate` handler.

### 5.3 Jobs worker design

Atomic-claim pattern using SQLite's `UPDATE ... RETURNING`:

```python
async def claim_next_job(pool):
    return await pool.read_one("""
        UPDATE jobs SET state='running', started_at=CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM jobs WHERE state='queued'
            ORDER BY id LIMIT 1
        )
        RETURNING id, kind, game_id, platform, payload
    """)

async def worker_loop(pool, deps):
    while not _shutdown.is_set():
        row = await claim_next_job(pool)
        if not row:
            await asyncio.sleep(Settings.jobs_worker_poll_interval_sec)
            continue
        handler = HANDLERS.get(row["kind"])
        if not handler:
            await mark_failed(pool, row["id"], "no handler for kind")
            continue
        try:
            await handler(row, deps)
            await mark_succeeded(pool, row["id"])
        except Exception as e:
            await mark_failed(pool, row["id"], type(e).__name__ + ": " + str(e)[:180])
```

Single worker loop; 1-second poll on empty queue; concurrent multi-job execution deferred (D10).

### 5.4 Library upsert semantics

```sql
INSERT INTO games (platform, app_id, title, owned, metadata)
VALUES (?, ?, ?, 1, ?)
ON CONFLICT(platform, app_id) DO UPDATE SET
  title = excluded.title,
  owned = 1,
  metadata = excluded.metadata
```

- `metadata` JSON: `{"depots": [...], "steam_packages": [...]}`.
- `size_bytes` left NULL — BL12 fills it when a manifest is fetched.
- `status` defaults to `not_downloaded` for newly-added rows; pre-existing rows keep their status.

### 5.5 Auto-trigger

In BL10's auth-success path:
```python
await pool.execute_write(
    """INSERT INTO jobs (kind, platform, state, source)
       VALUES (?, ?, 'queued', 'api')""",
    ("library_sync", "steam"),
)
```

### 5.6 Manual endpoint

- `POST /api/v1/platforms/steam/library/sync` — bearer required, NOT loopback-only.
- Dedup: if a `library_sync` job for `steam` is already `queued|running`, return its `job_id` instead of creating a duplicate. Uses the existing `idx_jobs_dedupe` partial index.
- Response: `202 + {job_id}`.

### 5.7 Tests (~25)

- Jobs worker: happy path (queued → running → succeeded), unknown `kind` → failed, handler-crash isolation (one bad handler doesn't kill the loop).
- Library sync: N-app library populates N games rows; second sync upserts no duplicates.
- Dedup: concurrent POSTs return same job_id.
- Auth integration: successful auth queues a `library_sync` job.
- Empty library: 0 apps → 0 games inserted, job state=succeeded.
- Steam worker offline: handler fails with structured error, job state=failed, no partial games-table writes.

### 5.8 Days: 3-5

**UAT-6 fires after BL11 ships** (counter at 2/2). Manual session validates BL10 + BL11 end-to-end before BL12.

## 6. BL12 — Manifest fetcher

**Scope:** manifest fetch job + manifests-table write (zstd-compressed raw BLOB) + per-game endpoint. Final BL of F1.

### 6.1 New files

| Path | Purpose | ~LoC |
|---|---|---|
| `src/orchestrator/jobs/handlers/manifest_fetch.py` | manifest-fetch handler | 180 |
| `src/orchestrator/api/routers/manifest_trigger.py` | per-game trigger endpoint | 80 |

### 6.2 Modified files

- `src/orchestrator/jobs/worker.py` — register `manifest_fetch` handler.
- `src/orchestrator/platform/steam/worker.py` — implement `manifest.fetch(app_id, depot_id?)`.
- `requirements-steam-worker.in` — `zstandard` already listed (BL10); confirm.
- `requirements.in` — add `zstandard` (orchestrator side; for symmetric round-trip tests + future inspection).

### 6.3 Trigger flow

1. `POST /api/v1/games/{game_id}/manifest/fetch` — bearer required. Body: `{depot_id?: int}` (optional).
2. Server validates: `games.id` exists; `games.platform == 'steam'`; dedupe via `idx_jobs_dedupe`.
3. Insert `jobs` row: `kind='manifest_fetch'`, `game_id`, `payload={depot_id?}`.
4. Return `202 + {job_id}`.
5. Worker picks job, calls `client.manifest_fetch(app_id, depot_id)`, gets `{depot_id, manifest_gid, raw_b64, total_bytes, chunk_count}`.
6. Handler UPSERTs into `manifests` (UNIQUE on `game_id+version` → idempotent re-fetch).
7. Handler also updates `games.size_bytes = manifest.total_bytes`.

### 6.4 Serialization

Inside the worker:
```python
pickled = pickle.dumps(manifest_object, protocol=5)
compressed = zstandard.ZstdCompressor(level=3).compress(pickled)
raw_b64 = base64.b64encode(compressed).decode("ascii")
```

- Pickle safety: the manifest is generated by our own worker from steam-next data we just received over TLS; no untrusted input. Orchestrator process NEVER unpickles (D14).
- zstd level 3: good ratio/speed tradeoff for protobuf-derived data.
- Base64 for JSON-IPC transport; binary stays binary in the DB BLOB.

### 6.5 SQL

```sql
INSERT INTO manifests (game_id, version, fetched_at, chunk_count, total_bytes, raw)
VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
ON CONFLICT(game_id, version) DO UPDATE SET
  fetched_at = CURRENT_TIMESTAMP,
  chunk_count = excluded.chunk_count,
  total_bytes = excluded.total_bytes,
  raw = excluded.raw
```

### 6.6 End-to-end verification (post-BL12)

1. `POST /api/v1/platforms/steam/auth` → 2FA flow → authenticated.
2. Auto `library_sync` job populates `games` (e.g., 50 owned Steam apps).
3. `POST /api/v1/games/{counter_strike_id}/manifest/fetch` → manifest fetched, stored.
4. `GET /api/v1/manifests?game_id={counter_strike_id}&include=game` → row visible via the BL9 read endpoint.
5. **First real data has flowed through every layer (BL1-BL12).**

### 6.7 Tests (~30)

- Happy path: queue job → worker fetches → manifest in `manifests` table → `GET /api/v1/manifests` returns it.
- Dedup: concurrent triggers for same game return same job_id.
- 404: game not found.
- 400: game on wrong platform (`epic`).
- Default depot selection from `games.metadata.depots[0]`.
- Explicit `depot_id` in body honored.
- BLOB round-trip: zstd-decompress + unpickle the stored `raw` → matches what steam-next returned.
- `games.size_bytes` updated on success.
- Steam worker offline: job fails cleanly, manifest NOT written.
- Re-fetch (same version) is idempotent (UPSERT updates `fetched_at`).
- Large manifest (synthetic 50 MB compressed BLOB) — verify pool's BLOB write path respects `manifest_size_cap_bytes` (128 MB default).

### 6.8 Days: 3-5

## 7. Cross-cutting concerns

### 7.1 Security

- All credentials confined to the worker subprocess. Orchestrator process never sees raw username/password/refresh-tokens.
- Only the 8-char `sha256_prefix` of session tokens reaches orchestrator logs (consistent with BL5 auth-rejection pattern).
- IPC channel is process-private pipes (no network exposure).
- Worker spawned with `start_new_session=True`; env filtered to remove non-essential vars on spawn.
- Auth challenge state in-memory only with 5-min TTL; server restart invalidates all in-flight challenges.
- `platforms.config` JSON has `{steam_id, username, last_refreshed_at}` ONLY. NEVER contains tokens. Username persists as identifier-not-credential.

### 7.2 Settings additions (total across BL10-BL12)

| Key | Default | Purpose |
|---|---|---|
| `steam_worker_python_path` | `/opt/orchestrator/venv-steam-worker/bin/python` | Worker venv python |
| `steam_worker_ipc_timeout_sec` | `30` | Per-IPC-request timeout |
| `steam_worker_max_restart_attempts` | `3` | Restart-storm guard |
| `steam_session_dir` | `/var/lib/orchestrator/steam_session` | steam-next-managed credential dir |
| `jobs_worker_poll_interval_sec` | `1.0` | Empty-queue poll cadence |

(`steam_session_path` already exists from earlier Settings work.)

### 7.3 Testing strategy

- **Unit tests** use a **stub `SteamWorkerClient`** (asyncio-only, in-process, no subprocess) registered via DI override — same pattern as the existing `unit_app` / pool override pattern in `tests/api/conftest.py`. Validates the orchestrator-side contract without spinning up real steam-next.
- **One integration test per BL** boots a real subprocess running a **mock worker** (`tests/integration/mock_steam_worker.py`) — a Python script that speaks the same JSON-IPC protocol but returns canned responses. Validates the IPC plumbing without hitting Steam.
- **Live Steam-side validation** deferred to **UAT-6** — the assistant cannot run interactive Steam auth, so the operator runs through the auth flow with their real Steam account during UAT.

### 7.4 Risks + mitigations

| Risk | Mitigation |
|---|---|
| Steam IP throttling from reconnect storms | Long-lived worker (one process per boot) + max 3 restart attempts; documented in HANDOFF.md |
| steam-next version drift | Pin exact version in `requirements-steam-worker.txt`; re-validate Spike A's manifest_gid monkey-patch at BL10 start |
| Worker crash mid-job | Handler sees `IPCTimeout` or `WorkerDiedError` → job state=failed + structured error + `platforms.auth_status='error'`; next operator action respawns |
| Pickle in BLOB | Only deserialized inside our own worker subprocess (D14); worker doesn't accept BLOBs over IPC |
| Container layout (dual venv) | BL10 docs the layout; Dockerfile ships in Phase 4 |
| `challenge_id` in-memory loss on restart | Acceptable; auth is rare; operator restarts the flow |

### 7.5 Telemetry events (added to the existing structlog catalog)

- `steam_worker.spawned` / `steam_worker.died` / `steam_worker.restarted` / `steam_worker.restart_storm_guard_fired`
- `steam_worker.ipc_request` / `steam_worker.ipc_response` (DEBUG) / `steam_worker.ipc_timeout` (WARN)
- `platform.auth.began` / `platform.auth.completed` / `platform.auth.failed`
- `platform.session.refreshed` / `platform.session.expired`
- `jobs.worker.started` / `jobs.worker.claimed_job` / `jobs.handler.started` / `jobs.handler.completed` / `jobs.handler.failed`

### 7.6 Documentation deltas

- **Per-BL** (BL10, BL11, BL12): spec, plan, security audit, CHANGELOG entry, FEATURES.md entry.
- **ADR-0013** (new, lands with BL10): "Steam-next subprocess isolation pattern". Locks in the architecture for F2 (Epic) and any future platform integration to reuse.

## 8. Out of scope (explicit deferrals)

- F2 — Epic OAuth (separate milestone; will reuse the subprocess pattern).
- F5/F6 — CDN prefill (downloading chunks through Lancache).
- F12 — Scheduled sync cycle (the auto-on-auth + manual endpoints suffice until F12 lands).
- Concurrent multi-job dispatch (single worker loop suffices for F1).
- Per-endpoint rate limiting (not on the threat model for F1).
- Admin endpoint to reset worker after restart-storm-guard fires (operator-restart of orchestrator suffices; revisit in F18 if a friction point).
- CLI subcommands (`orchestrator-cli auth steam` — F11 is post-MVP).
