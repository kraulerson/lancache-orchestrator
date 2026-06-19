# Orchestrator Re-Architecture — North-Star Design + Roadmap

**Date:** 2026-06-19
**Status:** Approved (north-star architecture + decomposition). Each sub-project below gets its own spec → plan.
**Repo:** lancache_orchestrator. **Branch:** `feat/re-architecture` (doc only).

> This is an **architecture/roadmap** doc, not an implementation spec. It defines the target design and decomposes it into 4 sequenced, independently-shippable sub-projects. Detailed design + TDD plans are produced per sub-project as we build them.

---

## 1. Why re-architect (three problems, diagnosed live this session)

1. **Steam auth is on a dead foundation.** The steam worker uses `ValvePython/steam 1.4.4` (gevent subprocess), stuck on Steam's **legacy `login_key` flow that Steam deprecated in 2023**. Steam no longer issues login_keys, so nothing persists (the credential dir is empty); the session lives only in worker memory. Diagnosed live: a slow depot manifest wedged the worker ~10 min → IPC timeout → worker restart → **session lost with no relogin → 2,298 cascading `NotAuthenticated`** (a 2,484-game sweep got ~119 successes). **SteamPrefill never has this** because it uses SteamKit2's modern auth (`BeginAuthSessionViaCredentialsAsync` + `IsPersistentSession=true` → ~6-month JWT refresh token, persisted, relogin with token). It's a library-generation gap, not a bug we can patch.
2. **The orchestrator is CPU-starved.** It runs in the lancache VM **on the UGREEN NAS** (4-vCPU HARD cap, shared with NAS duties → **~36% CPU steal**, not disk I/O). That starvation is a direct contributor to the worker wedges/timeouts. Adding vCPUs to the VM doesn't help (steal would worsen). See [[project_infra_topology]].
3. **It can't simply move to more resources.** A prefill = the orchestrator acts as the **download client**: `prefill/downloader.py` (Steam) and `prefill/epic_downloader.py` (Epic) `client.stream("GET", chunk_uri)` to lancache (loopback `127.0.0.1`, spoofed CDN `Host`) and `async for _ in resp.aiter_bytes(): pass` — **stream-and-discard** — so lancache caches the chunk. Lancache has **no cache-warm/control API**; caching only happens when a client requests + reads the bytes. So whoever triggers a prefill **receives the full byte volume** → moving the whole orchestrator off the lancache host **hairpins** every prefill (CDN → lancache → remote orchestrator → discard).

**What is already fine (do not touch):** **Epic** — `EpicClient` already mirrors EpicPrefill (modern OAuth, **persists + refreshes + re-saves the refresh token** to `epic_session_path`). Only inherent caveat (shared with EpicPrefill): Epic refresh tokens expire ~weeks, so periodic re-auth — by design, not a defect.

## 2. Target architecture

A **control-plane / data-plane split**, where the heavy/cache-touching work stays local and the brain becomes movable.

### Control plane (stateless brain — eventually a Proxmox LXC)
API, DB, scheduler, Game_shelf integration, validation **logic**, **Epic OAuth + manifest** (unchanged), and **Steam orchestration** (decides what to prefill/validate, drives the Steam tool via the agent, records state). Holds no bulk bytes — all metadata + KB-scale platform-API calls.

