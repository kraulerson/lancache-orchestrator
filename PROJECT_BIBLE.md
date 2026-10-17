# Project Bible — lancache_orchestrator

<!--
  The governing technical document for Phase 2 onward.
  Synthesizes all Phase 0 and Phase 1 outputs into one authoritative reference.
  Every architectural decision lives here. Every Phase 2 feature is built against
  this Bible. Every Phase 3 audit verifies against it. Every Phase 4 deploy
  conforms to it.

  Completion gates entry to Phase 2. Minimum 14 numbered sections required by
  check-phase-gate.sh; 16 present.

  No unfilled ISO-date placeholders (check-phase-gate.sh warns on strings of the form four-digit-year dash two-digit-month dash two-digit-day used as placeholders).
-->

**Track:** Light
**Deployment:** Personal
**Framework:** Solo Orchestrator v1.0
**Phase Gate:** Phase 1 → Phase 2
**Document version:** 1.0
**Issued:** 2026-04-20
<!-- Last Updated: 2026-04-20 -->

**Canonical source files** (do not edit the Bible directly if editing these):
- `PRODUCT_MANIFESTO.md` — governing scope & features
- `PROJECT_INTAKE.md` — governing constraints
- `lancache-orchestrator-brief.md` — technical reference rev 3
- `docs/phase-0/frd.md`, `docs/phase-0/user-journey.md`, `docs/phase-0/data-contract.md`
- `docs/phase-1/architecture-proposal.md`, `docs/phase-1/threat-model.md`, `docs/phase-1/data-model.md`, `docs/phase-1/interface-spec.md`
- `docs/ADR documentation/0001-orchestrator-architecture.md`

---

## 1. Product Manifesto (embedded)

<!-- Last Updated: 2026-04-20 -->

The full `PRODUCT_MANIFESTO.md` (418 lines) is the authoritative product scope. Key elements inlined below; refer to the full document for the complete MVP Cutline and Post-MVP backlog.

### 1.1 Product Intent

A fully autonomous Python service running on a DXP4800 NAS alongside Lancache. It owns its own SQLite database, APScheduler cron, per-platform authentication (Steam CM + Epic OAuth), and a FastAPI REST API on port 8765. It proactively fills the Lancache nginx cache with owned Steam and Epic games, **validates cache state by reading the nginx cache directory from disk rather than trusting a flat-file log** (the core value proposition), and exposes state to operators via CLI, a single-file HTML status page, and a REST API consumed by Game_shelf. **Zero runtime dependency on Game_shelf.**

### 1.2 MVP Must-Haves (17 features)

Full expansion at `docs/phase-0/frd.md` §2. Summary:

- **F1–F4.** Per-platform authentication (Steam CM, Epic OAuth) + library enumeration.
- **F5–F6.** CDN prefill via stream-discard through Lancache (host-header override pattern).
- **F7.** Disk-stat cache validator.
- **F8.** Block list.
- **F9.** REST API (FastAPI on :8765, bearer-auth).
- **F10.** Single-file HTML status page at `GET /`.
- **F11.** Click-based CLI bundled in the container.
- **F12.** Scheduled sync cycle (6h default).
- **F13.** Weekly full-library validation sweep (OQ7).
- **F14–F17.** Game_shelf integration — backend proxy routes, CacheBadge + CachePanel, Cache dashboard page, bidirectional graceful degradation (promoted by OQ1).

Plus ten implicit-dependency foundations (ID1–ID10) covering migrations, Lancache self-test, structured logging, secrets, post-prefill validation hook, startup job reaper, CLI bundling, accessibility compliance, backup, pfSense rule documentation.

### 1.3 Post-MVP (explicitly deferred)

SSE/WebSocket progress stream; access-log tail; Prometheus metrics; incremental validation; CLI `--json`; webhook/ntfy notifications; new-purchase fast cycle; CLI fuzzy title search; diagnostic bundle; LRU pressure alert; backup verification.

### 1.4 Explicit Will-Not-Have

Ubisoft Connect prefill, EA App prefill, GOG prefill, game installation, multi-user support, second React SPA, ORM (SQLAlchemy / Alembic).

### 1.5 Phase 0 Decisions Carried Forward

All 18 Phase 0 questions (7 OQs + 3 JQs + 8 DQs) are resolved in `PRODUCT_MANIFESTO.md` §8. Standing decisions that govern Phase 2:

- `steam-next` pinned by git SHA; fork to `kraulerson/steam-next` on >15 days upstream silence (OQ4).
- `POST /api/v1/platforms/{name}/auth` is 127.0.0.1-only (OQ2).
- Brief "Phase 0–4" renamed "Build Milestones A–E" (OQ3) to disambiguate from Solo Orchestrator methodology phases.
- Mutation responses use the normalized envelope `{"ok", "job_id", "message"}` (DQ6).
- `manifests.raw` is a compressed BLOB in SQLite (DQ3); `cache_observations` ships in 0001 even though populated Post-MVP (DQ2); `games.platform` FK uses `ON DELETE RESTRICT` (DQ8).
- Manifest response size cap: 128 MiB, configurable (DQ7).
- `/api/v1/health` surfaces `scheduler_running: bool` and returns 503 on scheduler death (JQ3).

---

## 2. Cost Constraints & Revenue Model

<!-- Last Updated: 2026-04-20 -->

**SKIPPED revenue content — internal tool, Light track, Personal deployment.**

**Cost constraints (operational):**
- Infrastructure: $0 incremental (runs on existing DXP4800 NAS).
- One-time budget: $0 (all hardware existing).
- Ongoing: $0.
- Hosting cost ceiling: N/A (self-hosted LAN).
- AI tooling: existing Claude Max subscription (sunk cost).

No break-even calculation, no per-user costs, no hosting ceiling at scale — the system is explicitly single-operator, LAN-only, non-commercial. If ever open-sourced under MIT-or-similar, the cost model is still zero (each deployer pays their own hardware).

---

## 3. Architecture Decision Record

<!-- Last Updated: 2026-10-16 -->

### 3.1 Selected architecture — control plane plus data-plane agent (rewritten 2026-10-16)

