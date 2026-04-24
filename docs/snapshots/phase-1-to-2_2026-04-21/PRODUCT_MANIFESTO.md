# Product Manifesto — lancache_orchestrator

<!--
  This document is the foundational artifact produced during Phase 0.
  It defines what the product does, who it serves, and what is in/out of scope.
  It is the north star for all subsequent phases.
  Completion gates entry to Phase 1. All 8 numbered sections are filled.
  Appendices are track-conditional — A and C skipped for Light track.
-->

**Status:** Draft — pending Orchestrator approval
**Approved By:** Karl (self-review; single-operator Light track)
**Approval Date:** _(set when APPROVAL_LOG.md Phase 0 → Phase 1 entry is written)_
**Phase Gate:** Phase 0 → Phase 1
**Track:** Light
**Deployment:** Personal
**Companion artifacts:**
- Functional Requirements: `docs/phase-0/frd.md` (17 Must-Haves expanded with triggers, failures, acceptance)
- User Journey Map: `docs/phase-0/user-journey.md` (Skeptical PM review, 8 entry points, 8 exit points)
- Data Contract: `docs/phase-0/data-contract.md` (6 input surfaces, 12 transformations, 11 outputs)
- Technical Brief: `lancache-orchestrator-brief.md` revision 3 (architecture decisions, library evaluation, risk register)
- Project Intake: `PROJECT_INTAKE.md` (governing constraint)

---

## 1. Product Intent

A fully autonomous Python service running on a DXP4800 NAS alongside Lancache. It owns its own SQLite database, APScheduler cron, per-platform authentication (Steam CM + Epic OAuth), and a FastAPI REST API on port 8765. Its job is to proactively fill the Lancache nginx cache with owned Steam and Epic games, **validate cache state by reading the nginx cache directory from disk rather than trusting a flat-file log** (the core value proposition — addresses the reliability gap in SteamPrefill/EpicPrefill), and expose current state to operators via CLI, a single-file HTML status page, and the REST API consumed by Game_shelf as the rich UI. The orchestrator has **zero runtime dependency on Game_shelf** — if Game_shelf is offline, caching continues unchanged. The rich UI is intentionally hosted in Game_shelf (existing system) rather than duplicated in a second React SPA.

Architecture that contradicts this statement is rejected. Features not serving this intent are not built.

---

## 2. Functional Requirements

<!-- Full detail in docs/phase-0/frd.md. Summary form here. -->

### Must-Have (MVP) — 17 features

