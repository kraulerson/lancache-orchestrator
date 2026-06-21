# Sub-project ② — Data-Plane Agent — Design

**Date:** 2026-06-20
**Status:** Approved (design)
**Repo:** lancache_orchestrator. **Branch:** `feat/data-plane-agent`
**Parent:** the re-architecture north-star (`docs/superpowers/specs/2026-06-19-re-architecture-design.md`, PR #174 merged). This is roadmap **step ②**.
**Predecessor:** step ① (`docs/superpowers/specs/2026-06-19-steam-via-prefill-design.md`, PR #175) — the `SteamPrefillDriver` this agent relocates.

> Scope: extract the orchestrator's data plane (chunk-puller + cache disk-stat + Steam-tool runner) into a thin, bearer-authed, source-allowlisted **HTTP agent** that stays on the UGREEN lancache VM. The control plane calls it over loopback instead of doing the work in-process. **No host move** (that's step ④); the agent link stays loopback. Ships: a clean, testable control/data boundary proven on one host — with **no externally-visible behavior change**.

---

## 1. Architecture & boundary

The agent is a **thin HTTP wrapper over code that already exists.** To honor "no behavior change," the puller (`prefill/downloader.py`, `prefill/epic_downloader.py`), the disk-stat (`validator/disk_stat.py`), and the `SteamPrefillDriver` are **not rewritten** — they stay as shared library functions. The new agent app imports and exposes them over HTTP; the control plane stops calling them in-process and calls an **`AgentClient`** instead. Only the caller changes.

```
lancache VM (on UGREEN NAS; lancache is the VM itself, not a container)
 ├─ orchestrator container  ── CONTROL PLANE (brain)
 │    • API / DB / scheduler / Game_shelf integration  (unchanged, stays at 192.168.1.40:8765)
 │    • job worker: prefill/validate handlers call AgentClient (HTTP), not in-process
 │    • cache-KEY compute stays here (validator/cache_key.py — pure, no FS)
 │    • manifest_expand via the kept steam worker (still co-located in ②)
 │         │  loopback HTTP  (bearer + source-allowlist)
 │         ▼
 └─ agent container  ── DATA PLANE (thin)
      • POST /v1/pull          → stream-and-discard chunks through lancache loopback
      • POST /v1/stat          → disk-stat cache hashes (owns cache_root + levels)
      • POST /v1/steam/prefill → SteamPrefillDriver.prefill_apps (relocates here)
      • mounts: cache:ro, /SteamPrefill + Config + ~/.cache, lancache loopback
```

**Responsibility split:**
- **Control plane decides & computes** (metadata, KB-scale): what to prefill/validate, builds the `{url, host}` chunk list (Steam: `/depot/{depot}/chunk/{sha}`; Epic: `{cdn_base_path}/{chunk_path}`), expands manifests (kept worker), computes the cache-key md5 hashes.
- **Agent executes & touches bytes/FS**: pulls chunks through lancache, stats the cache, runs the host SteamPrefill binary. Owns its filesystem layout (`cache_root`, `levels`) and the lancache loopback.

The agent is **dumb and effectively stateless** (only ephemeral in-flight job state) — it never decides *what* to do, only executes a fully-specified instruction. That is what de-risks the ④ LXC move: the boundary is already a real authed HTTP hop, exercised in production on one host first.

## 2. The three endpoint contracts

All routes are under bearer + source-allowlist. Two job types are long-running (**async job + poll**); `/v1/stat` is fast (**synchronous**). The agent keeps an **ephemeral in-memory job registry** for the async ones — durability lives in the orchestrator's DB job that drives them, so an agent restart just fails the in-flight call (the orchestrator job retries), no persistence needed.

### 2.1 `POST /v1/pull` — platform-agnostic chunk puller (async)

Control pre-builds every `{url, host}` pair, so the agent never distinguishes Steam from Epic.

```jsonc
// request
{
  "chunks": [ {"url": "/depot/2347771/chunk/<sha40>", "host": "lancache.steamcontent.com"},
              {"url": "/Builds/.../<chunk>",          "host": "epicgames-download1.akamaized.net"} ],
  "user_agent": "Valve/Steam HTTP Client 1.0",   // per-batch (Steam UA vs Epic UA)
  "concurrency": 32                               // optional; agent default from settings
}
// 202 → {"job_id": "..."}
// GET /v1/pull/{job_id} → {"state":"running","done":840,"total":9000}
//                       → {"state":"done","chunks_total":9000,"chunks_ok":8990,
//                          "chunks_failed":10,"failures":[["<url>","http 404"], ...]}  // capped 50
```

The agent owns `lancache_base_url` (loopback), retry/backoff, the concurrency semaphore. The response is exactly today's `PrefillResult` / `EpicPrefillResult` shape — the existing `prefill_chunks` functions run unchanged inside the agent. **SSRF guard:** `url` must be a relative path (no scheme, `//`, userinfo, or `..`); it is joined to the agent's fixed `lancache_base_url`; `host` is only a routing header.

### 2.2 `POST /v1/stat` — cache disk-stat (synchronous)

Control computes the md5 cache-key hashes (pure `cache_key.py`); the agent localizes each to a path with **its own** `cache_root` + `levels` and stats it. Per-game ≈ thousands of hashes, sub-second — no job needed.

```jsonc
// request
{ "hashes": ["3f2a...e9", ...] }      // each 32 lowercase hex
// 200 → {"cached": 39780, "missing": 5635, "errors": 0}
```

Runs the existing `validate_chunks` over the bounded 2-worker stat pool. **Guard:** each hash validated as `^[0-9a-f]{32}$` at the boundary (defense-in-depth over the path-containment backstop already in `cache_path`). The agent owns `cache_root`/`levels` because it owns the filesystem — control never sends absolute paths.

### 2.3 `POST /v1/steam/prefill` — Steam-tool runner (async) + status reads

The `SteamPrefillDriver` relocates into the agent (it is the host-binary toucher — this also retires ①'s "orchestrator container must mount `/SteamPrefill`" note; the **agent** holds that mount now).

```jsonc
// POST /v1/steam/prefill   {"app_ids":[440,570], "force":false}
//   202 → {"job_id":"..."}
//   GET /v1/steam/prefill/{job_id} → {"state":"running"} → {"state":"done","ok":true,"raw":"...tail..."}
// GET  /v1/steam/downloaded-state  → {"440":[<gid>,...], ...}   // successfullyDownloadedDepots.json
// GET  /v1/steam/auth-status       → {"ok":true,"reason":""}    // account.config presence
```

`raw` is the capped (4000-char) SteamPrefill stdout tail, already scrubbed of token bytes by the driver. The orchestrator's prefill handler (wired in ①) swaps its direct `prefill_driver.prefill_apps(...)` for `agent_client.steam_prefill(...)`; `/health.steam_auth_ok` reads `agent_client.steam_auth_status()`.

### 2.4 Control-side `AgentClient`

Wraps all of the above: `pull()` / `steam_prefill()` POST-then-poll to completion (so callers keep their simple `await` shape); `stat()`, `downloaded_state()`, `auth_status()` are single calls. Injected into `JobsDeps` (replacing `prefill_driver`) and used by the validator orchestration. Agent-unreachable / 401 surfaces a **typed error** the handlers catch (§4/§5), never a raw crash.

## 3. Code structure (what moves, what stays, what's new)

Guiding rule from §1: **wrap existing functions over HTTP — minimal code movement, maximal reuse.** The puller/disk-stat/driver logic is battle-tested; physically relocating it would be churn that risks the "no behavior change" guarantee. Those modules **stay where they are** and the agent app *imports* them.

```
src/orchestrator/
├── agent/                         ← NEW: the data-plane HTTP service (own uvicorn app)
│   ├── app.py                     create_agent_app() + lifespan: builds SteamPrefillDriver,
│   │                              mounts BearerAuth + SourceAllowlist (reused middleware)
│   ├── __main__.py                entrypoint: uvicorn agent.app:app  (separate container CMD)
│   ├── jobs.py                    ephemeral in-memory job registry (async pull + steam/prefill)
│   └── routers/
│       ├── pull.py                POST /v1/pull (+ GET /{id}) → prefill.downloader /
│       │                          epic_downloader prefill_chunks  (imported, UNCHANGED)
│       ├── stat.py                POST /v1/stat → validator.disk_stat.validate_chunks (UNCHANGED)
│       └── steam.py               POST /v1/steam/prefill (+GET), GET downloaded-state, auth-status
│                                  → SteamPrefillDriver  (imported, UNCHANGED)
│
├── clients/
│   └── agent_client.py            ← NEW: control-plane HTTP client (AgentClient): pull()/stat()/
│                                  steam_prefill()/downloaded_state()/auth_status(); POST-then-poll
│
├── prefill/downloader.py          ← UNCHANGED (now imported by agent, not by the handler)
├── prefill/epic_downloader.py     ← UNCHANGED (ditto)
├── validator/disk_stat.py         ← SPLIT FATE within one module:
│                                    • validate_chunks + _stat_batch + the stat executor →
│                                      UNCHANGED leaf, imported by the agent's /v1/stat
│                                    • validate_game (the control orchestration that lives here
│                                      today) → MODIFY: after cache_key compute, call
│                                      agent_client.stat(hashes) instead of validate_chunks()
│                                    (both stay in this file; the agent imports only the leaf)
├── validator/cache_key.py         ← UNCHANGED, STAYS control-side (pure compute)
├── platform/steam/prefill_driver.py ← UNCHANGED (imported by agent; control no longer constructs it)
│
├── jobs/worker.py                 ← MODIFY: JobsDeps.prefill_driver → agent_client: AgentClient
├── jobs/handlers/prefill.py       ← MODIFY: Steam path calls agent_client.steam_prefill(); Epic
│                                    path calls agent_client.pull(chunks, ua) instead of in-process
├── api/main.py                    ← MODIFY: lifespan builds AgentClient (from agent_base_url+token),
│                                    injects into JobsDeps; DROP direct SteamPrefillDriver construction
├── api/routers/health.py          ← MODIFY: steam_auth_ok via agent_client; add agent_reachable
└── core/settings.py               ← MODIFY: agent settings (below)
```

**New settings** (both processes read the same `Settings`, env-selected):
- `agent_base_url: str = "http://127.0.0.1:8780"` — control→agent (loopback in ②, LAN in ④).
- `agent_bind_host` / `agent_bind_port` — where the agent serves.
- `agent_enabled: bool = false` — the migration flag (§5).
- Agent reuses `orchestrator_token` for bearer + `allowed_source_ips` for the allowlist (one secret, one allowlist; ④ can split if desired). The agent's lifespan reuses `_enforce_lan_bind_policy` (fail-closed if bound non-loopback without an allowlist).

**Two entrypoints, one codebase, one image:** the existing container runs the orchestrator API; a second container (same image) runs `python -m orchestrator.agent` with host mounts. No second build.

**Deliberately NOT done:** no rewrite of the pull/stat/driver internals; no manifest-parser relocation (the kept steam worker stays control-side for ②); no Epic auth change. The diff is concentrated at the **call seam** (handlers/validator → AgentClient) plus the new agent shell.

## 4. Security

This step adds a new network-reachable surface that executes byte-pulls, filesystem stats, and a **host subprocess** on command. The agent treats every control-plane instruction as untrusted-until-validated, even though the caller is trusted today.

**1. Authn/authz — reuse shipped primitives, no new crypto.**
- `BearerAuthMiddleware` (hmac.compare_digest, oversized-header reject, strict-ASCII) guards every agent route. No auth-exempt path except `/v1/health`.
- `SourceAllowlistMiddleware` restricts callers to the orchestrator's source IP (loopback in ②; the control-plane LXC IP in ④). Reads `scope["client"]` directly — no X-Forwarded-For trust.
- `_enforce_lan_bind_policy` fail-closed boot guard in the agent lifespan: refuses to start if bound non-loopback without an allowlist (`raise SystemExit(1)`), same as the API.

**2. Input validation at the agent boundary (defense-in-depth).**
- `/v1/pull`: each `url` must be a **relative path** — reject any scheme, `//`, userinfo, or `..` segment. It is joined to the agent's fixed `lancache_base_url`; `host` is a header value only (validated as a hostname, never used to form the connection target). This is the **anti-SSRF core**: a compromised/buggy control plane cannot make the agent fetch arbitrary internet hosts — every request goes to lancache loopback, full stop.
- `/v1/stat`: each hash must match `^[0-9a-f]{32}$`. The agent builds the path from its **own** `cache_root` + `levels`; control never supplies a path. `cache_path`'s existing path-containment backstop remains a second layer.
- `/v1/steam/prefill`: `app_ids` a list of non-negative ints (re-validated even though the driver coerces); `force` strict bool. The driver already writes only `[int,...]` to `selectedAppsToPrefill.json`.

**3. Secrets discipline.**
- The agent holds the **lower** credential surface by design: it reads `account.config` presence for auth-status but **never reads or logs the token bytes** (driver guarantee from ①, carried in unchanged). SteamPrefill stdout is capped + driver-scrubbed; the agent must not log `raw` beyond that tail.
- The bearer token is a `SecretStr`, never logged; rejections log only an 8-char SHA fingerprint (existing pattern).
- The cache mount is **read-only** — the agent can stat but never mutate cache files. The SteamPrefill mounts (`/SteamPrefill`, `Config`, `~/.cache`) are the only writable host paths, and only the driver touches them.

**4. Blast-radius framing.** The agent is the privileged data-plane process (host mounts, subprocess exec); the control plane is the network-facing brain (Game_shelf, API). A Game_shelf-facing API bug can't directly drive the host binary, and an agent bug can't reach the DB/Game_shelf — each side is reachable only through its own authed, allowlisted, input-validated interface. The link is loopback in ②, not on the wire until ④.

**5. New failure mode, handled safely:** if the agent is unreachable/unauthorized, prefill/validate handlers **fail the job cleanly** (record `last_error`, no crash-loop) and `/health` surfaces `agent_reachable:false` — never silently report success (§5).

## 5. Migration (keeping F14–F17 live throughout)

Hard constraint: Game_shelf F14–F17 is **live**, talking to the orchestrator API at `192.168.1.40:8765` via the LAN-bind allowlist. This step must not perturb that — and it doesn't, because **the agent is purely internal**: Game_shelf → control plane → agent. Game_shelf never sees the agent, so `ORCH_API_URL` and the Game_shelf-facing allowlist are untouched (those change only in ④).

**Externally for Game_shelf: nothing changes.** Same address, routes, responses. Prefill/validate produce identical results; only the *internal execution path* moves from in-process to a loopback HTTP hop.

**Rollout sequence (each step independently safe, reversible):**
1. **Land the code behind a flag.** `agent_enabled=false` ⇒ handlers/validator call the in-process functions exactly as today (the imported functions still exist — §3). Merging with the flag off = zero behavior change in the live deploy.
2. **Deploy the agent container** alongside the orchestrator in the lancache VM (same image, `python -m orchestrator.agent`, mounts: cache:ro, `/SteamPrefill`+Config+`~/.cache`, loopback). It is up but nothing routes to it yet. Verify `GET /v1/health` over loopback, bearer-gated.
3. **Flip `agent_enabled=true`** and restart the orchestrator container. Prefill/validate now route through the agent. The orchestrator container **drops its `/SteamPrefill` mount** (the agent owns it).
4. **Live smoke** (operator-collaborative): one Steam prefill + one validate of an already-cached game through the agent; confirm identical cached/missing counts to the pre-flip baseline and a real prefill→cache→validate loop. On any problem, flip the flag back to false and restart — instant rollback to the in-process path, no redeploy.
5. **Remove the flag + the in-process call sites** in a follow-up once proven stable (the imported functions remain — they are now the agent's implementation). Keeps the risky cutover and the cleanup as separate, independently-revertible changes.

**Failure handling during/after cutover:** if `agent_enabled=true` but the agent is unreachable or returns 401, the handler records `last_error` (e.g. `agent_unreachable`), marks the job failed **without** crash-looping the worker, and `/health` reports `agent_reachable:false`. A degraded agent never masquerades as success — same discipline as the existing PoolError→503 convention.

**Game_shelf verification:** after the flip, re-confirm F14–F17 still light up (cache badges, prefill triggers, validate) against the unchanged API — a regression check that the internal re-route is invisible externally.

## 6. Testing

TDD throughout. Because "no behavior change" is the headline guarantee, the strategy is built to **prove equivalence** — the agent path must produce identical results to the in-process path. Tests follow existing pytest conventions; the agent app gets the same httpx-test-client treatment as the API.

**1. Agent endpoint tests** (new, `tests/agent/`):
- `/v1/pull` against a **fake lancache** (reuse the downloader tests' httpx transport stub): streams-and-discards, returns the right `chunks_ok/failed/failures`, honors concurrency, async job lifecycle (202 → `running` w/ `done/total` → `done`). SSRF guards: `url` with scheme / `//` / `..` / userinfo → 400, never a request off-loopback.
- `/v1/stat` against a temp cache tree (reuse the `disk_stat` tests' builder): cached/missing/errors counts, mode-000 exclusion, symlink skip. Boundary: non-32-hex hash → 400.
- `/v1/steam/prefill` + status with the **fake SteamPrefill binary** stub from ① (`tests/platform/steam/`): drives the driver, async lifecycle, `downloaded-state`/`auth-status` reads. Assert no token bytes in any response/log.
- Auth/allowlist: every route 401 without bearer, 403 from a disallowed source, `/v1/health` exempt. Agent lifespan fail-closed boot guard (non-loopback bind + empty allowlist → SystemExit).

**2. `AgentClient` tests** (new, `tests/clients/`) against a mock agent (httpx MockTransport): `pull()`/`steam_prefill()` POST-then-poll until `done` and surface the result; `stat()`/`downloaded_state()`/`auth_status()` single-call mapping; agent-down/401 → **typed error** handlers can catch.

**3. Seam tests — the equivalence proof** (modify existing prefill/validate/validator tests):
- `agent_enabled=false`: existing tests pass **unchanged** — proves the flag-off path is byte-identical to today (the live-deploy safety net).
- `agent_enabled=true` + mocked `AgentClient`: Steam handler calls `agent_client.steam_prefill([app],force)`; Epic handler calls `agent_client.pull(chunks,ua)`; `validate_game` computes cache_key hashes then calls `agent_client.stat(hashes)`. Assert the **same** DB writes (status, last_prefilled_at, cached_version, validate enqueue) either way.
- Agent-unreachable with the flag on → job recorded failed with `last_error`, worker survives; `/health` `agent_reachable:false`.

**4. End-to-end** (new, `tests/agent/test_e2e.py`): a real in-process agent app + a real `AgentClient` pointed at it (ASGI transport, no network), driven through a prefill and a validate against the fake-lancache + temp-cache harnesses. Proves the full control→agent→result loop without seam mocks.

**5. Equivalence drift guard:** given a manifest's chunk shas, assert the hashes computed control-side map to the same cache paths the agent stats — so the compute/stat split doesn't drift from today's single-process `validate_game`.

**Live testing** (operator-collaborative, §5 step 4): the only non-automated leg — one real prefill + one real validate through the deployed agent, counts matched against baseline. Steam 2FA only if SteamPrefill itself needs re-auth (already authed, so likely not).

## 7. Scope / YAGNI

**In scope (②):** the agent service (`/v1/pull`, `/v1/stat`, `/v1/steam/prefill` + status reads, `/v1/health`, async job registry, reused bearer+allowlist+boot-guard); `AgentClient`; seam rewire of the Steam-prefill handler, Epic-prefill handler, and `validate_game` behind `agent_enabled`; one image / two entrypoints; the agent container with host mounts; the orchestrator drops its `/SteamPrefill` mount after the flip; the migration flag, deploy, live smoke, and the equivalence test suite.

**Out of scope (deferred, with where they land):**
- **The LXC move (step ④).** The agent link stays loopback; `agent_base_url` is `127.0.0.1`. No cross-host networking, no Game_shelf `ORCH_API_URL` change, no allowlist re-point. ② only *proves the boundary* on one host.
- **Deleting the steam worker / modern manifest source** — still the named ①-follow-up. `manifest_expand` stays control-side via the kept worker; the agent does not parse manifests. cache_key compute stays control-side.
- **Epic auth / anything Epic beyond relocating its chunk-pull behind `/v1/pull`.** EpicClient (OAuth/manifest) stays put.
- **Splitting the bearer token / allowlist between API and agent** — one `orchestrator_token`, one `allowed_source_ips` for now; ④ may split.
- **Persisting the agent's job registry** — ephemeral in-memory only; durability is the orchestrator's DB job. No DB on the agent.
- **Progress streaming / SSE / websockets** — poll-based only; no richer progress than `done/total`.
- **Throttling / nice / ionice / load-aware scheduling** — carried as a note for the agent (the north-star throttling concern), not built here; the agent runs pulls at today's behavior.
- **A plugin/abstraction framework for pullers** — the `{url,host}` contract already unifies Steam+Epic; no plugin system (YAGNI).

**Thesis:** ② turns one in-process call into one authed loopback HTTP call, with the live system never more than a flag-flip from current behavior — nothing more.