> **The single-process monolith that ADR-0001 chose no longer exists.** ADR-0001
> (2026-04-20) put everything in one `uvicorn` process, including Steam through a
> gevent-patched `steam-next` thread. Re-architecture ①–④ (PRs #174–#199) moved the
> work that touches the cache into a data-plane agent beside lancache, and deleted
> the in-process Steam worker (ADR-0013, superseded 2026-06-22). Nothing in `src/`
> imports `steam-next` or `gevent` today. This section describes what runs now;
> ADR-0001 is kept for history.

**Control plane** (LXC 1105). One async-native `uvicorn` process runs FastAPI, APScheduler, the jobs worker and the `aiosqlite` pool (§5.6). It talks to the agent and to Uptime Kuma over `httpx`. **Jobs run one at a time** on a single worker (`jobs/worker.py`), so a long job delays the next scheduled one instead of running beside it.

**Data-plane agent** (NAS, beside lancache; `python -m orchestrator.agent`). It reads the lancache filesystem directly. Its routers are `health`, `pull`, `stat`, `steam`, `epic` and `manual_downloads`, covering:
- chunk validation, disk-stat and purge;
- the prefill drivers;
- Steam library enumeration, auth status and selection maintenance;
- the Steam manifest fetch;
- manual-download listings.

The control plane reaches it only over HTTP (`ORCH_AGENT_BASE_URL`), never through the filesystem. §3.5 explains the split.

**No Steam login runs in the API process.** The two components that hold a Steam session both run on the NAS (§7.2):
- **SteamPrefill** is a host binary. The host cron runs it for the scheduled library, and the agent runs the same binary, bind-mounted at `/SteamPrefill`, for per-game prefill jobs.
- **The manifest helper** (`/steam-manifest-helper/SteamManifestHelper`) is a C# program on SteamKit2 shipped in the image. The agent runs it for the weekly `fetch_manifests` job, and it logs in once per run, plus at most one reconnect (ADR-0019, #361). It replaced one DepotDownloader process per app, which meant 1,211 logins a run.

**Steam code that needs no session does run in the control plane:**
- library sync, which enumerates through the agent and names games from the public store `appdetails` API (`platform/steam/store.py`);
- the selection classifier;
- a `SteamPrefillDriver`, used for auth status only when the agent is off.

**Epic** parses manifests in pure Python, with no vendored `legendary` (ADR-0014).

**History.** At Phase 1:
- **Option B** (a subprocess-isolated downloader) was the fallback if Spike F failed.
- **Option C** (a multi-container split) was rejected, citing Intake §6.4.

The re-architecture later adopted a two-host split anyway, for the operational reasons in §3.5. Spike F's single-process latency gate does not apply to the current design.

### 3.2 Sub-ADRs (Phase 2)

Issued during Phase 2 construction:

- **ADR-0008** — Migration runner architecture (BL1, ID1).
- **ADR-0009** — Logging framework architecture (BL2, ID3).
- **ADR-0010** — Settings module design (BL3, ID4) + BL4 addendum (DB pool fields).
- **ADR-0011** — DB pool architecture (BL4) — hybrid 1-writer-N-reader topology, defense-in-depth write serialization, comprehensive API surface.
- **ADR-0012** — FastAPI skeleton architecture (BL5) — hybrid module layout, pure-ASGI middleware stack (CorrelationId / BodySizeCap / BearerAuth / CORS), lifespan-managed pool, fail-fast 503 health policy.
- **ADR-0013** — steam-next subprocess isolation (BL10). **Superseded 2026-06-22** (re-arch ③c): the Steam worker was deleted.
- **ADR-0014** — Epic prefill parses manifests in pure Python; no vendored `legendary` (F6).
- **ADR-0015** — Operator-driven cache purge (F18, #243).
- **ADR-0016** — Ownership as an explicit input. **Proposed**; its invariant stands, and its mechanism is resolved elsewhere.
- **ADR-0017** — Rejected by adversarial review; there is no file. ADR-0018 records why.
- **ADR-0018** — Standalone ownership and launcher-auth service, including Steam credential custody. **Proposed**, not built.
- **ADR-0019** — One-login Steam manifest helper on SteamKit2 (#361). It replaces one DepotDownloader process per app.

**Scheduled at Phase 1 and never written.** Each decision ended one of three ways:
- **Moot after the re-architecture:** ADR-0002 (pinning `steam-next`), ADR-0005 (Spike F) and ADR-0007 (lancache via the compose service name).
- **Answered elsewhere:** ADR-0006 by ADR-0014, and ADR-0004 in part by ADR-0008.
- **Never written as an ADR:** ADR-0003. The choice of in-memory `MemoryJobStore` is explained in PRODUCT_MANIFESTO.md and in FEATURES.md (F12, D2), and the code is `scheduler/manager.py`.

### 3.3 Stack (pinned; rows updated 2026-10-16)

| Layer | Choice | Pinned via |
|---|---|---|
| Language | Python 3.12 | Base image `python:3.12-slim`, by digest |
| Web framework | FastAPI | `requirements.txt` with hash |
| ASGI server | `uvicorn[standard]` (uvloop + httptools) | `requirements.txt` with hash |
| HTTP client | `httpx[http2]` | `requirements.txt` with hash |
| DB driver | `aiosqlite` | `requirements.txt` with hash |
| Scheduler | `APScheduler` 3.x with `MemoryJobStore` | `requirements.txt` with hash |
| Logging | `structlog` | `requirements.txt` with hash |
| Config | `pydantic` v2 + `pydantic-settings` | `requirements.txt` with hash |
| CLI | `click` | `requirements.txt` with hash |
| Steam manifests | C# (.NET 10) helper on SteamKit2 (LGPL-2.1), `tools/steam_manifest_helper/` | NuGet exact versions with committed lock files; built in the Dockerfile's `helper` stage from a digest-pinned `dotnet/sdk:10.0` |
| Steam downloading | SteamPrefill, a host binary on the NAS | Outside this image. Run by the host cron, and by the agent through a bind mount at `/SteamPrefill` |
| Epic protocol | Pure-Python manifest parsing (ADR-0014) | No vendored code |

### 3.4 Process topology — rewritten 2026-10-16

Two containers on two hosts (§3.5). Both run builds of one image, `orchestrator:dpa`, built on the LXC and copied to the NAS. A deploy to one host only leaves the two on different builds.

- **Control plane** — one `uvicorn` process (`orchestrator.api.main:app`) running FastAPI, APScheduler, the jobs worker and the SQLite pool.
- **Data-plane agent** — one process (`python -m orchestrator.agent`, port 8780). It starts child processes: SteamPrefill for Steam prefill jobs, and the manifest helper during `fetch_manifests`.

No broker, no Celery/Redis. The image measures 254 MB in CI's image-size check, against its 275 MiB amd64 limit. That limit was raised from 250 MiB on 2026-10-07 when the .NET helper was added. The LXC's containerd store reports a larger figure for the same image; compare CI's number.

### 3.5 Deployment topology — SUPERSEDED, rewritten 2026-09-19

> **The single-host design below was replaced by a two-host split** across PRs
> #174–#199 (re-architecture ①–④). The Bible was not updated at the time; this
> section described a system that no longer existed for roughly two months.
> `docs/deploy/INSTALL.md` is the installable description, and
> `docs/deploy/live-configuration.md` is the authoritative operator reference —
> prefer both over this summary.

**Current topology: control plane and data plane on separate hosts.**

| plane | host | runs |
|---|---|---|
| control | Proxmox **LXC 1105** (`10.100.23.105`) | `orchestrator` — API, scheduler, jobs, SQLite |
| data | **NAS** (`192.168.1.30`, UGREEN DXP4800) | `orchestrator-agent`, `lancache-monolithic`, `lancache-dns`, `cache-catcher` |
| monitoring | **CT 1057** (`10.100.23.57:3001`) | Uptime Kuma — receives push heartbeats from both |

**Why the split.** Byte-pulling, disk-stat and the prefill drivers must run
*beside* the cache: the agent reads the lancache filesystem directly, and the NAS
is 4-core and IO-bound. Keeping the API, scheduler and SQLite on the NAS made the
control plane hostage to that IO contention. The control plane moved to an LXC;
the data plane stayed on the NAS. The orchestrator reaches the agent over HTTP
(`ORCH_AGENT_BASE_URL`), never the filesystem.

**Consequences that bite in operations, all learned the hard way:**

- **The agent must run as uid 0** (`--user 0:0`). Some cache directories are mode
  `0700`; as uid 1000 the agent sees 65 of 256 buckets and reports ~8 % cached on
  every game. Verify uid 0 and 256/256 buckets after every recreate.
- **The agent is unreachable from its own host** by the published address; test
  its DNS and reachability *from inside the agent container*, not from the NAS
  shell.
- **A container recreate reaps a running sweep.** 12 of 15 historical sweep
  failures were recreate kills. Check `jobs WHERE state IN ('running','queued')`
  is empty first, and stage slow steps while the old container still serves.
- **`cache-catcher` is the exception:** it serves no traffic and runs no jobs, so
  restarting it needs no window.

**Control-plane container hardening is NOT what the original design specified.**
Corrected 2026-10-16 against `docker inspect orchestrator` on LXC 1105. The
original design called for non-root, `read_only`, `cap_drop: [ALL]`,
`no-new-privileges`, and `orchestrator_token` as a secret file. Of those, only
**non-root** is live: the image's `USER orchestrator`. `/root/deploy-orchestrator-lxc.sh`
runs the container with a writable root filesystem (`ReadonlyRootfs=false`), no
dropped capabilities (`CapDrop=[]`) and no security options (`SecurityOpt=[]`).
The bearer token arrives as the `ORCH_TOKEN` env var from `/root/orch-lxc.env`,
not as a secret file. A previous version of this section said the posture was
"unchanged"; that was never verified. Hardening the deploy script is tracked in
#374. The agent runs as root by design, for the reason above.

---

## 4. Threat Model & Risk/Mitigation Matrix

<!-- Last Updated: 2026-04-20 -->

Full artifact: `docs/phase-1/threat-model.md`. Summary here for in-Bible access.

### 4.1 Assets and actors

8 assets ranked (A1 Steam refresh token → A8 Steam account indirectly). 7 threat actors (T-EXT-LAN → T-PHYS).

### 4.2 STRIDE coverage (23 threats total, stable TM-### IDs)

- **Spoofing:** TM-001 (bearer-token leak via Game_shelf `.env`), TM-002 (compose-network peer spoofs Lancache), TM-003 (LAN DNS poisoning — out of orchestrator scope).
- **Tampering:** TM-004 (session-file tamper on host), TM-005 (SQL injection through API path params), TM-006 (chunk MITM in compose network), TM-007 (poisoned manifest from upstream CDN).
- **Repudiation:** TM-008 (block-list denial), TM-009 (scheduler activity denial).
- **Information Disclosure:** TM-010 (bearer token leak via frontend bundle — **F17 CI invariant**), TM-011 (stack-trace leak in 5xx), TM-012 (log-stream credential leak — Semgrep pattern), TM-013 (version fingerprinting on `/health`), TM-014 (DB file readable on host).
- **Denial of Service:** TM-015 (connection-pool exhaustion), TM-016 (prefill-triggered WAN DoS), TM-017 (scheduler death via malformed cron), TM-018 (manifest memory bomb — 128 MiB cap).
- **Elevation of Privilege:** TM-019 (container escape), TM-020 (supply-chain compromise via steam-next — OQ4 mitigates), TM-021 (CLI argument injection — Click mitigates), TM-022 (setuid in image — CI-audited).

### 4.3 Multi-step kill chain (TM-023)

Detailed in `docs/phase-1/threat-model.md` §2.7: Game_shelf npm-dep RCE → `.env` harvest → orchestrator bearer → library dox → prefill storm → persistence via API-labeled blocks. Mitigations: F17 CI grep + `.env` mode 0600 + Game_shelf auth hardening + pfSense host-specific rules + Phase 3 access-log middleware with client IPs.

### 4.4 Architecture stress test

- 5 edge cases (MemoryJobStore cron loss; WAL contention at F12×F13 overlap; fd exhaustion; gevent patch leak; Steam CM session hang).
- 3 inherent vulnerabilities (discipline-bound event-loop hygiene; single bearer token scope; upstream dependency trust).
- 2 storage bottlenecks (manifests.raw VACUUM at 12mo; jobs/validation_history growth without pruning).
- 1 12-month rewrite risk (if both A and B fail, Option C requires Intake scope-change).

### 4.5 Risk/Mitigation Matrix

Full matrix in `docs/phase-1/threat-model.md` §5 cross-references every TM to its mitigation location, enforcer, and Phase 3 verification strategy. Referenced during every Phase 2.4 security audit and every Phase 3.2 verification run.

---

## 5. Data Model

<!-- Last Updated: 2026-10-16 -->

Full artifact: `docs/phase-1/data-model.md`. Canonical SQL lives at `src/orchestrator/db/migrations/0001_initial.sql` (packaged as Python package-data per ADR-0008, loaded via `importlib.resources`).

### 5.1 Entity inventory

Eleven entity tables + one meta table (migrations 0001–0018):

- `platforms` — enum-like with 2 rows (seeded by 0001).
- `games` — one row per `(platform, app_id)`. Since migration 0015 the row carries **cache truth** (`status`, `status_measured_at`, `last_measure_attempt_at`) and **job outcome** (`last_job_outcome`, `last_job_outcome_at`) as separate, disjoint sets of columns — see §5.7. Migration 0018 (#316) retired the unreachable `failed` value. The 19 owned games stuck on it became `blocked`, after a live check on 2026-09-20 confirmed they cannot be downloaded. It changes data only, no schema: `failed` stays legal in the `CHECK` constraint, which SQLite cannot alter in place.
- `manifests` — one row per `(game_id, version)`, `raw BLOB` compressed.
- `block_list` — independent; no FK to games (allows pre-blocking unknown app_ids).
- `validation_history` — one row per F7 run.
- `jobs` — one row per enqueued operation. There are 8 kinds: `prefill`, `validate`, `library_sync`, `manifest_fetch`, `fetch_manifests`, `sweep`, `auth_refresh` and `purge`. The CHECK constraint was last widened by migration 0014.
- `cache_observations` — populated only when Post-MVP access-log tail ships (DQ2 — schema ships in 0001).
- `measurement_transitions` — one row per cache-truth write, observed or commanded (migration 0015; `commanded` added by 0016): `game_id`, `prior`, `new_status`, `downward`, `commanded`, `occurred_at`. The circuit breaker counts only `commanded = 0` rows — see §5.7. Durable rather than in-memory because the breaker that reads it must survive a restart, and a restart is exactly what produced the 2026-09-01 corruption.
- `sweep_pass` — the validation sweep's pass boundary (migration 0017): `pass_number`, `pass_started_at`, pinned to a single row by `CHECK (id = 1)`. A full pass costs more than one 6 h-capped run, so it necessarily spans several runs; gating candidates on `pass_started_at` is what makes an empty candidate set a *proof of coverage* rather than a coincidence — see §5.7. Single-row by construction because "the current pass" must never be ambiguous.
- `prefill_exclusions` — one row per game excluded from FUTURE scheduled prefill. Migration 0011 created it with a `source` column, which records where each exclusion came from, so an operator's manual choice and the #225 auto-classifier's are distinguishable. Migration 0012 added the `gameshelf` source. Independent of `block_list`, which blocks a game outright; an exclusion only stops the scheduler picking it up.
- `steam_app_info` — cached Steam app metadata. Migration 0008 created it, and 0013 added the category flags the #225 auto-classifier reads to exclude soundtracks, tools, servers and demos. A cache of an external API, not cache truth: it is safe to delete and refetch.
- `schema_migrations` — migration runner meta; created by runner if missing.

### 5.2 Key constraints

- `games.platform REFERENCES platforms(name) ON DELETE RESTRICT` (DQ8).
- `manifests.game_id REFERENCES games(id) ON DELETE CASCADE`.
- `validation_history.game_id REFERENCES games(id) ON DELETE CASCADE`.
- `measurement_transitions.game_id REFERENCES games(id) ON DELETE CASCADE`, plus `CHECK (downward IN (0, 1))` and `CHECK (commanded IN (0, 1))`.
- `jobs.game_id REFERENCES games(id) ON DELETE SET NULL` — job history preserved even if a game row is later removed.
- `UNIQUE(platform, app_id)` on `games` and `block_list`.
- `CHECK` enumerations on `platforms.name`, `platforms.auth_status`, `games.status`, `jobs.state`, `jobs.kind`, `validation_history.outcome`, `cache_observations.event`.
- All tables declared `STRICT` (SQLite 3.37+ type enforcement).

### 5.3 Pragmas (set at migration time, persist for this DB file)

- `journal_mode = WAL` — readers don't block writers.
- `synchronous = NORMAL` — safe under WAL.
- `foreign_keys = ON`.
- `temp_store = MEMORY`.
- `mmap_size = 268435456` (256 MB).
- `cache_size = -32000` (~32 MB page cache).

### 5.4 Migration strategy

Numbered `.sql` files in `src/orchestrator/db/migrations/` (ships as Python package-data via `importlib.resources` — see ADR-0008): filename format `NNNN_snake_case_description.sql`. Down-migrations are NOT in MVP scope (rollback removed in BL1; re-introducing requires a dedicated ADR covering versioning + data-preservation). Every file has a pinned SHA-256 entry in the sibling `CHECKSUMS` manifest (also package-data). The runner (~500 LoC at `src/orchestrator/db/migrate.py`) wraps the full read-applied-and-apply pass in a single `BEGIN IMMEDIATE` / `COMMIT` so same-process concurrent callers serialize; a module-level `threading.Lock` handles same-process races that busy_timeout alone doesn't cover. Pre-apply checks: manifest checksum cross-check (file vs pinned SHA), gap detection (applied ∪ available must be contiguous from 1), local-filesystem assertion (refuses NFS/CIFS/Gluster/Ceph/... because WAL + networked mmap silently corrupts; `ORCH_REQUIRE_LOCAL_FS=strict` upgrades `unknown` fstype to fail-closed). Post-apply sanity check (inside the transaction, before COMMIT) verifies the expected table set derived from each migration's SQL. Any of these checks failing raises `MigrationError` and the container refuses to start.

### 5.5 Retention policy

- `validation_history`: 90-day prune, daily.
- `jobs`: 90-day prune for non-error rows, daily. Error rows kept indefinitely.
- `manifests`: keep latest 3 versions per game, weekly prune.
- `cache_observations`: 30-day prune, weekly (no-op in MVP).
- `measurement_transitions`: no retention policy, and **measured 2026-09-18 as a non-issue**: 18,766 rows and **1.0 MB**. The earlier estimate here of ~1.7M rows/year was wrong by two orders of magnitude — it assumed a row per measurement, but only a *transition* writes one, and a converged library is mostly unchanged measurements. Only rows inside the breaker window are ever read. A prune remains possible but would reclaim nothing worth the risk.
- **What actually consumes the database** (measured 2026-09-18, total ~1.1 GB): `manifests` is **978.8 MB — 92%** — from just 923 rows, because each stores a compressed manifest blob averaging ~1 MB (ARK ModKit alone is 63 MB). `validation_history` is 53.7 MB across 415,176 rows; everything else is under 4 MB combined. Of the manifest bytes only **73.6 MB** is superseded versions, so the long-deferred "keep latest 3 per game" pruning would recover ~7%; the remaining ~903 MB is the current manifest for each of 757 games and is needed. Growth is event-driven, not steady — 649 MB arrived in August during the manifest-fetcher backfill, and nothing has been fetched since 2026-09-01. Disk headroom is watched by Kuma monitor 217 (see `docs/deploy/live-configuration.md` §1.3).
- `platforms`, `games`, `block_list`: never auto-pruned.

### 5.6 Concurrency model

Single-process, single-writer-lock application-side. Implemented in BL4 as a hybrid `aiosqlite` pool: 1 dedicated writer connection + N reader connections (default 8, configurable via `ORCH_POOL_READERS` 1..32). WAL handles reader concurrency; application `asyncio.Lock` serializes all writes; `BEGIN IMMEDIATE` provides engine-level serialization; `busy_timeout=5000ms` absorbs transient contention. Defense-in-depth eliminates SQLITE_BUSY under F12×F13 overlap (threat-model §4.3.2 mitigation). Reader connections enforce read-only at the SQLite layer via `PRAGMA query_only=ON`. Connection-replacement state machine on disk-I/O errors with per-role storm guard (>3 in 60s → degraded). See ADR-0011 for full architecture.

### 5.7 Cache truth vs job outcome (migration 0015, design 2026-09-04)

Design artifact: `docs/superpowers/specs/2026-09-04-cache-validation-integrity-design.md`.

`games.status` used to carry two unrelated meanings — what a measurement found on disk, and how the last job ended. On 2026-09-01 an interrupted prefill batch stamped 1769 games with a dead job's outcome, and Epic's scheduled prefill (which reads status) would have re-downloaded 655 already-cached titles. The two meanings now live in disjoint columns, and the split is enforced rather than merely documented.

- **One writer.** `games.status` and `games.status_measured_at` are written by exactly one function: `orchestrator.jobs.measurement.record_measurement()`. Every other module — the validate handler, both prefill handlers, the jobs worker, the boot reaper, purge, library_sync — records how a job ended through `record_job_outcome()`, which writes `last_job_outcome` / `last_job_outcome_at` (and mirrors the text into the legacy `last_error` column until that column is retired) and has no access to the truth columns. **The rule is enforced by a build-breaking source scan** (`tests/test_measurement_writer_guard.py`), which fails CI if any other module under `src/orchestrator` contains a statement assigning `status` or `status_measured_at` on `games`. A source scan rather than a runtime test, because the regression it prevents is a new `UPDATE games SET status=…` typed into a handler that no existing test would execute.
- **The four columns.** `status_measured_at` moves only on a successful measurement; `last_measure_attempt_at` moves on **every** attempt, success or failure; `last_job_outcome` / `last_job_outcome_at` record the last job's fate and are never consulted for a download decision. The `status` vocabulary itself is unchanged (renaming it would have forced a snapshot/drop/recreate of the whole table, since SQLite cannot alter a `CHECK` constraint in place). `downloading` and `failed` are no longer written by anything — in-flight state is the `jobs` row (`state='running'`). Since migration 0018, no row holds `failed` either (§5.1).
- **Measurement before download.** Epic's scheduled prefill (`scheduler/jobs.py::enqueue_scheduled_prefill`) selects `status IN ('validation_failed','not_downloaded') AND status_measured_at IS NOT NULL`. Previously `status <> 'up_to_date'`, i.e. "not proven cached" meant "download it". Nothing is downloaded without a measurement that actually looked and found the game absent or incomplete; `unknown` triggers measurement, never traffic. (Steam downloading stays driven by the host SteamPrefill cron and is out of this scope.)
- **Ordering is the resume cursor.** The F13 sweep selects owned games `ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC` and carries **no status filter at all**. A persisted cursor would go stale on every insert or delete and need its own repair path; ordering needs no persisted state, because an attempted game sorts to the back and an interrupted sweep therefore resumes by construction. Removing the status filter removes a whole bug class — no status value can ever be excluded from measurement again. Supported by `idx_games_measure_attempt`.
- **Circuit breaker.** `record_measurement()` ranks the four cache-truth states (`up_to_date` 3, `validation_failed` 2, `not_downloaded` 1, `unknown` 0; every other value carries no rank and takes no part) and refuses to write once `ORCH_MEASUREMENT_BREAKER_THRESHOLD` games have dropped a rank inside `ORCH_MEASUREMENT_BREAKER_WINDOW_MINUTES`, raising `CircuitBreakerTripped` and pushing DOWN to `ORCH_KUMA_PUSH_MEASUREMENT_BREAKER` once per window. Since #313 the once-per-window clock is stamped **after** the push and only on a delivery the monitor accepted; an undelivered push (connection failure, or an HTTP error such as the 404 a mistyped token returns) is retried after a 60 s backoff instead of silencing the incident for the full window. Upward moves are ignored at any volume, so a recovery sweep over repaired `unknown` rows stays silent. The sweep stops on a trip and logs `sweep.aborted` rather than `sweep.completed`, and (since #311) does **not** advance the sweep pass — an aborted run cannot be mistaken for coverage. **`commanded` is the one exception to the veto:** `record_measurement(..., commanded=True)` — passed only by purge, whose files are already unlinked by the time it records — is exempt from the breaker, because a purge is cache truth this system *caused* rather than an observation whose trustworthiness is in question, and refusing it would leave a green badge over an empty cache (security audit finding 1, SEV-2). It is **not** an exception to the transition log: since #310 the row is written and marked `commanded = 1`, and the breaker's window count (and its supporting partial index, rebuilt by migration 0016) reads only unmarked rows. So a purge storm still cannot trip the alarm, and it is no longer invisible in the record of cache truth — which is where the reader looking for "what happened to the library" goes first.
- **Purge measures, it does not assume (#310).** `purge_handler` deletes through the agent and then runs a real validation of the disk, recording *that* as the measurement. It used to record a hard-coded `partial` plus a `chunks_cached = 0` observation whatever the agent reported — and the agent returns HTTP 200 with `{"deleted": 0, "failed": N}` when every unlink fails, which is what happens when it comes back as uid 1000 instead of `0:0`. A bulk purge under that fault wrote `validation_failed` over a completely intact cache and queued the whole library for re-download, with the breaker skipped by construction so nothing raised an alarm. Cache truth now comes from the disk rather than from the agent's report of its own work, which also gets the partial case right: 300 of 337 deleted records the 37 that survived. If the post-purge validation itself fails after files were really deleted, it falls back to the conservative `partial` — leaving the pre-purge status would put a green badge over a cache the system just emptied, which is the defect `commanded` was introduced to fix.

---

## 6. Data Migration Plan

<!-- Last Updated: 2026-04-20 -->

**N/A — no legacy data.**

The orchestrator replaces SteamPrefill/EpicPrefill functionally but does NOT import their state. SteamPrefill's flat-file tracker is the root cause of the problem this project solves (it drifts from actual cache state, reports false positives). Its contents are not authoritative and are not imported.

On first deployment, the orchestrator's initial state is: empty DB, no sessions, no cached_version data. First library sync cycle + first post-prefill F7 validations establish ground truth from platform APIs + disk-stat. This is intentional — the whole point is that ground truth comes from disk, not from prior-tool logs.

Recorded per Builder's Guide §1.4.5 requirement.

---

## 7. Auth & Identity Strategy

<!-- Last Updated: 2026-10-16 -->

### 7.1 Two auth boundaries

The orchestrator authenticates across two unrelated boundaries:

1. **Platform auth** — the operator's Steam and Epic accounts are used to enumerate libraries and download content via Lancache. **Only Epic authenticates through the orchestrator.** Steam logins are held outside the API process (§7.2).
2. **API auth** (consumers → orchestrator) — CLI, status page, and Game_shelf authenticate TO the orchestrator with a static bearer token.

### 7.2 Platform auth (per F1, F2)

**Steam — no orchestrator login (rewritten 2026-10-16).** The `orchestrator-cli auth steam` command and the `steam-next` session file described here originally were deleted with the in-process Steam worker in re-arch ③ (ADR-0013, superseded). Two components hold Steam sessions today, both on the NAS:
- **SteamPrefill** (host binary, host cron) keeps its own persistent login for downloading.
- **The manifest helper** (`/steam-manifest-helper/SteamManifestHelper`, ADR-0019) imported the session token DepotDownloader had already saved, so it needed no phone approval. It uses that session until Steam rejects it. Its exit codes:
  - **Exit 2, token rejected:** the message names the helper's `login` subcommand.
  - **Exit 3, session missing or unreadable:** same advice. This covers no or invalid `session.json`, the wrong account, or no DepotDownloader login to import.
  - **Exit 2, other refusals or Steam unreachable:** no login advice, because the token was never judged.

  To renew, the operator runs `login` once with a TTY and approves it in the Steam app. Nothing renews the session silently.

The planned single custodian for all Steam credentials is ADR-0018 §5.11. It is **Proposed, not built**.

**Epic (Epic OAuth):**
- Interactive auth-code exchange via CLI (`orchestrator-cli auth epic`): user pastes code from `https://legendary.gl/epiclogin`.
- Only the refresh token is persisted, at `/var/lib/orchestrator/epic_session.json`, mode 0600, opened with `O_NOFOLLOW` (`platform/epic/oauth.py::save_refresh_token`).
- Silent refresh with 10-minute pre-expiry buffer; no ongoing user action required.

**Credential handling rules** (threat-model TM-004, TM-012):
- Credentials enter via CLI stdin only — never logged, never written to DB, never in env vars.
- Tokens are never logged, raw or hashed. The `token_sha256_prefix` field planned at Phase 1 does not exist in the code.
- On auth failure, no session file is written (prevents corrupted-state carryover).
- Not implemented: nothing checks session-file modes at startup. The Phase 1 world-readable WARN does not exist.

### 7.3 API auth (per F9)

- Single static bearer token. Settings accept it as the secrets-file name `orchestrator_token` or as the env var `ORCH_TOKEN`. **Live, it is the `ORCH_TOKEN` env var**: from `/root/orch-lxc.env` on the control plane, and on the NAS from `agent.env`, the `env_file` of `orchestrator-agent` in `/home/karl/lancache-host/docker-compose.yml` (corrected 2026-10-16; see §3.5).
- Stripped of whitespace, minimum 32 characters.
- Timing-safe comparison via `hmac.compare_digest` (threat-model TM-001).
- **Missing token at startup → the process refuses to start.** Building the app raises `ValueError: orchestrator_token validation failed: Field required`, and the process exits 1. No CRITICAL line is logged.
- Paths matching `/api/v1/platforms/{name}/auth` require a loopback client (`127.0.0.1`, `::1`) in addition to the bearer (OQ2 / TM-023 mitigation). Today the only such route is `/api/v1/platforms/epic/auth`. The OpenAPI schema and docs pages are loopback-only too (`api/_constants.py::LOOPBACK_ONLY_PATTERNS`).

**Rotation procedure** (documented in Phase 4 HANDOFF.md):
1. Generate new token (`openssl rand -hex 32`).
2. Update `ORCH_TOKEN` in `/root/orch-lxc.env` on LXC 1105 and in `agent.env` on the NAS. The control plane sends its own token as the bearer to the agent (`api/main.py`, `AgentClient(token=...)`).
3. Update `ORCHESTRATOR_TOKEN` env on Game_shelf LXC.
4. Recreate the services in an empty-queue window. A recreate reaps a running sweep (§3.5).
5. Stale tokens return 401 until restart completes.

### 7.4 Explicit non-goals

- No user accounts, no sessions, no OAuth, no SSO, no per-endpoint scopes.
- No TLS for the orchestrator itself (LAN-only; pfSense is the perimeter). TLS between Game_shelf and orchestrator is deferred to post-MVP (Brief Appendix C).

---

## 8. Observability & Logging Strategy

<!-- Last Updated: 2026-09-19 -->

### 8.1 Structured logging

- `structlog` configured at startup → JSON renderer → stdout.
- Every log line is a single JSON object.
- Minimum fields per event: `timestamp` (ISO 8601 UTC), `level` (DEBUG/INFO/WARNING/ERROR/CRITICAL), `event` (short stable identifier), `correlation_id`.
- Event-specific fields: `component`, `platform`, `app_id`, `job_id`, `duration_ms`, `error` — present when relevant.

### 8.2 Correlation IDs

- Generated as UUID4 at every API request entry or job creation.
- Propagated via `contextvars` through every `await`ed call within the request scope.
- Returned in response headers as `X-Correlation-ID` and in every error body.
- Logged in every line emitted during that request / job.

### 8.3 Log retention

The orchestrator writes to stdout; Docker's logging driver owns retention (DQ4). Phase 4 HANDOFF.md documents recommended config:

```yaml
services:
  orchestrator:
    logging:
      driver: json-file
      options:
        max-size: 50m
        max-file: 5
```

Retention is operator-controlled. The orchestrator takes no action on log files.

### 8.4 Health endpoint as observability surface

`GET /api/v1/health` returns 200 with four health booleans when all subsystems are green:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "uptime_sec": 3847,
  "scheduler_running": true,
  "lancache_reachable": true,
  "cache_volume_mounted": true,
  "validator_healthy": true,
  "git_sha": "abc1234"
}
```

Returns 503 if any boolean is false (JQ3).

### 8.5 No external observability stack in MVP — amended 2026-09-19

No Sentry, no Datadog, no Grafana, no Prometheus. Post-MVP Prometheus endpoint considered if homelab monitoring stack materializes.

**Amendment: Uptime Kuma is now a dependency of the alerting path.** The original
rule was written against *metrics* stacks and still holds for those — nothing
scrapes, aggregates or dashboards this system. What changed is that several
failures proved undetectable from inside the application, because the thing that
failed was the thing that would have reported it:

- the 2026-07-31 mass deletion ran for nine days with nothing logged at all;
- a SteamPrefill outage ran 13 days green because the wrapper exited 0;
- the cache-catcher tripwire could not prove it was alive, only that it had not
  spoken.

The answer in every case is a **push** monitor on Uptime Kuma (CT 1057,
`10.100.23.57:3001`), because a push monitor treats **silence as DOWN**. That is
the property the application cannot provide about itself. Each scheduled job and
each watcher pushes on a wall clock; a dead process, a dead container or a dead
host all surface as red.

Two rules follow, both learned by breaking them:

1. **Bind every monitor to a notification.** Monitors 176–182 spent months
   turning red on a dashboard and telling nobody.
2. **One monitor answers exactly one question.** A monitor carrying two signals
   cannot say which one it means — the defect behind #326, #330 and #337. Where
   two facts exist, create two monitors: `lancache:cache-guard` (is the watcher
   alive) and `lancache:cache-eviction` (is cache loss happening) are deliberately
   separate for this reason.

See §8.7 for the current monitor inventory and `docs/deploy/live-configuration.md`
§3 for the operator procedure.

### 8.6 Secret redaction rules (TM-012 enforcement)

- Authorization header redacted in access logs: `Bearer <redacted>`.
- Semgrep rule rejects `log.*(password|refresh_token|auth_code|orchestrator_token)[^_]` patterns at CI.
- Exception handler strips locals-in-traceback (`structlog.processors.format_exc_info` without `with_locals=True`).
- Negative test per auth path: raise in the auth flow with the token in a local var; verify the ERROR log entry does NOT contain the token substring.

---

### 8.7 The keys_zone alarm (2026-09-19)

nginx OSS publishes **no** gauge for cache-index (`keys_zone`) occupancy: there is
no `http_api_module`, `stub_status` omits cache zones, and the error log stays
silent through the normal evict-to-fit path. `df` cannot see it either, because
the index is shared memory, not disk. The 2026-07-31 incident was eviction from a
~94 %-full index, invisible to every signal the system had.

The metric is therefore **derived**, by two stdlib-only instruments running as
daemon threads inside the existing `cache-catcher` container on the NAS:

| instrument | module | what it answers |
|---|---|---|
| leading gauge | `tools/cache_catcher/key_budget.py` + `key_budget_probe.py` | how full is the index, and how fast is it filling |
| lagging tripwire | `tools/cache_catcher/fanotify_guard.py` | is cache loss happening right now |

The gauge samples leaf directories (`listdir` only — `stat` runs at ~180 files/sec
on this NAS), scales the mean across all 65 536 leaves, and compares against the
**nearest real ceiling**, `min(zone_keys, ram_keys)`, naming which one binds. It
trips on a floor (0.75) **or** a projected horizon (90 days).

Four decision rules are load-bearing, and each encodes a failure already suffered:

- **An unreadable leaf is a read failure, never an empty one.** A denied read on
  a mode-`0700` directory is indistinguishable from an empty directory; treating
  them alike made the first design measurement come out **13× low**.
- **A sample that read nothing reports `unknown`, not zero.**
- **A flat or shrinking trend projects nothing.** A falling object count means
  eviction is already under way; extrapolating it answers "never", the most
  dangerous output the function could give. Exercised in production 2026-09-19.
- **Unknown never reads as safe** — a failed sample or unreadable ceiling pushes
  DOWN rather than staying silent.

Decision logic lives in a pure module because `fanotify_guard.py` calls
`ctypes.CDLL("libc.so.6")` at import and cannot be imported off the NAS — the same
constraint that produced `delete_actor.py`. Everything that can be arithmetically
wrong is therefore reachable by CI.

**Known limitation:** the configured zone (`CACHE_INDEX_SIZE=10000m`, 9.77 GiB
when full) exceeds what the host can supply alongside the agent's 8 GiB limit on
15.40 GiB of RAM — a 2.44 GiB deficit. Tracked as **#346**, deferred 2026-09-19
because the index sits at 48 % with flat-to-negative growth. The alarm measures
its ceiling from **live RAM**, not the configured number, so it stays correct
regardless.

## 9. Interface Specifications (CLI + REST + Status Page + Game_shelf contract)

<!-- Last Updated: 2026-10-16 -->

Full artifact: `docs/phase-1/interface-spec.md`. Summary here. **BL5 implements the REST API skeleton** (F9 partial); see ADR-0012 for design.

### 9.1 CLI (F11)

Click-based; entry point `orchestrator-cli`. Most groups talk to the API at `--url` (default `http://127.0.0.1:8765`). The exceptions are `db` (local admin with no API endpoint, by design) and `config show` (reads local settings). Commands, as `orchestrator-cli --help` lists them on 2026-10-16:

| Group | Subcommands |
|---|---|
| `auth` | `epic`, `status` (there is no `auth steam`; see §7.2) |
| `cache` | `fetch-manifests`, `validate-all` |
| `config` | `show` |
| `db` | `migrate`, `vacuum` (no `rollback`; down-migrations are out of scope, §5.4) |
| `game` | `list`, `show`, `validate`, `prefill` (`--force`), `purge`, `block`, `unblock`, each taking a numeric `GAME_ID` except `list` |
| `jobs` | (no subcommands) filtered by `--kind`, `--state`, `--limit` |
| `library` | `sync` |
| `selection` | `allow`, `classify`, `exclude`, `exclusions`, `unset` |
| `status` | (no subcommands) overall health and platform auth |

`game list` filters by `--platform`, `--status` and `--title`, a `contains` match, and pages with `--limit`/`--offset`.

Exit codes (`cli/client.py` plus Click):
- **0:** success.
- **1:** an error the API returned (e.g. 404), or an unexpected response.
- **2:** API unreachable, and also Click's own usage errors (unknown option, a non-integer `GAME_ID`).
- **3:** auth failure.

The Phase 1 codes 4 (upstream error) and 130 (SIGINT) are not implemented.

All interactive subcommands define four states (Empty/Loading/Error/Success). Output lines carry a glyph beside a text label (`cli/output.py`): `✓` for success, `⚠` for a warning, and `✗` for an error, written to stderr. Status cells read like `✓ UP_TO_DATE`.

Colorblind-safe: text prefixes are primary signal; color is additive.

### 9.2 REST API (F9)

FastAPI on `:8765`, mounted at `/api/v1/*`. Bearer-auth (timing-safe) is required on every route except those in `api/_constants.py::AUTH_EXEMPT_PATHS`:
- `/api/v1/health`;
- the status-page shell at `/`, whose data fetches are themselves bearer-gated;
- the schema and docs pages (`openapi.json`, `docs`, `redoc`), which are loopback-only instead.

`/api/v1/platforms/{name}/auth` additionally requires a loopback client (§7.3).

**25 routes as of 2026-10-16**, counted from the router modules: 24 under `/api/v1` plus the status page at `/`. The Phase 1 figure was 12, and the list in `docs/phase-1/interface-spec.md` §2.4 is the original spec, not the current surface. The running app's OpenAPI document (`/api/v1/openapi.json`, loopback-only) is the canonical list. The groups:
- health, platforms
- games: list, show, validate, prefill, purge
- block-list
- jobs
- manifests and `fetch-manifests`
- `sweep`
- Steam and Epic `library/sync`
- Epic `auth`
- `selection/candidates`
- `prefill-exclusions`
- `manual-downloads/{launcher}`

**Mutation responses carry no envelope.** The Phase 1 DQ6 envelope `{"ok", "job_id", "message"}` was never built; it exists only in `docs/phase-1/interface-spec.md`. The real shapes:
- **Job triggers** (validate, prefill, purge, library sync) return 202 `{"job_id": N}`. `fetch-manifests` adds `queued`, and `sweep` adds `full` and `queued`.
- **Block-list:** POST returns the entry (201 when new, 200 when it already existed), and DELETE returns `{"removed": n}`.
- **Prefill-exclusions** return the row or `{"deleted": n}`. The Game_shelf bulk PUT returns `{"platform", "added", "removed", "total"}`.
- **Epic auth** returns `{"account_id", "display_name"}`.

Request body size cap 32 KiB; Pydantic `extra='forbid'` on every model. Unknown fields → **400**, because `api/main.py` converts FastAPI's default 422 to 400.

OpenAPI 3.1 at `/api/v1/openapi.json`, Swagger UI at `/api/v1/docs` and ReDoc at `/api/v1/redoc`. All three are loopback-only and exempt from the bearer.

Latency SLOs (Intake §2.3):
- `/api/v1/health`: p99 < 50 ms idle, < 100 ms under Spike F load.
- `/api/v1/games` (full list): p99 < 500 ms at 2,200 games.
- `/api/v1/platforms`: p99 < 50 ms.
- `/api/v1/games/{game_id}`: p99 < 100 ms.
- `/api/v1/jobs`: p99 < 200 ms. (The Phase 1 `/api/v1/stats` endpoint does not exist in the current code.)

### 9.3 Status page (F10)

Single static HTML < 20 KB gzipped at `GET /`. Bearer token entered via `sessionStorage` + `prompt()` (FG1 tracks Post-MVP HTML-form replacement).

5 panels: Health, Platforms, Active Jobs, Stats, Recent Errors. Each has four states. Every status indicator uses color + icon + text label (Intake §9 colorblind-safe hard constraint).

Polling, per `api/routers/status.py`:
- **Every 2 s** (`POLL_FAST_MS`): `/health`, `/platforms`, `/jobs?state=queued&limit=10` and `/jobs?state=running&limit=10`.
- **Every 10 s** (`POLL_SLOW_MS`): Stats, built from `/games?limit=1` and `/manifests?limit=1`, and recent errors from `/jobs?state=failed&limit=5`.

There is no `/stats` endpoint. Back off to 10 s on 5xx until success.

### 9.4 Game_shelf contract (F14 consumer; Game_shelf repo owns implementation)

Orchestrator commits to:
- `/api/v1/*` endpoint stability within major version.
- Mutation response shapes (§9.2) stay stable within the major version. There is no envelope.
- Version + status always present on `/api/v1/health`.
- CORS default-deny, allowlist via the `ORCH_CORS_ORIGINS` env var.
- Bearer token rotation documented; no schema change on Game_shelf side.

Game_shelf commits to:
- Token lives in Express backend env only, never in frontend bundle (F17 CI grep invariant).
- Offline/degraded UX per F17 (single-shot retry, no storms).
- Tolerant merging of response shapes.

---

## 10. Coding Standards

<!-- Last Updated: 2026-09-07 -->

### 10.1 Formatting & linting

- **Ruff** for linting + formatting (replaces Black + isort + flake8 + pylint for this project). Ruff runs on every commit via pre-commit hook and on every CI run.
- Line length: 100.
- Quote style: double.
- Import ordering: stdlib → third-party → first-party (`orchestrator.*`) → local.
- Type hints required on every public function signature; `from __future__ import annotations` at top of every module.

### 10.2 Required static checks (CI-gated)

- **mypy** in `--strict` mode across `src/`.
- **Semgrep** with `p/owasp-top-ten` + `p/security-audit` + project-local custom rules (see below).
- **gitleaks** on every commit + on CI.
- **Snyk CLI** on every CI run (dependency vulnerability).
- **License compliance** via `pip-licenses` — only MIT/BSD/Apache-2.0/PSF/ISC/MPL-2.0 permitted; GPL/AGPL blocked.

### 10.3 Project-local custom Semgrep rules

Mandatory rules (failing CI on match):

| Rule ID | Pattern | Reason |
|---|---|---|
| `no-requests-on-main-loop` | `import requests` outside `adapters/steam/` executor paths | Threat-model TM-015; ADR-0001 discipline |
| `no-urllib-on-main-loop` | `import urllib.request` | Same |
| `no-sync-sqlite` | `import sqlite3` (must use `aiosqlite`) | ADR-0001 + DQ3 |
| `no-time-sleep-in-async` | `time.sleep(` in any `async def` | Main-loop blocker |
| `no-shell-true` | `subprocess.*(shell=True` | TM-021 mitigation |
| `no-credential-log` | `log.*(password\|refresh_token\|auth_code\|orchestrator_token)[^_]` | TM-012 |
| `no-f-string-sql` | `(execute\|executemany)\(f"` or `.execute("..." + ...)` | TM-005 |

### 10.4 Naming conventions

- Modules: `snake_case`, short.
- Classes: `PascalCase`.
- Functions: `snake_case`, verb-first for actions, noun-first for predicates.
- Constants: `UPPER_SNAKE_CASE`.
- Private: single leading underscore; never dunder for non-magic.
- Test functions: `test_<subject>_<condition>_<expected>` per QA-engineer-persona convention (Builder's Guide §2.2).

### 10.5 "Never do this" rules (stack-specific)

1. Never open a DB connection outside `db/` module.
2. Never call `time.sleep` in an `async def` — use `asyncio.sleep` or `loop.run_in_executor`.
3. Never reach Lancache by IP — use the compose service name `http://lancache:80` (ADR-0007 pending).
4. Never store credentials in the DB or in env vars; always Docker secret + in-memory.
5. Never commit a non-pinned requirement; always SHA-pinned / version-pinned with hash in requirements.txt.
6. Never use `--no-verify` on commits; CI-blocked.
7. Never stash uncommitted session-artifact files mixed with source changes (per established workflow pattern).
8. Never assume a platform adapter's protocol surface — always validate upstream response shape with Pydantic.
9. Never write `games.status` or `games.status_measured_at` outside `jobs/measurement.py::record_measurement()` — cache truth has exactly one writer, and `tests/test_measurement_writer_guard.py` fails the build on any other `UPDATE games SET … status =` / `DO UPDATE SET … status =`. How a job ended goes to `record_job_outcome()` (§5.7).

---

## 11. Build & Distribution Strategy

<!-- Last Updated: 2026-04-20 -->

### 11.1 Container image

**Multi-stage Dockerfile**:
- Stage 1 (`builder`): `python:3.12-slim` + build-essential + git. Installs pinned `requirements.txt` + clones vendored git deps (`fabieu/steam-next` at pinned SHA, `legendary-gl/legendary` at pinned SHA, copied into `vendor/legendary/`).
- Stage 2 (`runtime`): `python:3.12-slim`. Copies `/app/.venv` + application code + CLI entrypoint. Installs `orchestrator-cli` on `$PATH`.
- Runs as UID 1000 (`orchestrator` user); read-only root filesystem; tmpfs for `/tmp`; `cap_drop: ALL`; `no-new-privileges:true`.

### 11.2 Tagging & versioning

- Semantic versioning: `vMAJOR.MINOR.PATCH`.
- Images published to `ghcr.io/kraulerson/lancache-orchestrator:{semver}` + `:latest`.
- `git_sha` baked into image via build arg; surfaced at `/api/v1/health`.
- GitHub Actions tag-triggered build → push → release notes.

### 11.3 CI/CD pipeline (`.github/workflows/ci.yml` — Phase 2 construction)

Jobs on every PR:
- `lint` — Ruff + mypy strict.
- `test` — pytest + pytest-asyncio; unit + integration; coverage target ≥ 80% for core logic.
- `sast` — Semgrep with OWASP + security-audit + custom rules.
- `secrets` — gitleaks.
- `deps` — Snyk CLI (`snyk test --severity-threshold=high`).
- `licenses` — `pip-licenses` with allowlist.
- `build` — build image, verify size < 250 MB, run container, hit `/api/v1/health`, `docker run --rm orchestrator-cli --help` returns 0.

On tag push:
- `publish` — build + push to `ghcr.io` with semver + latest tags.
- `release-notes` — generated from commits since last tag.

### 11.4 Distribution to deployer (Karl)

Manual pull: `docker compose pull && docker compose up -d` on DXP4800.

**No auto-update in MVP.** Post-MVP Watchtower / Diun consideration. Version is surfaced at `/api/v1/health` and the status page so Karl can see what's running.

### 11.5 Dependency update policy

- **Dependabot** enabled for Python deps + GitHub Actions. Weekly PRs.
- **Snyk** fails CI on new known-high CVE in any pinned dep.
- **steam-next SHA policy (OQ4):** weekly CI job (or scheduled Claude Code agent) checks `fabieu/steam-next` commit history. If no new commits for >15 days, emit a CI warning + ping the Orchestrator; if >30 days, file an issue in our repo labeled `steam-next-fork-trigger`. Fork procedure documented in ADR-0002 (Phase 2).

### 11.6 Platform-specific build notes

- **Target host is ARM64 (DXP4800).** Multi-arch image build (`docker buildx build --platform linux/arm64,linux/amd64`) in CI so x86 Mac dev environments can run the container locally.
- Base image `python:3.12-slim` is multi-arch; no special adjustments needed.

---

## 12. Test Strategy

<!-- Last Updated: 2026-04-20 -->

### 12.1 Test categories and tools

| Category | Tool | Scope | CI-gated? | Pass criteria |
|---|---|---|---|---|
| Unit | pytest | Pure logic (cache-key computation, diff, manifest dedup, Pydantic models, CLI parsing) | Yes | 100% pass; coverage ≥ 80% on `src/core/`, `src/adapters/`, `src/validator/` |
| Integration | pytest + httpx test client | DB + API handlers + mocked Steam/Epic adapters | Yes | 100% pass; all endpoint four-states exercised |
| Security (SAST) | Semgrep + custom rules | Static source scan | Yes | 0 high/critical findings |
| Secrets | gitleaks | Secret scan on every commit + CI | Yes | 0 findings |
| Dependency | Snyk CLI | Known CVE in pinned deps | Yes | 0 high/critical findings |
| License | pip-licenses | License compliance | Yes | All deps within allowlist |
| Build | Docker build | Image builds; size < 250 MB; container starts; `/health` returns 200 | Yes | Pass |
| Spike F (Performance) | Custom load harness | 32-concurrent chunk downloads, `/health` p99 < 100 ms | **Yes — hard gate before Milestone B** | p99 < 100 ms |
| UAT (manual) | Human tester (Karl) | Full user-journey scenarios | After every 2 features (Intake §11.5) | No SEV-1 open; no SEV-2 open/deferred at Phase 2→3 gate |
| Accessibility | Manual colorblind-sim in Chrome devtools + grayscale screenshot review | F10 status page + F15/F16 Game_shelf components | Phase 3.4 | All status indicators distinguishable without color |
| Chaos / Resilience | Manual (Phase 3.3) | Container restart mid-prefill; Lancache down; cache volume unmounted | Phase 3.3 | Startup reaper + degraded-mode health surfacing work |

### 12.2 Bug severity classification

| Severity | Definition | Examples |
|---|---|---|
| SEV-1 | Data loss, security breach, container crash on core flow, complete feature failure | Token leak, auth bypass, container OOM on prefill, orchestrator can't start |
| SEV-2 | Feature broken but workaround exists, significant UX failure | Prefill succeeds but validation reports wrong count, status page wrong on specific state, colorblind violation |
| SEV-3 | Minor UX issue, cosmetic, non-core edge case | CLI output alignment off, log line says "epic" instead of "Epic" |
| SEV-4 | Enhancement, suggestion, polish | "Would be nice if CLI supported bash completion" |

**Deferral rules** (Builder's Guide §2.7):
- SEV-1 cannot be deferred.
- SEV-2 can be deferred within a Phase 2 batch but must be resolved OR the feature removed/hidden at the Phase 2→3 gate.
- SEV-3, SEV-4 can be deferred freely with documented rationale.

### 12.3 UAT plan (Intake §11.5)

- **Interval:** every 2 features.
- **Bug tracker:** GitHub Issues in this repo.
- **Tester count:** 1 (Karl).
- **SLAs:** SEV-1 24h / SEV-2 7d / SEV-3 best effort.
- **Session template:** HTML preferred (`templates/uat/templates/test-session-template.html`), Markdown fallback.
- **Session location:** `tests/uat/sessions/<date>-session-N/templates/` + `submissions/` + `agent-results/`.
- **Archival:** on completion + review, archived to `docs/test-results/[date]_uat-session-N-vX.html`.

### 12.4 Entry criteria for Phase 3

All items in `PRODUCT_MANIFESTO.md` §5 MVP Cutline (F1–F17 + ID1–ID10 foundations) complete; full test suite passes; CI green; no open SEV-1/SEV-2 bugs; application builds on target ARM64 + x86_64.

### 12.5 Exit criteria for Phase 3 (= entry criteria for Phase 4)

Phase 3 checklist per `CLAUDE.md` §Phase 3-4 Documentation: integration testing, security hardening, chaos testing, accessibility audit, performance audit, contract testing, results archived to `docs/test-results/`. SECURITY.md generated. SBOM (`sbom.json`) at project root.

### 12.6 Test result storage

- CI artifacts: `tests/reports/{coverage,pytest,semgrep,snyk,licenses}/`.
- Phase 3 scan outputs: `docs/test-results/[date]_[scan-type]_[pass|fail].[ext]` per Builder's Guide.
- Spike F result: `docs/test-results/[date]_spike-f_pass|fail.md`.
- UAT sessions: `docs/test-results/[date]_uat-session-N-vX.html` (archived).

---

## 13. Orchestrator Profile Summary

<!-- Last Updated: 2026-04-20 -->

Per Intake §6.2, Manifesto Appendix B, and Interface Spec §5.

### 13.1 Orchestrator competencies

| Domain | Self-assessment | Validation approach |
|---|---|---|
| Product / UX Logic | **Yes** | Manual review + Skeptical PM pass (Phase 0 complete) |
| Frontend / UI | **Yes** | Existing Game_shelf React code patterns inform review |
| Backend / API | **Yes** | pytest + httpx test client, CI-enforced |
| Database (SQLite) | **Yes** | Migration dry-run + schema diff on PR CI |
| Security | **Partially** | **Mandatory:** Semgrep + gitleaks + Snyk CLI, CI-gated |
| Build & Packaging | **Yes** | Multi-arch CI builds; release smoke test in throwaway container |
| Accessibility | **Partially** | Manual colorblind-sim review (no WCAG AA target for a single-operator diagnostics page) |
| Performance | **Partially** | **Mandatory:** Spike F load test (hard gate before Milestone B) |
| Platform-Specific | N/A | Single target Linux Docker ARM64/x86_64 |

### 13.2 Known gaps accepted

- Accessibility only partially tool-gated — manual review baseline. If Phase 2 UAT reveals specific colorblind violations, they become Phase 3 remediation bugs.
- Security is Partially — if a SEV-1 security bug is discovered in Phase 2 that Semgrep + Snyk didn't catch, the Orchestrator reviews and approves remediation; does not become a blocker for Phase 1→2 gate.

---

## 14. Accessibility Requirements

<!-- Last Updated: 2026-04-20 -->

### 14.1 Intake §9 (hard constraint)

**Operator is colorblind.** The orchestrator must never rely on color alone for meaning. Every status indicator must use **color + icon + text label** — three distinct signals.

### 14.2 Scope

Applies to:
1. Status page (F10) — every panel's indicator (Health, Platforms, Active Jobs, Stats, Recent Errors).
2. CLI output (F11) — `[OK]`/`[WARN]`/`[ERROR]`/`[INFO]` text prefixes as primary signal; color additive.
3. Game_shelf `CacheBadge.jsx` (F15) — 7 states each with color + icon + text label.
4. Game_shelf `CachePanel.jsx` + `Cache.jsx` (F15, F16) — platform status cards, error banners.

### 14.3 Not in scope (Light track)

- No WCAG AA conformance target for the orchestrator's own diagnostic surfaces.
- No formal screen-reader testing; the status page's simple document order is sufficient for its single-operator purpose.
- No keyboard-only navigation testing beyond natural tab order.

### 14.4 Verification strategy

- **Unit tests:** every `.status-indicator` HTML element must contain a text node matching an allowed label (`[OK]`, `[WARN]`, `[EXPIRED]`, etc.) and an `aria-label` attribute. Test asserts presence on every state.
- **Grayscale screenshot review** during Phase 3.4 — every state's screenshot remains distinguishable when converted to grayscale.
- **Chrome devtools Deuteranopia simulation** during Phase 3.4 — operator verifies each state.
- **CLI output strip-ANSI test** — ANSI codes stripped (e.g., via `| sed`) → text is still legible and meaning preserved.

### 14.5 If Phase 2 UAT reveals a violation

Any UAT finding of color-only reliance is automatically SEV-2 (significant UX failure per §12.2). Cannot be deferred past the Phase 2→3 gate.

---

## 15. Platform-Specific Requirements

<!-- Last Updated: 2026-04-20 -->

**N/A — no Platform Module.** Intake §1 declared platform type "other" and Platform Module "None". The orchestrator runs as a Docker container on Linux ARM64 (primary) + Linux x86_64 (secondary for dev). No desktop, no mobile, no web-browser-hosted frontend in the orchestrator repo. All platform-specific concerns for Game_shelf integration are the Game_shelf repo's scope.

What would appear here for a non-N/A project: OS version matrix, native API references, platform store certification requirements, etc.

Recorded per Builder's Guide §1.6 completeness requirement.

---

## 16. Context Management Plan

<!-- Last Updated: 2026-04-23 -->

Per Builder's Guide §1.6 item 16: small / medium / large project categorization.

**This project is small.** MVP file count projection:

- `src/` — 25–35 Python modules (api, adapters/steam, adapters/epic, core, db, validator, cli, status)
- `src/orchestrator/db/migrations/` — 1–2 SQL files at MVP, shipped as package-data
- `vendor/legendary/` — ~5 files vendored
- `tests/` — mirrors `src/` structure, ~30–40 test modules
- `docs/` — ADRs, Phase 0/1 intermediates, interface docs, guides
- `templates/`, `scripts/`, CI config — framework-provided
- `frontend/` — **none in this repo** (Game_shelf lives elsewhere)

Total: < 60 Python source files.

### 16.1 Context strategy for Phase 2+ sessions

- **Full Bible per session** — this document is loaded at the start of every Phase 2 Build Loop session.
- **Companion intermediate artifacts** loaded on demand:
  - Phase 0: `docs/phase-0/*.md` when product-level context is needed.
  - Phase 1: `docs/phase-1/*.md` when architectural context is needed.
- **Last 3–4 active feature files** loaded at session start per Builder's Guide Context Health Check guidance.

### 16.2 Context Health Check (every 3–4 features)

Per CLAUDE.md — ask the agent to summarize features built, features remaining, current data model, known issues. Compare against this Bible. If the summary contradicts the Bible, start a fresh session.

### 16.3 Bible update protocol during Phase 2

- After every feature's documentation step (Builder's Guide §2.5), the Bible is updated to reflect new interfaces, schema changes, or new dependencies.
- The `<!-- Last Updated: ... -->` marker (ISO-8601 date) on each section is refreshed on every modification.
- Cross-section consistency verified after every update.

### 16.4 Qdrant memory

If Qdrant MCP remains available in Phase 2 sessions, persist:
- Architecture decisions after finalization.
- Debugging breakthroughs with root cause + fix.
- Phase gate transitions with accomplishments + lessons.
- Trade-off discussions with context + reasoning.
- Integration patterns after establishing connection architecture.

Recall at session start: `qdrant-find` for current work area before diving in (per `CLAUDE.md`).

### 16.5 CHANGELOG and FEATURES discipline

- Every feature's Build Loop Step 2.5 updates `CHANGELOG.md` (8 categories per Keep-a-Changelog).
- Every feature's Build Loop Step 2.5 adds a new entry to `FEATURES.md`.
- Every non-trivial decision during Phase 2 creates an ADR (`docs/ADR documentation/NNNN_*.md`).
- Periodic Bible check (§16.2) catches drift.

---

## Appendix: Review Checklist (Builder's Guide §1.6 gate)

- [x] Section 1 — Product Manifesto: ✅ embedded
- [x] Section 2 — Cost / Revenue: ✅ skipped with rationale
- [x] Section 3 — ADR: ✅ ADR-0001 referenced + sub-ADR list
- [x] Section 4 — Threat Model: ✅ 23 TMs summarized
- [x] Section 5 — Data Model: ✅ Brief §5 schema + DQ decisions
- [x] Section 6 — Data Migration: ✅ N/A recorded
- [x] Section 7 — Auth: ✅ two-boundary model
- [x] Section 8 — Observability: ✅ structlog + correlation IDs + log retention
- [x] Section 9 — Interface Spec: ✅ CLI + REST + status page + Game_shelf contract
- [x] Section 10 — Coding Standards: ✅ Ruff + mypy + Semgrep custom rules + never-do-this
- [x] Section 11 — Build & Distribution: ✅ Dockerfile + CI + multi-arch + distribution
- [x] Section 12 — Test Strategy: ✅ categories + bug-severity + UAT plan
- [x] Section 13 — Orchestrator Profile: ✅ competency matrix
- [x] Section 14 — Accessibility: ✅ colorblind-safe invariant + verification
- [x] Section 15 — Platform-Specific: ✅ N/A recorded
- [x] Section 16 — Context Management: ✅ full-Bible strategy + Qdrant

**16 of 16 sections present; gate requires minimum 14.** No unresolved placeholder date strings. Ready for Phase 1→2 gate.

---

## Sign-off

**Decision gate: Orchestrator reviews the complete Bible.** This is the point of no return for Phase 1 — from here, Phase 2 features are built against this document. Any deviation requires a superseding ADR.

**On approval:**
1. `APPROVAL_LOG.md` Phase 1 → Phase 2 row populated (self-review).
2. `.claude/phase-state.json` updated: `current_phase = 2`, `gates.phase_1_to_2 = <approval date>`.
3. Both files committed together per governance protocol.
4. Phase 2 Project Initialization begins (Builder's Guide §2 Project Initialization).

**Self-review risk acknowledgment (Builder's Guide §1.6 note):** for Standard+ track personal projects, external architecture review via a peer or fresh Claude session with the adversarial evaluation prompt is recommended. This is a **Light track** project; self-review is accepted. If promoted to organizational deployment via `upgrade-project.sh`, the Senior Technical Authority retroactively reviews and approves this Bible.