- **F1. Steam CM Authentication:** If no valid Steam session at startup or on `orchestrator-cli auth steam` → prompt for creds + Steam Guard, persist refresh token at mode 0600, silent re-connect on subsequent starts. **Failure state:** mark `platforms.steam.auth_status='expired'`, continue operating for Epic, surface actionable reconnect instruction on status page + Game_shelf Cache page.
- **F2. Epic OAuth Authentication:** If no valid Epic session or on `orchestrator-cli auth epic` → exchange auth code from `https://legendary.gl/epiclogin`, persist tokens, rotate silently with a 10-min pre-expiry buffer. **Failure state:** mark `expired`, continue operating for Steam, same surfacing.
- **F3. Steam Library Enumeration:** Every `SCHEDULE_CRON` (default 6h) or on CLI → authenticate, pull PICS, upsert `games`. **Failure state:** API unavailable → log + retry next cycle; partial response → process returned, warn.
- **F4. Epic Library Enumeration:** Same cadence as F3, with bulk catalog-title resolution chain. **Failure state:** partial title resolution → `epic:<id>` sentinel, retry next cycle.
- **F5. Steam CDN Prefill:** If `current_version != cached_version` and not blocked → fetch manifest, dedupe chunks, HTTP fan-out through Lancache with `Host:` override + stream-discard, concurrency-bound at 32 chunks. **Failure state:** 403 (manifest-code expiry) → refresh + retry; 508 (Lancache loop-detect) → fatal config; persistent chunk error → mark job failed, retry next cycle.
- **F6. Epic CDN Prefill:** Same pattern as F5 using vendored legendary modules + pinned preferred CDN host. **Failure state:** manifest URL expiry → re-fetch; v22 decrypt error → surface distinctly.
- **F7. Cache Validator (disk-stat):** On prefill completion OR API/CLI trigger → compute cache keys (Steam: `"steam" + uri + slice_range`; Epic: `http_host + uri + slice_range`), MD5, `os.stat()` the computed path at `/data/cache/cache/<H[28:30]>/<H[30:32]>/<H>`. **Failure state:** startup self-test fails → validator unhealthy, `/api/health` 503; cache volume unmounted → all validations `error`; formula drift → loud CRITICAL.
- **F8. Block List:** If added via API or CLI → skip during scheduled prefill; manual validation still runs on blocked games; idempotent. **Failure state:** block on unknown `(platform, app_id)` accepted (pre-block OK).
- **F9. REST API (FastAPI on :8765):** Bearer-auth via Docker secret on every non-health endpoint; timing-safe comparison; `POST /api/v1/platforms/{name}/auth` additionally requires `127.0.0.1` origin. **Failure state:** 401 uniform (no enumeration leakage); 400 on malformed body with Pydantic detail; 500 with correlation ID in response + full stack in logs; missing secret → container refuses start.
- **F10. Status Page (single-file HTML at `GET /`):** Polls API every 2 s via fetch(); browser `prompt()` for bearer token stored in `sessionStorage`. **Failure state:** API unreachable banner with Retry. **Hard constraint:** colorblind-safe (color + icon + text label on every status indicator).
- **F11. CLI (`orchestrator-cli`, Click-based):** Bundled in the same container; hits local REST API with the same bearer token. Subcommands: `auth steam|epic|status`, `library sync`, `game`, `jobs`, `db migrate|vacuum`, `config show`. **Failure state:** API down → exit 2; auth mismatch → exit 3. `--json` deferred to Post-MVP (OQ6).
- **F12. Scheduled Sync Cycle:** APScheduler `MemoryJobStore` fires `SCHEDULE_CRON` → Steam then Epic serial: refresh auth → enumerate → diff → prefill (one game at a time per platform) → validate. **Failure state:** misfire grace-time 24 h; startup reaper clears stale `state='running'` rows; scheduler death exposed via `/api/health` `scheduler_running: bool` + 503 (JQ3).
- **F13. Scheduled Full-Library Validation Sweep** *(added by OQ7)*: Separate APScheduler cron (default Sundays 03:00) → F7 validate every cached, non-blocked game in batches of 10. **Failure state:** validator unhealthy → skip; per-game errors don't abort the batch.
- **F14. Game_shelf Backend Proxy Routes** *(promoted to MVP by OQ1)*: In `kraulerson/Game_shelf` — Express routes at `/api/cache/*` proxy 10 read/write endpoints to the orchestrator API with an injected bearer token. `POST /api/cache/platforms/:name/auth` explicitly NOT proxied (per OQ2). **Failure state:** orchestrator 401 → Game_shelf 502; timeout/refused → 503 with `{"status":"orchestrator_offline"}`.
- **F15. Game_shelf Cache Badge + Cache Panel** *(promoted to MVP by OQ1)*: React components rendered inline on Library + GameDetail pages. 7 states each rendered with **color + icon + text label** (colorblind-safe per Intake §9). Bulk fetch for library view (no N+1). **Failure state:** offline → neutral `—` badges, mutations disabled; tolerant field merging on schema skew.
- **F16. Game_shelf Cache Dashboard Page** *(promoted to MVP by OQ1)*: New `pages/Cache.jsx` with stats, platform auth cards (copy-pasteable reconnect command), recent 25 jobs, block-list management (≥500 entries without pagination). **Failure state:** offline → full-page banner + skeleton sections; per-section fetch errors isolated.
- **F17. Orchestrator ↔ Game_shelf Graceful Degradation** *(promoted to MVP by OQ1)*: Bidirectional offline handling. Orchestrator keeps running unchanged when Game_shelf is down; Game_shelf library remains functional when orchestrator is down. `/api/v1/` versioning + `/api/cache/health` version field for skew detection. **Failure state:** network partition → each side's local flow unaffected; no retry storms (one check per page load, manual Retry button otherwise); bearer token NEVER reaches Game_shelf frontend (CI grep enforces).

### Should-Have (v1.1)

- **SSE/WebSocket live progress stream:** Replace 2-second polling with push. **Deferred because:** MVP polling is sufficient at one-operator scale.
- **Access-log tail for passive HIT/MISS observation + eviction detection:** Watch Lancache `access.log` for signals the validator can't see. **Deferred because:** disk-stat validator already answers "is it cached now?"; eviction-pressure indicator is cosmetic.
- **Prometheus metrics endpoint:** Expose `/metrics` for external monitoring. **Deferred because:** no monitoring stack in this homelab yet.
- **Incremental validation:** Only re-validate games whose manifest changed since last validation — optimization on F13 full sweep. **Deferred because:** F13 at 2,600 games is cheap enough on DXP4800 hardware.
- **CLI `--json` flag:** Machine-parseable output for scripting (OQ6). **Deferred because:** MVP consumers are a human operator; no scripts planned.

### Will-Not-Have

