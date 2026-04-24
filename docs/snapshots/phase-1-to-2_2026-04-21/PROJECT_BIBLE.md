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

<!-- Last Updated: 2026-04-20 -->

### 3.1 Selected architecture — Option A (single-container monolith)

Accepted in `docs/ADR documentation/0001-orchestrator-architecture.md` on 2026-04-20. Full text of ADR-0001 referenced there; summary:

**Single `uvicorn` process** running FastAPI + APScheduler + all adapters + validator, organized into **three cleanly separated work zones** per Brief §3.6:

1. **Main asyncio event loop** — FastAPI handlers, `httpx.AsyncClient` chunk fan-out (`aiter_raw` + 32-way `asyncio.Semaphore`), `aiosqlite` queries, APScheduler triggers. Async-native only.
2. **Dedicated gevent-patched worker thread** — `steam-next` `SteamClient` lives in a single long-lived thread. Gevent monkey-patching is restricted to that thread. Main loop reaches it only via `loop.run_in_executor(steam_thread_pool, fn)`.
3. **Default `ThreadPoolExecutor`** — disk-stat bursts + MD5 cache-key computation (F7 validator, F13 sweep) batched in 256-file chunks via `run_in_executor(None, ...)`.

**Rejected alternatives:**
- **Option B (subprocess-isolated downloader)** — retained as the pre-documented fallback if Spike F fails. A superseding ADR-0005 will record the Spike F outcome.
- **Option C (multi-container split)** — rejected outright; violates Intake §6.4 hard constraint.

**Spike F is the gate** between Option A and Option B. Passes: p99 `/api/v1/health` < 100 ms under 32 concurrent chunk downloads sustaining ≥ 300 Mbps aggregate for 10 minutes on real DXP4800 hardware.

### 3.2 Planned sub-ADRs (Phase 2)

Not yet issued; scheduled for Phase 2 construction:

- **ADR-0002** — steam-next SHA pin + 15-day fork-trigger policy (OQ4).
- **ADR-0003** — `MemoryJobStore` over `SQLAlchemyJobStore`.
- **ADR-0004** — Raw SQL + numbered migrations, no ORM (DQ2–DQ8 consolidated).
- **ADR-0005** — Spike F result → A vs B final commitment.
- **ADR-0006** — Vendored `legendary` subset vs PyPI.
- **ADR-0007** — Lancache reached via compose service name (`http://lancache:80`).

### 3.3 Stack (exact dependencies to pin in Phase 2)

| Layer | Choice | Pinned via |
|---|---|---|
| Language | Python 3.12 | Base image `python:3.12-slim` |
| Web framework | FastAPI (latest stable at Phase 2 init; verify via Context7) | `requirements.txt` with hash |
| ASGI server | `uvicorn[standard]` (uvloop + httptools) | `requirements.txt` with hash |
| HTTP client | `httpx[http2]` | `requirements.txt` with hash |
| DB driver | `aiosqlite` | `requirements.txt` with hash |
| Scheduler | `APScheduler` 3.x with `MemoryJobStore` | `requirements.txt` with hash |
| Logging | `structlog` | `requirements.txt` with hash |
| Config | `pydantic` v2 + `pydantic-settings` | `requirements.txt` with hash |
| CLI | `click` | `requirements.txt` with hash |
| Steam protocol | `fabieu/steam-next` (git SHA) | pip install `git+https://github.com/fabieu/steam-next@<SHA>` |
| Epic protocol | Vendored subset of `legendary-gl/legendary` | `vendor/legendary/` in repo with SHA recorded in `VENDORED.md` |

### 3.4 Process topology

Single container. Single process. Three work zones inside that process (above). No sidecars, no broker, no worker pool. No Celery/Redis. Container image target < 250 MB.

### 3.5 Deployment topology

Docker Compose stack on DXP4800 alongside Lancache:

```yaml
services:
  orchestrator:
    image: ghcr.io/kraulerson/lancache-orchestrator:<semver>
    restart: unless-stopped
    ports: ["8765:8765"]
    volumes:
      - monolithic-cache:/data/cache:ro
      - monolithic-logs:/data/logs:ro
      - orchestrator-state:/var/lib/orchestrator
    environment: [...]
    depends_on: [lancache]
    secrets: [orchestrator_token]
    user: orchestrator      # non-root per Phase 2 Dockerfile
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
```

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

<!-- Last Updated: 2026-04-20 -->

Full artifact: `docs/phase-1/data-model.md`. Canonical SQL for `migrations/0001_initial.sql` in §7 of that file.

### 5.1 Entity inventory

Seven entity tables + one meta table:

