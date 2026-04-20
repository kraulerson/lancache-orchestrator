# Architecture Proposal — lancache_orchestrator

**Phase:** 1
**Step:** 1.2
**Generated from:** `PRODUCT_MANIFESTO.md` + `lancache-orchestrator-brief.md` rev 3 + `PROJECT_INTAKE.md` §6 + Phase 0 Data Contract
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator selection and ADR-0001

---

## 1. Context and Hard Constraints

These decisions are **already locked** by the Intake (Section 6.4) and the Product Manifesto. Every option below must honor them:

- **Language:** Python 3.12 (hard constraint — steam-next and legendary are Python).
- **Database driver:** `aiosqlite` with raw SQL, numbered `.sql` migration files, no ORM (hard constraint; also OQ/DQ decisions).
- **Primary API framework:** FastAPI (hard constraint).
- **Target platform:** single Linux Docker container on DXP4800 NAS (ARM, 2.5 GbE).
- **Process topology:** "single container is simpler" per Brief §6.2 — multi-container orchestrator split is explicitly rejected.
- **No second SPA, no ORM, no broker** — Brief §6.2.
- **Configuration:** env vars + Docker secrets only (DQ7, Brief §3.9).
- **Deployment:** Docker Compose alongside existing Lancache on the DXP4800.
- **Observability:** structlog → JSON on stdout, Docker logging driver owns retention (DQ4).

**What is still open for Step 1.2** is the **process topology inside the container** and the **event-loop discipline** for the chunk-download fan-out under sustained API load. Brief §3.6 calls out Spike F ("API responsiveness under load") as a gating test. Option A is the primary path; Option B is the pre-documented fallback if Spike F fails; Option C is included for completeness but conflicts with the hard constraint.

---

## 2. Competency Matrix Input (Appendix B of Manifesto)

Partial / No domains drive mandatory tooling per Builder's Guide §0.6 enforcement:

| Domain | Status | Compensating tool for this proposal |
|---|---|---|
| Security | Partially | Semgrep 1.157.0 + gitleaks 8.30.1 + Snyk CLI 1.1304.0, CI-gated |
| Accessibility | Partially | Manual colorblind-sim review (Intake §9 hard constraint; no formal WCAG AA scope) |
| Performance | Partially | **Spike F load test gates the selected architecture** |

Spike F is load-bearing — it is the empirical gate between Option A and Option B.

---

## 3. Options

### Option A — Single-container monolith on a single asyncio event loop (Brief's recommendation)

One `uvicorn` process runs FastAPI and APScheduler in-process. Three cleanly separated work zones per Brief §3.6:

1. **Main asyncio event loop:** FastAPI handlers, `httpx.AsyncClient` chunk fan-out (`async with client.stream(...) + aiter_raw`), `aiosqlite` queries, APScheduler triggers.
2. **Dedicated gevent-patched thread** for the `steam-next` `SteamClient` (single long-lived thread started at boot). Main loop talks to it via `loop.run_in_executor(steam_thread_pool, fn)`.
3. **Default `ThreadPoolExecutor`** for disk-stat bursts and MD5 computation (F7 validator, F13 sweep) — `min(32, cpu_count + 4)` workers. CPU-bound hashing and blocking `os.stat()` batched in 256-file chunks via `loop.run_in_executor(None, ...)`.