### Data plane (thin local agent — stays on the UGREEN lancache host, loopback, no hairpin)
A small **authenticated HTTP agent** that executes three job types on command:
1. **Steam → DepotDownloader** (SteamKit2 CLI). Does modern refresh-token auth (persisted ~6-month, SteamPrefill-grade), manifest fetch, and the download through lancache — all locally. **This replaces the entire ValvePython/steam worker** (gevent, IPC, wedge/restart machinery, the auth hack — all deleted).
2. **Epic → the thin HTTP chunk-puller** (today's `epic_downloader.py` stream-and-discard, relocated). The control plane's `EpicClient` produces chunk URLs; the agent pulls them locally.
3. **Validation → local cache disk-stat** (F7). Kept local (NFS disk-stat over millions of cache files is slow). The control plane computes the cache-key/manifest input; the agent stats the cache and returns counts.

### Control ↔ data interface
A small **bearer-authed HTTP agent** on the UGREEN. The control plane POSTs jobs ("prefill app X", "validate app Y", "pull these Epic chunks") and receives progress/results. Reuses primitives already shipped: bearer token + the source-IP allowlist (`SourceAllowlistMiddleware`). Loopback while the control plane is co-located; LAN after the LXC move.

### Why this is the right shape
- **No hairpin:** all byte-pulling + cache-stat stays on the lancache host.
- **Steam fragility deleted:** the orchestrator stops re-implementing Steam; DepotDownloader (SteamKit2) is the canonical, modern, persistent-auth tool.
- **Resource win unlocked:** the brain (control plane) can move to a resourceful Proxmox LXC.
- **Epic unchanged:** already correct; just its chunk-puller relocates into the agent.

## 3. Roadmap — 4 sequenced, independently-shippable sub-projects

Each gets its own spec → plan; each ships value on its own.

1. **DepotDownloader integration spike** *(GATING; live, with Karl's account).* Confirm DepotDownloader (or the right SteamKit2 tool) does, per-app: persistent modern auth, manifest fetch, and **lancache-warm without saving the game to disk** — the key open question (DepotDownloader **saves files by default**; SteamPrefill **discards** for lancache-warming). The spike resolves the exact tool + invocation (DepotDownloader discard/validate mode vs wrapping SteamPrefill's selective-app mode). **If no clean lancache-warm path exists, revisit before building.**
2. **Data-plane agent extraction.** Build the thin local HTTP agent (Epic chunk-puller + F7 disk-stat + a Steam-tool runner) behind the bearer-authed interface, **still entirely on the UGREEN** (no move yet). Refactor the control plane to call the agent instead of doing the pulls in-process. Ships: clean separation, testable boundary, no behavior change.
3. **Steam rework.** Control plane drives the Steam tool through the agent; **delete** the ValvePython/steam worker + the superseded encrypted-password approach. Steam now persists like SteamPrefill (auth once, ~6 months).
4. **Move the control plane to a Proxmox LXC** *(last, riskiest).* Deploy the control plane on a Proxmox LXC (the cluster where Game_shelf/Caddy/pihole/homepage already live, healthy/idle); the agent stays on the UGREEN. **Rewires Game_shelf** (`ORCH_API_URL` moves off `192.168.1.40`) **and the source-allowlist** (control plane ↔ agent direction). Done last so the split is proven on one host first.

## 4. Migration & invariants

- **No-hairpin invariant:** byte-pulling + cache disk-stat ALWAYS run on the lancache host. Any design that routes prefill bytes off the UGREEN is wrong.
- **Live system continuity:** F14–F17 Game_shelf integration is **live**, talking to the orchestrator at `192.168.1.40:8765` via the LAN-bind allowlist. Steps 1–3 keep the orchestrator API where it is (no Game_shelf change). Only step 4 moves the API → at that point update Game_shelf's `ORCH_API_URL` + re-point the allowlist (control plane ↔ agent), and verify F14–F17 against the new endpoint.
- **Security primitives reused:** bearer + `SourceAllowlistMiddleware` (already shipped) secure the control↔agent link; the agent is bearer-gated + source-restricted to the control plane.
- **Throttling carries forward:** the agent runs Steam/Epic work at low priority + load-aware on the 4-vCPU NAS (the throttled-first-run concerns from the prior spec move into the agent).

## 5. Superseded / out of scope

- **Superseded:** the encrypted-password persistent-Steam-session spec + plan (`docs/superpowers/specs/2026-06-18-persistent-steam-session-design.md`, plan `2026-06-18-persistent-steam-session.md`, branch `feat/persistent-steam-session`, commits 02a360a/cd0741f) — replaced by delegating Steam to DepotDownloader. Do not build it; mark abandoned.
- **Out of scope (future feature):** a one-time **web login page** (Game_shelf form → a scoped OQ2 exception for the allowlisted Game_shelf host) so credential intake isn't CLI/SSH. Becomes pleasant once persistence is real; its own brainstorm later.
- **Epic rework:** none — Epic is already correct; only its chunk-puller relocates (step 2).

## 6. Open questions (resolved by the step-1 spike, before the Steam-rework spec)

- DepotDownloader **lancache-warm**: can it download-through-lancache **without persisting the game to local disk** (discard/null target, or a `--validate`/manifest-only mode that still warms the cache)? If not, do we wrap **SteamPrefill** instead (it's lancache-native + Karl already runs it)?
- Per-app control granularity: can the chosen tool prefill/validate **specific apps + version-diff** (the orchestrator's value-add), not just "the whole library"?
- Auth-store location + sharing: where does the tool persist its refresh token, and can it live on the data-plane host independent of the control plane? (Could even **reuse Karl's existing SteamPrefill auth store** if compatible — to be confirmed.)
- Invocation/IPC: CLI args + parsed stdout vs a structured mode; how the agent drives it and reports progress.

## 7. Files / components (anticipated, across sub-projects)
- **New:** the data-plane agent (own small service on the UGREEN); a control-plane client for it.
- **Replaced/deleted (step 3):** `platform/steam/` worker (worker.py/client.py/session.py/credentials.py), `prefill/downloader.py` (Steam) → DepotDownloader; the steam-worker venv + `requirements-steam-worker.txt`.
- **Relocated (step 2):** `prefill/epic_downloader.py` → agent; F7 validator disk-stat → agent.
- **Unchanged:** `platform/epic/` (EpicClient), API routers, DB, scheduler, Game_shelf integration (until step 4 endpoint move).