- `platforms` — enum-like with 2 rows (seeded by 0001).
- `games` — one row per `(platform, app_id)`.
- `manifests` — one row per `(game_id, version)`, `raw BLOB` compressed.
- `block_list` — independent; no FK to games (allows pre-blocking unknown app_ids).
- `validation_history` — one row per F7 run.
- `jobs` — one row per enqueued operation (prefill / validate / library_sync / auth_refresh / sweep).
- `cache_observations` — populated only when Post-MVP access-log tail ships (DQ2 — schema ships in 0001).
- `schema_migrations` — migration runner meta; created by runner if missing.

### 5.2 Key constraints

- `games.platform REFERENCES platforms(name) ON DELETE RESTRICT` (DQ8).
- `manifests.game_id REFERENCES games(id) ON DELETE CASCADE`.
- `validation_history.game_id REFERENCES games(id) ON DELETE CASCADE`.
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

Numbered `.sql` files in `migrations/`: `NNNN_snake_case_description.sql` (+ optional `NNNN_..._down.sql` for rollback). Applied atomically per-file. Runner (~50 LoC) checksums every applied migration; **content drift aborts startup** (CRITICAL `migration_content_drift`). Schema-version-ahead-of-code aborts with `schema_version_ahead`.

### 5.5 Retention policy

- `validation_history`: 90-day prune, daily.
- `jobs`: 90-day prune for non-error rows, daily. Error rows kept indefinitely.
- `manifests`: keep latest 3 versions per game, weekly prune.
- `cache_observations`: 30-day prune, weekly (no-op in MVP).
- `platforms`, `games`, `block_list`: never auto-pruned.

### 5.6 Concurrency model

Single-process, single-writer-lock application-side. `aiosqlite` connection pool size 10. WAL handles reader concurrency; application `asyncio.Lock` serializes all writes to eliminate SQLITE_BUSY under F12×F13 overlap (threat-model §4.3.2 mitigation).

---

## 6. Data Migration Plan

<!-- Last Updated: 2026-04-20 -->

**N/A — no legacy data.**

The orchestrator replaces SteamPrefill/EpicPrefill functionally but does NOT import their state. SteamPrefill's flat-file tracker is the root cause of the problem this project solves (it drifts from actual cache state, reports false positives). Its contents are not authoritative and are not imported.

On first deployment, the orchestrator's initial state is: empty DB, no sessions, no cached_version data. First library sync cycle + first post-prefill F7 validations establish ground truth from platform APIs + disk-stat. This is intentional — the whole point is that ground truth comes from disk, not from prior-tool logs.

Recorded per Builder's Guide §1.4.5 requirement.

---

## 7. Auth & Identity Strategy

<!-- Last Updated: 2026-04-20 -->

### 7.1 Two auth boundaries

The orchestrator authenticates across two unrelated boundaries:

1. **Platform auth** (Steam + Epic) — the orchestrator ACT AS the operator to their Steam and Epic accounts to enumerate libraries and download content via Lancache.
2. **API auth** (consumers → orchestrator) — CLI, status page, and Game_shelf authenticate TO the orchestrator with a static bearer token.

### 7.2 Platform auth (per F1, F2)

**Steam (Steam CM):**
- Interactive login via CLI (`orchestrator-cli auth steam`): username + password + Steam Guard code.
- 2FA type disambiguation (JQ1 conditional) — if `steam-next` exposes the challenge type, CLI prompt discriminates mobile-authenticator vs email-code.
- Refresh token persisted at `/var/lib/orchestrator/steam_session.json` mode 0600.
- Silent reconnect at every container start; no re-login required while the token is valid.
- Steam sends a "new device login" email on first auth — CLI warns operator; README documents.

**Epic (Epic OAuth):**
- Interactive auth-code exchange via CLI (`orchestrator-cli auth epic`): user pastes code from `https://legendary.gl/epiclogin`.
- Access + refresh tokens persisted at `/var/lib/orchestrator/epic_session.json` mode 0600.
- Silent refresh with 10-minute pre-expiry buffer; no ongoing user action required.

**Credential handling rules** (threat-model TM-004, TM-012):
- Credentials enter via CLI stdin only — never logged, never written to DB, never in env vars.
- Refresh tokens logged as `token_sha256_prefix=<first 8 hex of SHA256>`, never raw.
- On auth failure, no session file is written (prevents corrupted-state carryover).
- Session files world-readable check at startup logs WARN if mode drift detected.

### 7.3 API auth (per F9)

- Single static bearer token loaded from Docker secret `/run/secrets/orchestrator_token`.
- Stripped of whitespace, minimum 32 characters.
- Timing-safe comparison via `hmac.compare_digest` (threat-model TM-001).
- **Missing secret at startup → container refuses to start** (CRITICAL + exit 1).
- `POST /api/v1/platforms/{name}/auth` requires `request.client.host == '127.0.0.1'` in addition to the bearer (OQ2 / TM-023 mitigation).