| # | First-class decision | Selection |
|---|---|---|
| 1 | **Languages & Frameworks** | Python 3.12, FastAPI latest stable (verify via Context7 at Phase 2 init), `uvicorn[standard]` with uvloop + httptools, `httpx[http2]`, `aiosqlite`, `APScheduler` 3.x, `structlog` latest, `pydantic` v2 + `pydantic-settings`, `click`. Vendored: `fabieu/steam-next` by git SHA, `legendary-gl/legendary` subset by git SHA. |
| 2 | **Data storage** | SQLite file at `/var/lib/orchestrator/state.db` with WAL mode. 7 tables per Brief §5 + DQ2 decisions, created in `migrations/0001_initial.sql` with `ON DELETE RESTRICT` on `games.platform` FK (DQ8). `manifests.raw` as BLOB (DQ3). Raw SQL via `aiosqlite`. Numbered migrations applied by a ~50-LoC in-repo runner. |
| 3 | **Application architecture pattern** | Layered monolith: `api/` (FastAPI handlers, Pydantic models), `adapters/` (Steam + Epic platform adapters implementing `PlatformAdapter` protocol per Brief §3.1), `core/` (scheduler, diff engine, block list, job runtime), `db/` (aiosqlite connection pool + migration runner + repository functions), `cli/` (Click entrypoints calling the API over `127.0.0.1:8765`), `validator/` (disk-stat + cache-key computation, pure functions + I/O wrappers). Shared `correlation_id` propagated through async context (`contextvars`). |
| 4 | **Authentication & Identity** | Two boundaries: **(a) Platform auth** via per-platform session files at mode 0600 — Steam CM refresh token + Epic OAuth tokens. Credentials only ever enter via the Click CLI on the DXP4800 host (F11). **(b) REST API auth** via static bearer token loaded from `/run/secrets/orchestrator_token` at startup. Timing-safe comparison (`hmac.compare_digest`). `POST /api/v1/platforms/{name}/auth` additionally requires `request.client.host == '127.0.0.1'` (OQ2). No user accounts, no session cookies, no OAuth — LAN-only single-operator system. |
| 5 | **Observability** | structlog JSON output to stdout at INFO by default. `correlation_id` generated at API-request entry or job creation, propagated via `contextvars` through every async call. Log schema includes: `timestamp`, `level`, `event`, `correlation_id`, `component`, `platform` (where relevant), `app_id` (where relevant), `job_id` (where relevant), `duration_ms`, `error` (on failures). `/api/v1/health` exposes `scheduler_running`, `lancache_reachable`, `cache_volume_mounted`, `validator_healthy` booleans and returns 503 if any is false (JQ3). Docker logging driver (operator's compose-level concern) owns rotation + retention (DQ4). |
| 6 | **Secrets management** | Docker secrets at `/run/secrets/*`. Required: `orchestrator_token`. Optional: none in MVP (DQ1 dropped the Steam Web API key). Loader strips whitespace + validates min-length (32 chars for token). Fails-fast on startup if required secret missing: `CRITICAL orchestrator_token_missing` + exit(1). Never in env vars, never in DB, never in logs (SHA256 prefix only). |
| 7 | **Build & packaging** | Multi-stage Dockerfile: builder stage (Python 3.12-slim + build-essential + git) installs pinned `requirements.txt` + vendored git repositories → runtime stage (Python 3.12-slim) copies `site-packages` + application code + `orchestrator-cli` entrypoint on `$PATH`. Single image ~200 MB target. Published to `ghcr.io/kraulerson/lancache-orchestrator:{semver}`. GitHub Actions CI builds + tests + publishes on tag. `requirements.txt` + `requirements-dev.txt` pinned with hashes (pip-compile output). Lockfile committed. |
| 8 | **Scalability vs. Velocity** | Favor velocity strongly — this is a single-user system with zero external users. Scalability target: 1 operator, ~2,600 games, 12 TB cache. Concurrency is bounded locally (`PREFILL_CONCURRENCY=32` chunk-level, `PER_PLATFORM_PREFILL_CONCURRENCY=1` game-level). No horizontal scaling. No multi-region. If a future need emerges, Option B or C become the path. |
| 9 | **Distribution** | `docker compose pull && docker compose up -d` against `ghcr.io/kraulerson/lancache-orchestrator:latest` (or pinned semver). Operator-invoked. Auto-update is intentionally **not** in MVP scope (Intake §10) — Watchtower-style tooling is a Post-MVP consideration. Release notes at each tag. |
| 10 | **Auto-update mechanism** | None. Operator pulls manually. Version displayed in `/api/v1/health` and status page, consumed by Game_shelf for soft-warning on skew (F17). |

**Option A event-loop discipline (Brief §3.6 expanded):**

Under sustained prefill load, the main loop must stay responsive for `GET /api/v1/health` at p99 < 100 ms. The key invariants:

- Every HTTP handler is `async def` and awaits only on `aiosqlite` or `httpx` or executor pool calls — never synchronous I/O.
- `httpx.AsyncClient` chunk fan-out uses `async for _ in r.aiter_raw(chunk_size=65536): pass` with a 10 s per-chunk read timeout. The 32-way semaphore is cooperative; any one slow socket does not block the API because each connection awaits on `aiter_raw` independently.
- `steam-next`'s gevent-patched `SteamClient` lives in a dedicated thread pool (`ThreadPoolExecutor(max_workers=1, thread_name_prefix='steam-cm')`) launched at boot. Gevent's monkey-patching is restricted to that thread. The main loop only ever calls `loop.run_in_executor(steam_pool, fn)` to reach it.
- `os.stat()` and `hashlib.md5` batches run in the default `ThreadPoolExecutor` via `run_in_executor(None, ...)`.
- CI-enforced lint rule: `requests`, `urllib`, `sqlite3`, and `time.sleep` imports are rejected outside explicit executor wrappers. Ruff custom rule or Semgrep pattern.

**Spike F gate** (must pass before committing to Option A in Phase 2):
- Sustain 32-concurrent chunk downloads to Lancache at ≥ 300 Mbps aggregate for 10 minutes on DXP4800 hardware.
- Simultaneously poll `GET /api/v1/health` from a second client every 500 ms.
- Target: p99 < 100 ms, p50 < 30 ms.
- If fail: switch to Option B.

**Pros.**
- Minimum operational complexity — one container, one image, one process, one set of logs.
- Fastest iteration — any change redeploys the whole thing in one shot.
- Minimum debugging surface — single `strace` / `py-spy` target.
- Matches Intake + Brief hard constraints exactly.
- Smallest image.
- Recovers automatically on crash via `restart: unless-stopped`.

**Cons.**
- Gevent + asyncio coexistence is fragile (Risk R10 in Brief). Requires the dedicated-thread discipline to be watertight. One accidental sync `requests.get` on the main loop stalls the API.
- Event-loop saturation under prefill load is an empirical risk (Risk R22). **Mitigated by Spike F, not by design alone.**
- Single failure domain — if APScheduler dies, the whole process 503s on `/api/v1/health` (by design per JQ3); if uvicorn dies, everything goes down; if the Steam CM thread gets into a bad state, prefills stop (but API remains up).

---

### Option B — Single-container with subprocess-isolated downloader (Brief §3.6 fallback)

Two processes inside the same container image:

- **Process A — Orchestrator core:** FastAPI + APScheduler + validator + DB. Pure async, no gevent, no chunk fan-out. Launches Process B on demand.
- **Process B — Downloader worker:** Spawned per-cycle (once per `F12` tick, lives until cycle completes). Holds the `steam-next` CM session in its own process. Performs all chunk fan-out. Reports progress and completion to Process A via a Unix domain socket (JSON line protocol) or stdout line-streaming.
- Both share `/var/lib/orchestrator` (state + session files) and `/data/cache` (read-only for validator in Process A).

Structurally this matches Brief §3.6's contingency: *"If Spike F fails, subprocess-isolate the downloader and re-measure."*

| # | First-class decision | Delta from Option A |
|---|---|---|
| 1 | Languages & Frameworks | Same. Adds `asyncio.subprocess` or `anyio` process orchestration for Process A→B control. |
| 2 | Data storage | Same SQLite, but write coordination: Process A owns writes; Process B writes ONLY via HTTP callback into Process A's internal admin endpoints (localhost), never directly to the DB. Avoids SQLite write-locking conflicts. |
| 3 | Application architecture | Same layered modules, but `adapters/` and chunk fan-out run in Process B's image. Process A uses a client stub to invoke B. |
| 4 | Auth | Same — bearer token shared via the Docker secret; Process B reads it at startup from `/run/secrets/orchestrator_token`. |
| 5 | Observability | Both processes emit structlog JSON on their own stdouts. Docker picks up both via the logging driver. Correlation IDs propagated across the IPC boundary. |
| 6 | Secrets | Same. |
| 7 | Build & packaging | Same Dockerfile, same image. Supervisord or simple `asyncio.subprocess`-driven lifecycle — no second image. |
| 8 | Scalability vs velocity | Slower to iterate (IPC protocol changes require both process updates), but guarantees API responsiveness. |
| 9 | Distribution | Same. |
| 10 | Auto-update | Same. |

**Pros.**
- Guaranteed API responsiveness — Process A never does chunk I/O.
- Clean gevent isolation — gevent lives in Process B, no chance of contaminating Process A's asyncio loop.
- Failure domain isolation — a stuck download can be SIGKILL'd from Process A without bringing the API down.
- Explicit cycle boundary — Process B exits after each cycle, so there's no long-lived state leak.

**Cons.**
- IPC protocol to design + version. Breaking changes require image-wide lockstep.
- Debugging crosses process boundaries — harder strace, harder py-spy.
- Two sets of logs to correlate.
- Image stays the same size but startup cost per cycle grows (spawn + Python interpreter startup + dependency import: ~500 ms to 1.5 s). Noticeable if cycles become frequent.
- More moving parts for a single-operator personal project.
- Still a single container from the operator's perspective, but internally more complex than A.

---

### Option C — Multi-container split (api + worker, rejected)

Two Docker containers:

- `lancache-orchestrator-api`: FastAPI + status page only. Stateless except for reading shared DB.
- `lancache-orchestrator-worker`: Scheduler + adapters + validator. All DB writes.
- Coordination: shared SQLite on a named volume (with WAL) OR a small embedded Redis/NATS for job-state messaging.
- Validator in the API container for on-demand validations; F13 sweep in the worker container. (Or duplicate the validator module in both — ugly.)

| # | First-class decision | Delta |
|---|---|---|
| 1 | Frameworks | Adds a broker (Redis/NATS) OR relies on shared-DB polling. Either is meaningful complexity. |
| 2 | Data storage | SQLite with WAL shared over named volume — works, but two-writer scenarios are a known footgun at scale. For this scale, feasible. |
| 3 | Architecture | Two images, two services in Compose. |
| 7 | Build | Two Dockerfiles OR one Dockerfile with different ENTRYPOINTs — either way, 2× build surface. |

**Why rejected.**
- Intake §6.4 and Brief §6.2 explicitly constrain to single-container.
- Solo operator — multi-container raises the ops-learning bar for zero value at this scale.
- SQLite shared across containers is workable but adds WAL-contention failure modes that don't exist in Option A or B.
- No scalability need to justify the complexity.

**Pros.**
- Clean separation of concerns, textbook scalable pattern.
- API and worker can restart independently.

**Cons (dominant).**
- Violates hard constraint. Two images. Two restart policies. Two sets of logs. Two secret mounts. Extra coordination layer. **Zero value for a single-operator system with a well-specified workload.**

---

## 4. Trade-off Summary

| Decision axis | Option A (monolith) | Option B (subprocess-isolated) | Option C (multi-container) |
|---|---|---|---|
| Ops complexity | Lowest | Medium | Highest |
| API responsiveness guarantee | Empirical (Spike F) | By design | By design |
| Intake constraint compliance | ✅ | ✅ | ❌ violates §6.4 / §6.2 |
| Solo-maintainer fit | Best | Acceptable | Poor |
| Failure domain isolation | Single point | Process A / B split | Full split |
| Time to first running feature | Fastest | Medium | Slowest |
| Debugging surface | Smallest | Larger (2 processes) | Largest (2 containers) |
| Image size | ~200 MB | ~200 MB | ~400 MB combined |
| Iteration speed | Fastest | Slowed by IPC protocol | Slowed by lockstep deploys |
| Risk R22 (event-loop saturation) mitigation | **Empirical — Spike F gates** | **By design** | **By design** |
| Risk R10 (gevent/asyncio clash) mitigation | **Discipline-bound** | **Process-isolated** | **Process-isolated** |

---

## 5. Recommendation

**Select Option A (single-container monolith) for MVP.** It matches the Intake's hard constraints, minimizes ops complexity for a solo operator, and is the Brief's explicit primary design.

**Spike F is a hard pre-commit gate.** Before Phase 2 begins the Steam Adapter feature, Spike F must run against real DXP4800 hardware and demonstrate p99 < 100 ms on `/api/v1/health` during sustained 32-concurrent prefill load. This is logged as **Milestone A (Spikes)** per OQ3 terminology.

**If Spike F fails:** Option B is the pre-documented fallback. The Option A layered-modules structure (`api/ adapters/ core/ validator/ db/ cli/`) is deliberately chosen so that adapters + chunk fan-out can be hoisted into a subprocess without restructuring the API or validator code paths. This keeps the cost of falling back from A to B bounded to ~1 week of IPC-protocol work plus re-running the relevant feature tests.

**Option C remains rejected** even if both A and B fail. Re-evaluation would require revisiting the Intake's single-container hard constraint with a documented scope change.

---

## 6. Implementation Path (Phase 2 preview, not decisions)

The following are not Phase 1 decisions but inform the selected architecture's early-Phase-2 sequencing:

- **Build Milestone A (Spikes A–F):** Dedicated pre-construction phase per Brief §8. Spike F is the gate; Spikes A–E validate specific technical assumptions.
- **Build Milestone B (Steam adapter + core):** F1, F3, F5, F9, F10, F11, F12 per the MVP Cutline.
- **Build Milestone C (Epic adapter):** F2, F4, F6.
- **Build Milestone D (Game_shelf integration):** F14, F15, F16, F17 — cross-repo PR to `kraulerson/Game_shelf`.
- **Build Milestone E (Ops hardening):** F13 sweep + backup tooling + pfSense rule documentation.

F7 (validator) and F8 (block list) interleave naturally across Milestones B and C.

---

## 7. Open Items Feeding into Sub-ADRs (Phase 2 issuance)

Sub-ADRs scoped for Phase 2 issuance (or Phase 1 if time permits). These do not block ADR-0001:

- **ADR-0002:** `fabieu/steam-next` pin + 15-day-silence fork policy (OQ4).
- **ADR-0003:** `MemoryJobStore` over `SQLAlchemyJobStore` (documented in Brief §3.5; formalize).
- **ADR-0004:** Raw SQL + numbered migrations (no ORM) — DQ2/DQ3/DQ8 consolidated.
- **ADR-0005:** Asyncio-only downloader vs subprocess isolation — decision binds after Spike F runs.
- **ADR-0006:** Vendored `legendary` modules vs PyPI `legendary-gl` dependency.
- **ADR-0007:** Lancache reached via compose service name (`http://lancache:80`) rather than IP-probing.

---

## 8. Review Checklist (per Builder's Guide §1.2)

- [x] 3 options proposed — ✅ A (monolith), B (subprocess-isolated), C (multi-container)
- [x] 10 first-class decisions per option — ✅ tabled for A; delta-form for B and C
- [x] Stack familiarity from Intake §6.1 honored — ✅ Python / FastAPI / Docker
- [x] Budget ceiling respected — ✅ $0 incremental, homelab hardware
- [x] Solo maintainer weighting — ✅ Option A favored; Option B fallback documented
- [x] Target platforms covered — ✅ single Linux Docker container on DXP4800
- [x] Competency Matrix incorporated — ✅ mandatory tools cited; Spike F tied to Partially-flagged Performance domain
- [x] Recommendation with rationale — ✅ Option A selected, Spike F as empirical gate, Option B pre-documented fallback
- [x] Rejected alternatives documented — ✅ Option B (if Spike F passes, B is on-hold not chosen); Option C explicitly rejected against Intake constraint

---

## 9. Sign-off

**Awaiting Orchestrator selection of Option A, B, or C (recommendation: A).**

**On selection:** issue `ADR-0001` capturing the decision with this proposal as reference material. Any deviation from Option A during Phase 2 requires a superseding ADR.

**Next Phase 1 step:** 1.3 — Threat Model & Stress Test (Penetration Tester persona).