- **Ubisoft Connect prefill:** Lancache caching broken upstream (monolithic#195, 21-month-open issue); no production-quality protocol library exists.
- **EA App prefill:** Hostile vendor with TLS-pinned sessions; only OSS effort (Maxima) self-describes as "pre-pre-pre-alpha"; most endpoints are non-cacheable post-2025 refactor.
- **GOG prefill:** GOG CDN not in `uklans/cache-domains`; HTTPS-only to Fastly; Lancache architecturally cannot intercept.
- **Game installation or launching:** The orchestrator fills the cache only. Installing or running games is out of scope.
- **Multi-user or multi-tenant support:** Single user, single Lancache, LAN-only.
- **A second React SPA inside the orchestrator:** Rich UI lives in Game_shelf (F14–F17). Orchestrator ships single-file HTML diagnostics + CLI only.
- **ORM (SQLAlchemy / Alembic):** Seven tables of straightforward CRUD; raw SQL via `aiosqlite` is easier to audit and removes a large dependency chain.

---

## 3. User Journeys

<!-- Full detail in docs/phase-0/user-journey.md — 8 journeys A–H, 8 standing Skeptical-PM rules, 13 section-specific feature-gap flags. Summary form here. -->

### Persona

- **Who:** Karl — a solo homelab operator running a Proxmox cluster, Docker stacks, pfSense firewall, MikroTik switch, and Pi-hole DNS on a well-maintained home network. Deploys the orchestrator alongside existing Lancache on a DXP4800 NAS. Uses Game_shelf as the library UI.
- **Skill Level:** High — comfortable with SSH, Docker Compose, `jq`, structured JSON logs. **Not** a Python developer; will not read the source to diagnose a bug. Expects the CLI and status page to explain failures clearly.
- **Goal:** Set-it-and-forget-it. Keep a ~2,600-game multi-platform library cached and fresh with **trustworthy** cache-state reporting.
- **Emotional State on Arrival:** Skeptical. Has been burned by prefill tooling before (SteamPrefill's flat-file tracker drifts from actual cache state). Default assumption: any "up to date" indicator is lying until proven otherwise.
- **Accessibility constraint (Intake §9):** Colorblind. Every status indicator — status page, Game_shelf badges, CLI output — must use color + icon + text label. Never color alone.

### Success Path

1. **Deploy.** User adds orchestrator service block to existing Lancache `docker-compose.yml`, writes a Docker secret for the bearer token, runs `docker compose up -d`. System responds: structured JSON logs show `orchestrator_token_loaded` → `migration_applied` → `lancache_reachable=true` → `cache_volume_mounted=true` → `scheduler_ready` → `api_ready`. Status page at `http://<dxp4800>:8765/` is live.
2. **Authenticate platforms.** User runs `docker compose exec orchestrator orchestrator-cli auth steam`, enters username + password + Steam Guard code (type surfaced by F1 JQ1). Repeats for Epic with auth code from `https://legendary.gl/epiclogin`. System responds: persists refresh tokens at mode 0600, updates `platforms.*.auth_status='ok'`, prints SUCCESS — including a warning about the expected "new device login" email from Valve.
3. **First sync.** Within 6 h (or immediately via `orchestrator-cli library sync`), F3/F4 enumerate both libraries into `games`; F12 diff enqueues prefills; F5/F6 fill the cache; F7 validates each result. User observes: status page shows `last_sync_at` updating, jobs queue draining, badges transitioning `missing → downloading → cached`.
4. **Connect Game_shelf.** User sets `ORCHESTRATOR_URL` and `ORCHESTRATOR_TOKEN` env vars on Game_shelf's host. F14–F17 activate. User sees cache badges inline on library cards, CachePanel on game detail, Cache dashboard page at `/cache`.
5. **Steady state.** User interacts only when they want to. New Steam/Epic purchases appear within 6 h (or on demand via CLI sync). F13 runs weekly to catch eviction drift. Game_shelf and status page both surface current state any time the user checks.

### Failure Recovery

- **Step 1 — secret missing:** Container refuses to start with `CRITICAL orchestrator_token_missing`, error includes exact expected path and README pointer.
- **Step 1 — Lancache unreachable:** Orchestrator does NOT refuse to start; `/api/health` reports `lancache_reachable=false`; status page shows red banner with actionable error.
- **Step 2 — wrong 2FA type (mobile vs email):** F1's JQ1 resolution — if `steam-next` exposes challenge type, CLI prompt discriminates; otherwise prompt text enumerates both possibilities and README documents.
- **Step 2 — Steam mobile-approval pending:** CLI times out cleanly after 120 s with re-run guidance; no orphaned session.
- **Step 3 — API rate limit mid-sync:** Honor `Retry-After`; max 3 retries with fixed 60 s backoff; abort cycle cleanly, retry next window.
- **Step 3 — one game prefill repeatedly failing:** Marked `status='failed'` with `last_error`; surfaced on status page + Cache dashboard; next cycle retries; user chooses to block via F8 if persistently broken.
- **Step 4 — bearer token mismatch between Game_shelf and orchestrator:** Game_shelf logs loudly, frontend sees 502 with generic error; rotation documented.
- **Step 4 — orchestrator unreachable during normal use:** Library page renders with neutral badges, dismissible "Cache orchestrator unreachable" banner, mutations disabled. One health check per page load; no retry storms.

### Exit Points

Complete inventory in `docs/phase-0/user-journey.md` §11. Highlights:

- **X1. Setup wall.** Errors missing actionable fix → project abandonment. Recovery: every fatal startup error includes error code + exact file path + README pointer.
- **X2. 2FA type confusion.** Mobile-authenticator vs email-code ambiguity → frustration after 3 wrong codes. Recovery: JQ1 resolution above.
- **X3. Lost bearer token.** User rebuilds stack, new token, browser's `sessionStorage` stale → 401. Recovery: 401 banner includes command to re-read the secret.
- **X4. Silent auth expiry.** No notifications in MVP (Intake §10 defers). Recovery: documented as "check status page or Cache dashboard weekly" in Phase 4 handoff.
- **X5. Post-F13-sweep panic.** User sees 84 games transition to `validation_failed` after eviction drift → thinks something is broken. Recovery: dashboard surfaces sweep summary with explanation banner.
- **X6. Impatience after first auth.** 6 h cadence feels too slow on day 1. Recovery: status page shows "Next sync in Xh Ym" countdown; CLI `library sync` documented prominently.
- **X7. Valve "new device" email.** Out-of-band side effect mistaken for compromise → user rotates password → session invalidated. Recovery: CLI `auth steam` warns; README Security/Operations section documents prominently.
- **X8. pfSense rule blocks port 8765.** Game_shelf proxy times out → frontend offline state. Recovery: deployment guide step; logs capture failed inbound attempts for diagnosis.

---

## 4. Data Contracts

<!-- Full detail in docs/phase-0/data-contract.md. Summary form here. -->

### Inputs

- **Docker secrets (IS1):** `/run/secrets/orchestrator_token` (Confidential; required; minimum 32 chars; SHA256-prefix in logs). Steam Web API key fallback removed from MVP by DQ1.
- **Environment variables (IS2):** 14 typed settings via `pydantic-settings` — `LANCACHE_HOST`, `SCHEDULE_CRON`, `VALIDATION_SWEEP_CRON`, `PREFILL_CONCURRENCY`, `PER_PLATFORM_PREFILL_CONCURRENCY`, `STEAM_PREFERRED_CDN`, `EPIC_PREFERRED_CDN`, `CORS_ORIGINS`, `LOG_LEVEL`, `CACHE_ROOT`, `STATE_DIR`, `SWEEP_WARN_HOURS`, `API_BIND_HOST`, `API_PORT`. Invalid config → fail-fast.
- **CLI stdin (IS3):** Steam username / password / Steam Guard; Epic auth code; status-page `prompt()` bearer token. All Confidential. RAM-only during invocation; only refresh tokens persist.
- **REST API request bodies + headers (IS4):** Pydantic schemas on every endpoint; bearer auth precedes schema validation; `POST /api/v1/platforms/{name}/auth` additionally requires `127.0.0.1` origin.
- **Upstream HTTP responses (IS5):** Steam CM, Steam CDN, Epic Games Services, Epic CDN, Lancache. All untrusted; 128 MiB manifest-size cap (DQ7, configurable via `MANIFEST_SIZE_CAP_BYTES`); 10 s read timeout per chunk; stream-discard (bodies never fully loaded into memory).
- **SQLite DB reload on restart (IS6):** Numbered `.sql` migrations applied atomically; startup reaper clears mid-flight `jobs.state='running'` rows; corrupted session files downgraded to `auth_status='never'`.

### Transformations

Twelve discrete operations (T1–T12) with their own failure behavior:

- **T1. Library enumeration** → rows in `games`.
- **T2. Manifest fetch** → parsed manifest (protobuf for Steam, vendored legendary parser for Epic).
- **T3. Chunk dedupe** → unique-SHA set. Pure function.
- **T4. Chunk fan-out** → Lancache populated via stream-discard. Per-chunk retry (3× with 1/4/16 s backoff).
- **T5. Cache-key computation** → MD5 + disk path. Pure function. Self-tested at boot.
- **T6. Disk-stat validation** → validation counts + outcome.
- **T7. Diff** → prefill decisions. Pure function.
- **T8. Title resolution (Epic)** → id→title dict, fallback to `epic:<id>` sentinel.
- **T9. Cron-trigger serialization** → APScheduler invocation; cron parse error → fail-fast on settings load.
- **T10. API request → job** → `jobs` row + `games.status` update; concurrent job dedupe (409 on in-flight).
- **T11. Log emission** → structured JSON line per event.
- **T12. Health aggregation** → `/api/v1/health` response; any component unhealthy → 503.

### Outputs

- **Structured JSON logs on stdout:** Docker log-driver-retention is operator's concern (DQ4).
- **REST API `/api/v1/*` responses:** SLAs from Intake §2.3 — health < 50 ms p99 idle, games list < 500 ms for 2,200 entries, mutations < 100 ms. Versioned under `/api/v1/`; Game_shelf targets that prefix explicitly. Mutation response envelope normalized to `{"ok", "job_id", "message"}` (DQ6).
- **Status page HTML:** Served at `GET /`, < 20 KB gzipped, < 1 s first paint, vanilla JS + inline CSS.
- **CLI stdout:** Human-readable; `--json` Post-MVP.
- **SQLite `.backup` files:** Weekly external cron on DXP4800; per Intake §5.4.

### Third-Party Data

- **Steam CM (fabieu/steam-next):** Authentication + library + manifests + depot keys. Fallback: mark platform `expired`, retry next cycle.
- **Steam CDN:** Chunk GETs via Lancache, stream-discard. Fallback: per-chunk retry; persistent 403 → refresh manifest code; 508 → fatal config.
- **Epic Games Services (vendored legendary):** OAuth + library + catalog titles + manifest URLs. Fallback: same pattern as Steam CM.
- **Epic CDN:** Chunk GETs via Lancache. Fallback: same as Steam CDN.
- **Lancache nginx (compose peer):** Heartbeat + proxied CDN. Fallback: **fatal for orchestrator's purpose** — exposed via `/api/v1/health` 503.
- **Lancache filesystem (read-only mount):** Disk-stat validation paths. Fallback: validations report `outcome='error'` when mount is missing.

### State

- **Persists (SQLite):** `platforms`, `games`, `manifests` (incl. compressed raw BLOB per DQ3), `block_list`, `validation_history`, `jobs`, `cache_observations` (present in schema per DQ2, populated Post-MVP).
- **Persists (host filesystem):** `steam_session.json`, `epic_session.json` at mode 0600.
- **Ephemeral:** httpx connections, asyncio semaphore state, APScheduler `MemoryJobStore`, depot keys, manifest request codes (4.5 min TTL), correlation IDs, CLI interactive prompt state.
- **Retention:** `validation_history` 90 days; `jobs` 90 days (keep `error` rows indefinitely); `manifests` latest 3 versions per game; active state never auto-pruned.
- **Referential integrity:** `games.platform` FK declared `ON DELETE RESTRICT` (DQ8).
- **Backup:** Weekly external `sqlite3 .backup` cron on DXP4800.

### PII

**None handled.** The orchestrator stores no PII in the regulated sense — only credentials (Confidential, cryptographically sealed at mode 0600) and game library metadata (Internal). Full sensitivity summary in `docs/phase-0/data-contract.md` §9c.

---

## 5. MVP Cutline

<!-- Hard line. Features above ship first. Features below are Post-MVP candidates only. Do not move items above the line without Orchestrator approval and a recorded APPROVAL_LOG.md decision. -->

**Above the line (MVP — ships first):**

- F1. Steam CM Authentication
- F2. Epic OAuth Authentication
- F3. Steam Library Enumeration
- F4. Epic Library Enumeration
- F5. Steam CDN Prefill
- F6. Epic CDN Prefill
- F7. Cache Validator (disk-stat)
- F8. Block List
- F9. REST API
- F10. Status Page (single-file HTML)
- F11. CLI (`orchestrator-cli`)
- F12. Scheduled Sync Cycle
- F13. Scheduled Full-Library Validation Sweep (added by OQ7)
- F14. Game_shelf Backend Proxy Routes (promoted by OQ1)
- F15. Game_shelf Cache Badge + Cache Panel (promoted by OQ1)
- F16. Game_shelf Cache Dashboard Page (promoted by OQ1)
- F17. Orchestrator ↔ Game_shelf Graceful Degradation (promoted by OQ1)

**Implicit-dependency foundations** (required by MVP, explicitly funded):

- ID1. SQLite migrations framework (`migrations/*.sql` + ~50-LoC migrate script).
- ID2. Lancache reachability self-test + cache volume mount check at boot.
- ID3. Structured logging with correlation IDs (`structlog` → JSON on stdout).
- ID4. Docker secret loading with fail-fast on missing.
- ID5. Automatic post-prefill validation trigger (F7 enqueued from F5/F6 completion).
- ID6. Startup reaper for abandoned `jobs` rows.
- ID7. CLI bundled in container image.
- ID8. Status-page accessibility compliance (colorblind-safe).

---

**CUTLINE — nothing below this line is built in Phase 2 without Orchestrator approval**

---

**Below the line (Post-MVP — see Section 6):**

- SSE/WebSocket live progress stream
- Access-log tail for passive HIT/MISS observation and eviction detection
- Prometheus metrics endpoint
- Incremental validation (optimization on F13)
- CLI `--json` flag
- Webhook / ntfy notifications for auth expiry and repeated failures (journey-mapping flag FG3)
- New-purchase fast cycle (narrows the 6 h window for games played immediately after purchase; FG4)
- CLI fuzzy title search (FG5)
- `orchestrator-cli diagnostics` one-command bundle (FG6)
- LRU-pressure proactive alert (FG7)
- Backup verification / off-host replication
- Multi-user support (explicitly out of scope of MVP *and* Post-MVP until demand exists)

---

## 6. Post-MVP Backlog

Items below are candidates, not commitments. Prioritized by user feedback after launch, not by this document. No priority ordering here.

- **SSE/WebSocket live progress stream.** Would ship if: 2-second polling feels laggy during a large prefill, or if multiple concurrent operators ever exist.
- **Access-log tail (passive HIT/MISS + eviction detection).** Would ship if: the operator wants visibility into cache hits for content the orchestrator doesn't own (e.g., content family members download on their own gaming PCs), or if eviction pressure becomes a real issue.
- **Prometheus metrics endpoint.** Would ship if: a homelab-wide Prometheus stack is introduced and the operator wants orchestrator health/jobs/sweep metrics alongside everything else.
- **Incremental validation.** Would ship if: F13 full-library sweep exceeds 30 min consistently on DXP4800 hardware.
- **CLI `--json` flag.** Would ship if: the operator starts scripting against orchestrator state (e.g., a custom dashboard, automated regression testing).
- **Webhook / ntfy notifications (journey FG3).** Would ship if: the operator finds they're not checking the status page often enough and missing auth expiry for more than 48 h at a time.
- **New-purchase fast cycle (journey FG4).** Would ship if: a user reports repeatedly "bought a game, played it immediately, missed cache" at a frequency that justifies the complexity of a separate 15-min cron.
- **CLI fuzzy title search (journey FG5).** Would ship if: the operator builds muscle memory for specific games and wants `orchestrator-cli game "elder scrolls"` instead of looking up app_ids.
- **`orchestrator-cli diagnostics` bundle (journey FG6).** Would ship if: issue-reporting becomes a real workflow (e.g., project open-sourced for community and multiple users report bugs).
- **LRU pressure proactive alert (journey FG7).** Would ship if: cache thrashing becomes observable (F13 flagging many games as evicted each week).
- **Backup verification / off-host replication.** Would ship if: a backup restoration ever fails, or if the DXP4800 volume storing backups is considered insufficient.

---

## 7. Will-Not-Have List

Explicit scope boundaries applying to the entire product, not just the MVP. These are not deferred features — they are out of scope.

- **Ubisoft Connect prefill:** Lancache caching is broken upstream (`lancachenet/monolithic` issue #195, open since July 2024 with no fix); `ubisoft-manifest-downloader` is Windows-only, 9 stars, WIP; manifest schema is not public. Months of RE work against a brittle moving target with no cache to fill at the end.
- **EA App prefill:** Hostile vendor with TLS-pinned download sessions. Only OSS effort (`Maxima`) is self-described "pre-pre-pre-alpha"; Origin retired 2025-04-17 and the EA App refactor moved many endpoints off-cacheable. Not a tractable target for a solo project.
- **GOG prefill:** GOG is not in `uklans/cache-domains` and never has been. GOG's CDN is HTTPS-only to Fastly. Lancache cannot intercept HTTPS without a CA-trusted MITM, which is outside its model. Architecturally impossible with current Lancache design.
- **Game installation or launching:** The orchestrator fills the cache. It never installs or runs games. This discipline keeps the trust surface small and makes the system auditable.
- **Multi-user / multi-tenant support:** Single user, single Lancache, LAN-only. No user accounts, no role-based access, no isolation between users. If this ever changes, it's a different product.
- **A second React SPA inside the orchestrator:** Rich UI lives exclusively in Game_shelf (F14–F17). The orchestrator ships a single-file HTML diagnostics page (F10) and a CLI (F11). Building a second SPA would duplicate Game_shelf's work for no value.
- **ORM (SQLAlchemy / Alembic):** Seven tables, straightforward CRUD, no polymorphism, no lazy relationships. Raw SQL via `aiosqlite` is easier to read and audit than ORM DSL at this scale. Dropping SQLAlchemy also removes the need for APScheduler's `SQLAlchemyJobStore` — we use `MemoryJobStore` instead. This is a deliberate simplification.

---

## 8. Open Questions

All questions flagged during Phase 0 Steps 0.1–0.3 are resolved. No question remains Open. Full resolution history is preserved in the Phase 0 intermediate artifacts (`docs/phase-0/frd.md` §7, `docs/phase-0/user-journey.md` §15, `docs/phase-0/data-contract.md` §9). Summary of decisions that govern Phase 1+:

**Q1 (OQ1): Should Game_shelf integration be MVP or Post-MVP?**
- Context: Intake Success Criteria #5 required cache badges in Game_shelf; Intake §4.2 classified the integration as Post-MVP. Contradiction.
- Decision: **Keep as MVP.** Promoted F14–F17 to Must-Have. Intake §4.1 and §4.2 updated.
- Status: Resolved — 2026-04-20

**Q2 (OQ2): Should `POST /api/v1/platforms/{name}/auth` be remote-callable or localhost-only?**
- Context: F1/F2 say credentials enter only at the CLI on DXP4800; F9 listed a REST endpoint for auth.
- Decision: **Keep the endpoint but enforce `127.0.0.1` origin.** Bearer token + IP check together.
- Status: Resolved — 2026-04-20

**Q3 (OQ3): Brief "Phase 0–4" terminology conflicts with Solo Orchestrator "Phase 0–4."**
- Context: Two different numbering systems create ambiguity.
- Decision: **All Brief delivery phases renamed "Build Milestones A–E"** (A=Spikes, B=Steam+Core, C=Epic, D=Game_shelf, E=Ops hardening). "Phase 0–4" reserved exclusively for Solo Orchestrator methodology.
- Status: Resolved — 2026-04-20

**Q4 (OQ4): `fabieu/steam-next` bus-factor policy.**
- Context: Single-maintainer, 3-star fork. Risk R1 in Brief.
- Decision: **Pin SHA in Phase 2; monitor weekly; fork to `kraulerson/steam-next` if upstream has no commits for >15 days.** Formalized as Phase 1 ADR.
- Status: Resolved — 2026-04-20

**Q5 (OQ5): Status page auth UX.**
- Context: Browser `prompt()` is ugly; alternatives include login form, query-param token, IP allowlist.
- Decision: **Keep `prompt()`.** Diagnostic page for one operator; not worth the login-form surface.
- Status: Resolved — 2026-04-20

**Q6 (OQ6): CLI `--json` flag.**
- Context: Useful for scripting; adds per-command dual rendering paths.
- Decision: **Post-MVP.** MVP CLI consumers are the human operator only.
- Status: Resolved — 2026-04-20

**Q7 (OQ7): Scheduled full-library validation sweep.**
- Context: R13 (cache eviction drift) mitigated by "re-validate on schedule" in Brief but no Must-Have feature scheduled it.
- Decision: **Add as MVP F13** — weekly cron, default Sundays 03:00.
- Status: Resolved — 2026-04-20

**Q8 (JQ1, from user-journey mapping): CLI Steam-auth 2FA type disambiguation.**
- Context: Email 2FA code (6 digit numeric) and mobile authenticator code (5 char alphanumeric) look different; CLI prompt was ambiguous.
- Decision: **MVP-conditional:** if `steam-next` exposes the challenge type, CLI prompt discriminates. Otherwise prompt lists both possibilities and README documents. Phase 1 Step 1.2 deliverable.
- Status: Resolved — 2026-04-20

**Q9 (JQ2): Silent auth expiry for MVP with no notification mechanism.**
- Context: Operator might miss auth-expired state for days; Intake §10 defers notifications.
- Decision: **Accepted.** Phase 4 handoff docs list "check status page or Cache dashboard weekly" as an explicit maintenance task. Post-MVP FG3 tracks follow-up.
- Status: Resolved — 2026-04-20

**Q10 (JQ3): Scheduler-health check in `/api/health` as F12 acceptance.**
- Context: Skeptical-PM flagged silent scheduler death as a real failure mode.
- Decision: **Added as F12 hard acceptance criterion.** `/api/health` returns 503 with `{"scheduler_running": false, "scheduler_last_error": "..."}` on scheduler death. Status page + Cache dashboard show prominent red banner.
- Status: Resolved — 2026-04-20

**Q11 (DQ1): `steam_webapi_key` optional fallback.**
- Context: Intake §5.1 had it as optional; MVP doesn't exercise it.
- Decision: **Drop from MVP.** Intake §5.1 strikethrough with resolution note. Revisit Post-MVP if ever needed.
- Status: Resolved — 2026-04-20

**Q12 (DQ2): `cache_observations` table in `0001_initial.sql` vs deferred.**
- Context: Post-MVP access-log-tail feature populates it; empty table now is confusing but avoids a later schema migration.
- Decision: **Create now.** Ships in `migrations/0001_initial.sql`.
- Status: Resolved — 2026-04-20

**Q13 (DQ3): `manifests.raw` storage — SQLite BLOB vs external files.**
- Context: ~500 MB total at 12-month volume; BLOB makes `VACUUM` slower but simpler to back up.
- Decision: **BLOB in SQLite for MVP.** Revisit if `VACUUM` exceeds 5 s at 12 months.
- Status: Resolved — 2026-04-20

**Q14 (DQ4): Log retention ownership.**
- Context: Orchestrator emits JSON to stdout; rotation happens in Docker's logging driver.
- Decision: **Docker logging driver owns retention.** Phase 4 HANDOFF.md must include explicit configuration guidance.
- Status: Resolved — 2026-04-20

**Q15 (DQ5): Audit trail for `block_list` mutations beyond logs.**
- Context: On unblock, row is deleted — no historical record.
- Decision: **Accept current schema.** Structured logs cover every block/unblock event with context.
- Status: Resolved — 2026-04-20

**Q16 (DQ6): Mutation response envelope.**
- Context: FRD examples inconsistent (some returned `{"job_id", "status"}`, others `{"ok": true}`).
- Decision: **Normalize to `{"ok": bool, "job_id": int|null, "message": str|null}`.** Applied to every mutation endpoint.
- Status: Resolved — 2026-04-20

**Q17 (DQ7): Manifest response size cap.**
- Context: Typical ~100 KB–10 MB; flight-sim outliers ~50 MB.
- Decision: **128 MiB default, configurable via `MANIFEST_SIZE_CAP_BYTES` env var (range 1 MiB – 1 GiB).**
- Status: Resolved — 2026-04-20

**Q18 (DQ8): Platform FK cascade behavior.**
- Context: `games.platform REFERENCES platforms(name)` with no explicit ON DELETE.
- Decision: **Explicit `ON DELETE RESTRICT`.** Prevents silent cascade-delete of every game if platform row is ever accidentally removed.
- Status: Resolved — 2026-04-20

---

## Appendix A: Revenue Model & Unit Economics

**SKIPPED — internal tool, no revenue model required (Light track, Personal deployment).** Per Builder's Guide Track Requirements Matrix, Light-track projects skip Step 0.5. Project is single-user, personal, runs on existing homelab hardware, costs $0 incremental per Intake §3.2. If ever open-sourced for community use, there is still no revenue model — MIT-or-similar license is the intended path.

---

## Appendix B: Orchestrator Competency Matrix

Self-assessment of ability to validate AI-generated output in each domain. Honest assessment drives mandatory tooling. Source: Intake §6.2, refined by Data Contract §7 and User Journey accessibility constraint.

| Domain | Can I Validate? | If No / Partially: Automated Tool |
|---|---|---|
| Product / UX Logic | Yes | — (manual review + Skeptical PM pass during Phase 0) |
| Frontend / UI (Game_shelf React components F15–F16) | Yes | — (existing Game_shelf code patterns inform review) |
| Backend / API (FastAPI + Pydantic + async asyncio) | Yes | pytest + httpx test client (CI-enforced) |
| Database (SQLite, raw SQL, numbered migrations) | Yes | Migration dry-run + schema diff on PR CI |
| Security (auth, injection, IDOR, timing attacks) | **Partially** | **Semgrep (SAST) + gitleaks (secret detection) + Snyk CLI (dep vuln scanning).** All three installed (Intake §12). CI-gated. |
| Build & Packaging (Python multi-stage Dockerfile, image publish to ghcr.io) | Yes | CI pipeline on GitHub Actions; release smoke test in a throwaway container |
| Accessibility (colorblind-safe status page F10 + Game_shelf badges F15 + CLI output) | **Partially** | Manual colorblind-simulation review in Chrome devtools; no formal WCAG AA scope (Light track + Intake §9 "minimal" but hard constraint on color-independence) |
| Performance (asyncio event-loop discipline, chunk fan-out under API load) | **Partially** | **Spike F (Brief §3.6 / Phase 1)** — sustained 32-concurrent ≥300 Mbps chunk downloads while pinging `/api/health` with p99 < 100 ms target. **If Spike F fails, subprocess isolation for the downloader is the fallback.** |
| Platform-Specific | **N/A** — single target (Linux/Docker on DXP4800 ARM NAS). Portability to x86 Linux exists as a non-critical property (primary target is the operator's DXP4800). |

**Mandatory tooling requirement (per Builder's Guide Section 0.6 Enforcement):** All "Partially"-flagged domains have automated tooling active:
- **Security:** Semgrep 1.157.0, gitleaks 8.30.1, Snyk CLI 1.1304.0. CI-gated before Phase 2.
- **Accessibility:** Manual review baseline; no tooling (scope too small for axe-core overhead).
- **Performance:** Spike F load test in Phase 1 (architecture-gated).

**Known gap acceptance:** Accessibility is only partially tool-gated because the orchestrator's own UI surface is a single ~20 KB HTML page + CLI output. Game_shelf's React components (F15–F16) inherit existing Game_shelf accessibility practices. If Phase 2 UAT reveals specific WCAG failures, they become bugs in Phase 3 remediation.

---

## Appendix C: Trademark & Legal Pre-Check

**SKIPPED — internal tool, no trademark check required (Light track, Personal deployment).** Per Builder's Guide Track Requirements Matrix, Light-track projects skip Step 0.7. The project name "lancache_orchestrator" is descriptive and intentionally generic. If ever open-sourced, a name audit happens at that promotion event — not during MVP.

**Data Privacy Applicability:**
- GDPR: **N/A** — no user data collected, no users besides the operator, LAN-only.
- CCPA: **N/A** — same.
- Other regulations: **N/A.** Data Contract §9c confirms zero PII, zero financial data, zero health/regulated data.

**Distribution Channel Requirements:**
- **Container registry:** GitHub Container Registry (`ghcr.io`). No code signing or review process required.
- **Public/private:** Repository will be public if open-sourced, private otherwise. No app-store distribution; no code signing; no privacy-policy URL (no data collection surface).

---

**End of Product Manifesto. All 8 numbered sections populated. Appendices A and C SKIPPED per Light track. Appendix B populated. Every Phase 0 question resolved (18 in §8); the Phase 0 → Phase 1 gate's unresolved-question check is expected to pass.**