**Rotation procedure** (documented in Phase 4 HANDOFF.md):
1. Generate new token (`openssl rand -hex 32`).
2. Update Docker secret on DXP4800 (`docker secret create` or file-based).
3. Update `ORCHESTRATOR_TOKEN` env on Game_shelf LXC.
4. Restart both services.
5. Stale tokens return 401 until restart completes.

### 7.4 Explicit non-goals

- No user accounts, no sessions, no OAuth, no SSO, no per-endpoint scopes.
- No TLS for the orchestrator itself (LAN-only; pfSense is the perimeter). TLS between Game_shelf and orchestrator is deferred to post-MVP (Brief Appendix C).

---

## 8. Observability & Logging Strategy

<!-- Last Updated: 2026-04-20 -->

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

### 8.5 No external observability stack in MVP

No Sentry, no Datadog, no Grafana, no Prometheus. Post-MVP Prometheus endpoint considered if homelab monitoring stack materializes.

### 8.6 Secret redaction rules (TM-012 enforcement)

- Authorization header redacted in access logs: `Bearer <redacted>`.
- Semgrep rule rejects `log.*(password|refresh_token|auth_code|orchestrator_token)[^_]` patterns at CI.
- Exception handler strips locals-in-traceback (`structlog.processors.format_exc_info` without `with_locals=True`).
- Negative test per auth path: raise in the auth flow with the token in a local var; verify the ERROR log entry does NOT contain the token substring.

---

## 9. Interface Specifications (CLI + REST + Status Page + Game_shelf contract)

<!-- Last Updated: 2026-04-20 -->

Full artifact: `docs/phase-1/interface-spec.md`. Summary here.

### 9.1 CLI (F11)

Click-based. Commands: `auth steam|epic|status`, `library sync`, `game <p/a> [show|validate|prefill|block|unblock]`, `jobs [list|--active|<id>]`, `db migrate|rollback|vacuum`, `config show`.

Exit codes: 0 success, 1 user error, 2 environment error, 3 auth error, 4 upstream error, 130 SIGINT.

All interactive subcommands define four states (Empty/Loading/Error/Success). Non-interactive subcommands use `[OK]` / `[ERROR]` text-prefix output.

Colorblind-safe: text prefixes are primary signal; color is additive.

### 9.2 REST API (F9)

FastAPI on `:8765`, mounted at `/api/v1/*`. Bearer-auth (timing-safe) on every endpoint except `/api/v1/health`. `POST /api/v1/platforms/{name}/auth` additionally requires 127.0.0.1 origin.

12 endpoints enumerated in `docs/phase-1/interface-spec.md` §2.4 with four-state behavior per endpoint. Mutation responses use the normalized envelope `{"ok", "job_id", "message"}` (DQ6).

Request body size cap 32 KiB; Pydantic `extra='forbid'` on every model; unknown fields → 422.

OpenAPI 3.1 auto-generated at `/api/v1/openapi.json`; Swagger UI at `/api/v1/docs` (bearer-authenticated).

Latency SLOs (Intake §2.3):
- `/api/v1/health`: p99 < 50 ms idle, < 100 ms under Spike F load.
- `/api/v1/games` (full list): p99 < 500 ms at 2,200 games.
- `/api/v1/platforms`: p99 < 50 ms.
- `/api/v1/games/{p}/{a}`: p99 < 100 ms.
- `/api/v1/jobs`, `/api/v1/stats`: p99 < 200 ms.

### 9.3 Status page (F10)

Single static HTML < 20 KB gzipped at `GET /`. Bearer token entered via `sessionStorage` + `prompt()` (FG1 tracks Post-MVP HTML-form replacement).

5 panels: Health, Platforms, Active Jobs, Stats, Recent Errors. Each has four states. Every status indicator uses color + icon + text label (Intake §9 colorblind-safe hard constraint).

Polling: `/health`, `/platforms`, `/jobs?active` every 2 s; `/stats`, recent errors every 10 s. Back off to 10 s on 5xx until success.

### 9.4 Game_shelf contract (F14 consumer; Game_shelf repo owns implementation)

Orchestrator commits to:
- `/api/v1/*` endpoint stability within major version.
- Mutation envelope stability.
- Version + status always present on `/api/v1/health`.
- CORS default-deny, allowlist via `CORS_ORIGINS` env.
- Bearer token rotation documented; no schema change on Game_shelf side.

Game_shelf commits to:
- Token lives in Express backend env only, never in frontend bundle (F17 CI grep invariant).
- Offline/degraded UX per F17 (single-shot retry, no storms).
- Tolerant merging of response shapes.

---

## 10. Coding Standards

<!-- Last Updated: 2026-04-20 -->

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

<!-- Last Updated: 2026-04-20 -->

Per Builder's Guide §1.6 item 16: small / medium / large project categorization.

**This project is small.** MVP file count projection:

- `src/` — 25–35 Python modules (api, adapters/steam, adapters/epic, core, db, validator, cli, status)
- `migrations/` — 1–2 SQL files at MVP
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
