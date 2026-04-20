# Solo Orchestrator — Project Intake Template

## Version 1.0

---

## Document Control

| Field | Value |
|---|---|
| **Document ID** | SOI-004-INTAKE |
| **Version** | 1.0 |
| **Classification** | Project Initialization Template |
| **Date** | 2026-04-20 |
| **Companion Documents** | SOI-002-BUILD v1.0 (Builder's Guide), SOI-003-GOV v1.0 (Enterprise Governance Framework) |

---

## Purpose

This template collects every decision, constraint, and context variable that the AI agent needs to execute the Solo Orchestrator methodology with maximum autonomy. Fill it out completely before starting Phase 0. Incomplete sections will force the agent to stop and ask — every blank field is a round-trip.

### How This Document Flows Into the Process

The Intake is the primary input to the Builder's Guide. Here's where each section goes:

| Intake Section | Consumed By | Purpose |
|---|---|---|
| **1. Project Identity** | Phase 0 initialization, Platform Module selection | Names the project, sets the track, identifies which Platform Module the agent loads |
| **2. Business Context** | Phase 0 Steps 0.1-0.2 | The agent validates and expands this into the FRD and User Journey — it doesn't re-discover it |
| **3. Constraints** | Phase 0 and Phase 1 | Timeline, budget, and user targets constrain architecture and scope |
| **4. Features & Requirements** | Phase 0 Steps 0.1, 0.4 | The agent expands logic triggers and failure states, flags gaps, produces the Manifesto |
| **5. Data & Integrations** | Phase 0 Step 0.3, Phase 1 Step 1.4 | Drives the Data Contract, data model design, and third-party integration architecture |
| **6. Technical Preferences** | Phase 1 Steps 1.2-1.6 | Hard constraints and preferences feed directly into architecture proposals; Competency Matrix determines where automated tooling is mandatory |
| **7. Revenue Model** | Phase 0 Step 0.5, Phase 1 Step 1.2 | Hosting/distribution cost ceiling constrains architecture; pricing model shapes feature decisions |
| **8. Governance Pre-Flight** | Enterprise Governance Framework pre-conditions | Maps directly to the organizational approvals required before Phase 0 can begin |
| **9. Accessibility & UX** | Phase 1 Step 1.5, Phase 3 Step 3.4 | Architectural constraints from Day 1, not Phase 3 afterthoughts |
| **10. Distribution & Operations** | Phase 4, Platform Module | Distribution channels, monitoring, update strategy — platform-dependent |
| **11. Known Risks** | Phase 1 Step 1.3 | Additional inputs for the Iron Logic Stress Test |

The more complete the Intake, the more autonomously the agent can work. Where the Intake is vague or incomplete, the Builder's Guide prompts shift from validation to discovery — the agent will ask targeted questions instead of proposing options it doesn't have enough context to evaluate.

### How to Use This Document

You can fill this out using the **intake wizard** (`bash scripts/intake-wizard.sh`) or by **editing this file directly**. The wizard offers an interactive walkthrough and tracks your progress. Either approach works, but be aware of the difference:

1. Fill out every section. Mark fields N/A where they genuinely don't apply — don't leave blanks.
2. For organizational deployments, complete the Governance Pre-Flight (Section 8) before starting. This section maps to the Enterprise Governance Framework pre-conditions.
3. Once complete, provide this document to the AI agent at the start of Phase 0 with the instruction: "This is the Project Intake. Use it as the primary constraint for all phases. Do not suggest features, architectures, or tooling that contradict it."
4. The agent will use this to generate the Product Manifesto (Phase 0) and Project Bible (Phase 1) without stopping to ask for information that should already be decided.

> **If editing manually:** Section 1 fields (project name, platform, language, track) and Section 8 (governance mode) were used during init to generate your CI pipeline, release pipeline, platform module, and phase gate rules. If you change these fields here, you must also run the reconfigure script to update the generated files:
>
> ```bash
> bash scripts/reconfigure-project.sh --field <field> --old <old_value> --new <new_value>
> ```
>
> Supported fields: `name`, `platform`, `language`, `track`, `deployment`. The intake wizard handles this automatically — manual editing does not.

---

## 1. Project Identity

| Field | Value |
|---|---|
| **Project name** | lancache_orchestrator |
| **Project codename** (if different from public name) | N/A |
| **One-sentence description** | An autonomous Python service that replaces SteamPrefill/EpicPrefill by natively authenticating to Steam and Epic CDNs, proactively downloading game content through a Lancache proxy, validating cache state via disk-stat inspection, and exposing a REST API for status queries and commands — with Game_shelf as the display layer. |
| **Project track** | Light |
| **Platform type** | other |
| **Platform Module** | None |
| **Target platforms** | Linux (Docker container on Ugreen DXP4800 NAS). REST API consumed by Game_shelf (Express/Node.js on separate host). CLI runs inside the container. |
| **Is this a personal project or organizational deployment?** | Personal |
| **Repository URL** (if already created) | To be created under github.com/kraulerson |

---

## 2. Business Context

### 2.1 The Problem

```
The Lancache prefill tooling (SteamPrefill, EpicPrefill) is unreliable. These tools track
download state in a local flat file that drifts from the actual nginx cache state, causing
them to report games as "up to date" when the content is not actually cached. They cannot
target specific games by app ID, have no mechanism to validate whether games are really
cached, and don't exist at all for Ubisoft Connect, EA App, or GOG.

With a library of ~2600 games across five platforms, the current workflow requires running
prefill scripts on 6-hour cron jobs that regularly miss new purchases and updates. Manual
cache seeding is required for three of the five platforms. There is no visibility into what
is actually cached versus what the tools claim is cached.
```

### 2.2 Who Has This Problem

| Field | Value |
|---|---|
| **Primary user persona** | Homelab operator with a large multi-platform game library and a Lancache instance. Technically advanced (can SSH, manage Docker, read logs). Wants a "set it and forget it" system that proactively caches all owned games and tells them what's actually cached. |
| **Secondary personas** | N/A — single-user system. Game_shelf exposes cache status to the same user via a web UI. |
| **How do they solve this problem today?** | SteamPrefill and EpicPrefill on 6-hour cron jobs with `--recently-purchased` flags. Manual downloads through game clients for Ubisoft/EA/GOG to seed the cache. Manual spot-checking by installing a game and watching whether traffic hits the WAN or cache. |
| **What's wrong with the current solution?** | Prefill tools trust their own flat-file log instead of actual cache state, miss new purchases, can't target individual games, and three platforms have no prefill tooling at all. No dashboard, no validation, no visibility. |

### 2.3 Success Criteria

| Metric | Target | How Measured |
|---|---|---|
| New Steam/Epic purchases cached within 6 hours of purchase | 100% (excluding blocked games) | Orchestrator reports `up_to_date` status; validated by disk-stat |
| Cache state accuracy | Orchestrator status matches reality (no false "cached" reports) | Periodic validation runs confirm disk-stat results match expected manifests |
| Zero dependency on SteamPrefill/EpicPrefill binaries | Complete removal | Prefill containers decommissioned; orchestrator handles all Steam/Epic prefill |
| API responsiveness during active prefills | `/api/health` p99 < 100ms during 32-concurrent chunk downloads | Spike F load test |
| Game_shelf displays cache status for all Steam/Epic games | Functional integration | Cache badges render on library view; game detail shows cache panel |

### 2.4 What This Is NOT

1. Not a replacement for Lancache itself — the nginx caching proxy layer is working fine and is not being modified.
2. Not a Ubisoft Connect, EA App, or GOG prefill tool — these platforms are explicitly out of scope (Ubisoft caching is broken upstream, EA is hostile/TLS-pinned, GOG is HTTPS-only and not in cache-domains).
3. Not a game installer or game launcher — the orchestrator downloads content through Lancache to fill the cache; it never installs games to disk or launches them.
4. Not a multi-user or multi-tenant system — single user, single Lancache instance, LAN-only.
5. Not a replacement for Game_shelf's library management — Game_shelf continues to own library enumeration, metadata enrichment, and the user-facing UI. The orchestrator owns cache state only.

---

## 3. Constraints

### 3.1 Timeline

| Field | Value |
|---|---|
| **Target MVP date** | No hard date. Phase 0 spikes validate feasibility; Phase 1 (Steam adapter + validator + API) is the functional MVP. |
| **Hard deadline?** | No |
| **Orchestrator availability** | Evenings and weekends, variable. Claude Code does the heavy lifting; human time is primarily review, testing, and auth setup. |
| **Blocked time or interleaved?** | Interleaved with other work and projects. |

### 3.2 Budget

| Field | Value |
|---|---|
| **Monthly infrastructure ceiling** | $0 incremental — runs on existing homelab hardware (DXP4800 NAS). No cloud hosting. |
| **One-time budget** | $0 — all infrastructure exists. |
| **AI subscription** | Already have Claude Max subscription + Claude Code. |
| **Who approves spending?** | Self |

### 3.3 Users

| Field | Value |
|---|---|
| **Users at launch** | 1 (Karl) |
| **Users at 6 months** | 1 — potentially open-sourced for community use but not multi-tenant |
| **Users at 12 months** | 1 + community contributors if open-sourced |
| **Internal only or external?** | Internal (personal LAN) |
| **Geographic distribution** | Single home network in Longmont, CO |

---

## 4. Features & Requirements

### 4.1 Must-Have Features (MVP)

| # | Feature | Business Logic Trigger | Failure State |
|---|---|---|---|
| 1 | **Steam CM authentication** | If the orchestrator starts and no valid Steam session exists, it must prompt for credentials via CLI (`orchestrator-cli auth steam`). If a valid refresh token exists, it must reconnect silently. | Auth failure: mark platform as `expired`, log error, continue operating for other platforms. Surface "Steam auth expired — reconnect via CLI" on status page and API. |
| 2 | **Epic OAuth authentication** | If the orchestrator starts and no valid Epic session exists, it must prompt for auth code via CLI (`orchestrator-cli auth epic`). If a valid refresh token exists, it must rotate silently (10-min pre-expiry buffer). | Auth failure: mark platform as `expired`, log error, continue operating for other platforms. Surface reconnect instruction on status page and API. |
| 3 | **Steam library enumeration** | Every 6 hours (configurable), the orchestrator must authenticate to Steam CM, pull the full owned library via PICS, and upsert into its `games` table with current manifest versions. | API unavailable: log error, retry next cycle. Partial response: process what was returned, log warning. |
| 4 | **Epic library enumeration** | Every 6 hours (configurable), the orchestrator must authenticate to Epic, pull the full owned library via EGS REST API with pagination, resolve codename titles via catalog bulk endpoint, and upsert into `games` table. | Same as Steam — log, retry next cycle. Title resolution failure: fall back to `{platform}:{app_id}`, resolve on next cycle. |
| 5 | **Steam CDN prefill** | If a game's `current_version` (manifest GID) differs from `cached_version`, or if `status` is `not_downloaded`, and the game is not blocked, the orchestrator must fetch the depot manifest, dedupe chunks, and download all chunks through Lancache using the stream-discard pattern with `Host:` header override. | Mid-download failure: mark as `failed` with error, retry next cycle. Manifest request code expiry (5-min TTL): refresh per-depot code at 4.5 min, retry on 403. Lancache 508 (loop detection): treat as fatal config error, surface clearly. |
| 6 | **Epic CDN prefill** | Same logic as Steam but using vendored legendary modules for manifest parsing and Epic's CDN URL pattern. Pin preferred CDN host for cache-key locality. | Same failure handling as Steam. |
| 7 | **Cache validator (disk-stat)** | On demand (via API/CLI) or after each prefill completes, the validator must compute the expected nginx cache key for each chunk (accounting for 1 MiB slicing), MD5 hash it, and `stat()` the expected disk path at `/data/cache/cache/<H[28:30]>/<H[30:32]>/<H>`. File exists + non-empty = cached. | Formula drift: startup self-test (HEAD probe + disk-stat on a known-cached chunk) must fail loudly if the cache-key formula doesn't match. Disk inaccessible: log error, mark validation as `error`. |
| 8 | **Block list** | If a game is added to the block list (via API or CLI), the orchestrator must skip it during scheduled prefill. Block list matches on `(platform, app_id)` exact. Blocking does NOT prevent manual validation. | N/A — block/unblock is idempotent. |
| 9 | **REST API** | The orchestrator must expose a FastAPI REST API on port 8765 with endpoints for: health, game list, game detail, validate, prefill, block/unblock, platform status, jobs, stats. Bearer token auth via Docker secret. | Unauthenticated request: 401. Malformed request: 400 with description. Internal error: 500 with logged traceback, sanitized response. |
| 10 | **Status page** | The orchestrator must serve a single-file HTML status page at `GET /` showing: orchestrator version/uptime, per-platform auth state, active/recent jobs, last errors, disk usage. Polls the REST API via vanilla JS fetch(). | Orchestrator API unreachable from its own status page: show "API unreachable" (should only happen if uvicorn itself is down). |
| 11 | **CLI** | Click-based CLI (`orchestrator-cli`) bundled in the container for: auth flows (Steam/Epic), library sync trigger, game status/actions, job inspection, DB migration, config display. | CLI commands hit the local REST API. If API is down, CLI reports "orchestrator not running." |
| 12 | **Scheduled sync cycle** | APScheduler with a configurable cron trigger (default: every 6 hours) runs the full cycle: refresh auth → enumerate libraries → diff versions → enqueue prefills → validate after prefill. | Scheduler failure: log error. Next trigger still fires (APScheduler MemoryJobStore re-registers on startup). |
| 13 | **Scheduled full-library validation sweep** (added by Phase 0 OQ7 resolution, 2026-04-20) | Second APScheduler cron (default `0 3 * * 0` — Sundays 03:00) runs F7 disk-stat validation against every non-blocked cached game. Batches of 10 in parallel. Emits summary log line on completion. Mitigates R13 (cache eviction drift). | Sweep elapsed > SWEEP_WARN_HOURS (default 2): log at WARN. Validator unhealthy: skip sweep, log reason. Container restart mid-sweep: reaper clears stale rows, next cron iteration re-runs. Per-game errors do not abort the batch. |
| 14 | **Game_shelf backend proxy routes** (promoted to MVP by Phase 0 OQ1 resolution, 2026-04-20) | In the `kraulerson/Game_shelf` repo: add `backend/src/routes/cache.js` mounted at `/api/cache` + `backend/src/services/orchestratorClient.js` (axios wrapper). Inject `ORCHESTRATOR_URL`, `ORCHESTRATOR_TOKEN`, `ORCHESTRATOR_TIMEOUT_MS` env vars. Proxy 10 read/write routes 1:1; `POST /api/cache/platforms/:name/auth` explicitly NOT proxied (OQ2). | 401 from orchestrator (token mismatch): log loudly in Game_shelf, return 502 with `{"error": "orchestrator_auth_mismatch"}`. Timeout/refused: return 503 with `{"status": "orchestrator_offline"}`. Orchestrator version mismatch: `/api/cache/health` exposes version; frontend shows soft warning. |
| 15 | **Game_shelf cache badge and cache panel** (promoted to MVP by OQ1) | New React components `CacheBadge.jsx` (inline on `GameCard.jsx`/`GameRow.jsx`) and `CachePanel.jsx` (in `pages/GameDetail.jsx`). 7 states (`cached`, `pending-update`, `downloading`, `missing`, `blocked`, `validation-failed`, `unknown`) each rendered with **color + icon + text label** to satisfy Intake §9 colorblind constraint. TanStack Query hooks in new `utils/cacheApi.js`. Single bulk fetch per library view (not N+1). | Orchestrator offline: library banner, badges render as `—`, mutations disabled. Mutation failure: toast + optimistic rollback. Cache row missing for a game: badge renders as `unknown`. Schema mismatch: tolerant merging (extra fields ignored, missing fields rendered as `—`). |
| 16 | **Game_shelf cache dashboard page** (promoted to MVP by OQ1) | New `pages/Cache.jsx` linked from `Nav.jsx`. Sections: overall stats, per-platform auth status with copy-pasteable CLI reconnect command, recent 25 jobs feed, block-list management (supports ≥500 entries without pagination). Polling: 10 s for stats/jobs, 60 s for platform status. | Orchestrator offline: full-page banner + skeleton sections. Individual section fetch failure: section-local error state; others unaffected. |
| 17 | **Orchestrator ↔ Game_shelf graceful degradation** (promoted to MVP by OQ1) | Explicit bidirectional offline handling: orchestrator keeps caching when Game_shelf is down (F10 status page + F11 CLI remain ops surface); Game_shelf keeps library UI functional when orchestrator is down (cache columns degrade only). Version skew surfaced via `/api/cache/health` with soft-warning banner in frontend. No automatic retry storms. | Network partition: each side's local workflow unaffected; polling reconciles on reconnect. Bearer token must NEVER reach Game_shelf frontend — CI grep enforces this. |

### 4.2 Should-Have Features (Post-MVP v1.1)

> **Updated 2026-04-20 by Phase 0 OQ1 and OQ7 resolutions:** Game_shelf integration promoted to MVP (now F14–F17). Scheduled full-library validation sweep promoted to MVP (now F13). Removed from this list. CLI `--json` added per OQ6.

1. SSE/WebSocket live progress stream for active prefill jobs.
2. Access-log tail for passive HIT/MISS observation and eviction detection.
3. Prometheus metrics endpoint for external monitoring.
4. Incremental validation — only re-validate games whose manifest changed since last validation (optimization on F13's full sweep).
5. CLI `--json` flag for machine-parseable output (deferred from Phase 0 OQ6, 2026-04-20).

### 4.3 Will-Not-Have Features (Explicit Exclusions)

1. Ubisoft Connect prefill — Lancache caching is broken upstream (monolithic issue #195, open 21+ months). No protocol library at production quality.
2. EA App prefill — hostile vendor, TLS-pinned sessions, only OSS effort (Maxima) is self-described "pre-pre-pre-alpha."
3. GOG prefill — GOG is not in `uklans/cache-domains`, CDN is HTTPS-only to Fastly, Lancache cannot intercept.
4. Game installation or launching — the orchestrator fills the cache, it does not install or run games.
5. Multi-user or multi-tenant support — single user, single Lancache instance.
6. A second React SPA dashboard inside the orchestrator — the rich UI lives in Game_shelf only. The orchestrator ships a single-file HTML status page and a CLI.
7. ORM (SQLAlchemy/Alembic) — raw SQL via aiosqlite with numbered migration files.

---

## 5. Data & Integrations

### 5.1 Data Inputs

| Input | Data Type | Validation Rules | Sensitivity | Required? |
|---|---|---|---|---|
| Steam credentials (username/password) | Text | Entered via CLI only, never stored in DB — session token persisted | Confidential | Yes (for Steam) |
| Steam Guard code | Text (OTP) | 5-digit alphanumeric, entered via CLI during auth | Confidential | Yes (first auth + re-auth) |
| Epic auth code | Text | One-time code from `https://legendary.gl/epiclogin`, entered via CLI | Confidential | Yes (first auth) |
| API bearer token | Text | Loaded from Docker secret file, ≥32 chars | Confidential | Yes |
| ~~Steam Web API key (optional fallback)~~ | ~~Text~~ | ~~Loaded from Docker secret file~~ | ~~Confidential~~ | **Removed from MVP by Phase 0 DQ1 resolution 2026-04-20. Post-MVP re-add if a "library-only, no CDN prefill" mode is ever requested.** |
| Block list entries | (platform, app_id) tuples | Platform must be 'steam' or 'epic'; app_id non-empty string | Internal | No |

**Sensitivity classifications:** Public, Internal, Confidential, PII, Financial, Health/Medical, Regulated

### 5.2 Data Outputs

| Output | Format | Latency Expectation |
|---|---|---|
| Game list with cache status | JSON via REST API | <500ms for full list (~2200 games) |
| Single game detail with validation history | JSON via REST API | <100ms |
| Platform auth status | JSON via REST API | <50ms |
| Active/recent jobs with progress | JSON via REST API | <100ms |
| Cache stats (disk usage, LRU headroom) | JSON via REST API | <200ms |
| HTML status page | Static HTML + JS polling API | Page load <1s; updates every 2s |

### 5.3 Third-Party Integrations

| Service | What Data We Send/Receive | Auth Method | Fallback if Unavailable | Existing Account? |
|---|---|---|---|---|
| Steam CM servers | Auth credentials → session token; PICS queries → owned apps/depots/manifests | Username/password + Steam Guard → refresh token | Mark platform `expired`; retry next cycle | Yes |
| Steam CDN | HTTP GETs for depot manifests and chunks (stream-discarded through Lancache) | Manifest request codes (5-min TTL) from CM session | Retry on 403 (refresh code); mark game `failed` on persistent error | N/A (public CDN) |
| Epic Games Services | OAuth auth code → access/refresh tokens; library API → owned games; catalog API → titles; manifest URLs → manifest data | OAuth2 bearer token | Mark platform `expired`; retry next cycle | Yes |
| Epic CDN | HTTP GETs for chunks (stream-discarded through Lancache) | Manifest URL includes auth token | Retry; mark game `failed` on persistent error | N/A (public CDN) |
| Lancache (local) | HTTP downloads routed through nginx proxy; disk-stat reads of cache directory | N/A (same Docker compose network) | Fatal — orchestrator cannot operate without Lancache. Startup self-test fails loudly. | Yes (running) |

### 5.4 Data Persistence

| Question | Answer |
|---|---|
| **What data must persist across sessions?** | SQLite database (games, manifests, block_list, validation_history, jobs, platforms, cache_observations). Platform session tokens (JSON files in `/var/lib/orchestrator/`). |
| **What data can be ephemeral?** | In-progress download state (httpx connections, semaphore state). APScheduler MemoryJobStore (single cron job re-registered on startup). |
| **Expected data volume at 12 months** | ~2200 game rows, ~2200 manifest rows (latest per game), ~50k validation_history rows, ~20k job rows. Total DB size: <100 MB. Manifest BLOB storage (compressed): ~500 MB. |
| **Data retention requirements** | Keep forever. Validation history and job history can be pruned to last 90 days if DB grows. |
| **Backup requirements** | Weekly SQLite backup via cron (`.backup` command). Session token files included. Stored on DXP4800 NAS alongside existing Proxmox backups. |

---

## 6. Technical Preferences

### 6.1 Orchestrator Technical Profile

| Field | Value |
|---|---|
| **Languages you know well** | Python (read/review), JavaScript/Node.js, Bash |
| **Frameworks you've used** | Express.js, React, Docker/Docker Compose |
| **Languages/frameworks you're willing to learn** | FastAPI (Python), Click (Python CLI) |
| **Languages/frameworks you refuse to use** | Java, PHP |
| **Database experience** | SQLite (via better-sqlite3 in Game_shelf), basic PostgreSQL |
| **DevOps experience level** | Advanced — manages Proxmox cluster, Docker, pfSense, MikroTik, Pi-hole, Cloudflare Tunnels |
| **Mobile development experience** | Some (React Native/Expo for Tender Reminders app) — not relevant to this project |

### 6.2 Competency Matrix

| Domain | Self-Assessment | Automated Tooling Required? |
|---|---|---|
| Product/UX Logic | Yes | No |
| Frontend Code (HTML/CSS/JS) | Yes | No |
| Backend / API Design | Yes | No |
| Database Design & Queries | Yes | No |
| Security (Auth, Injection, IDOR) | Partially | Yes — Semgrep + gitleaks already installed |
| DevOps / Infrastructure | Yes | No |
| Accessibility (WCAG) | Partially | Yes — but scope is limited (status page + CLI, not a full web app) |
| Performance Optimization | Partially | Yes — Spike F validates event-loop discipline under load |
| Mobile (iOS/Android) | N/A | N/A |

### 6.3 Development Environment

| Field | Value |
|---|---|
| **Primary development machine** | Mac mini (Apple Silicon) — primary Claude Code machine |
| **Secondary machines** | DXP4800 NAS (deployment target), Lenovo ThinkStation P360 Tiny (Proxmox node, Game_shelf host) |
| **IDE/Editor** | Claude Code (terminal-based) |
| **Docker available?** | Yes — Docker 29.3.1 via Colima |
| **Node.js version** | 25.9.0 (for Game_shelf integration only) |
| **Python version** | 3.12 (in container) |
| **Claude Code installed?** | Yes — 2.1.114 |
| **AI subscription tier** | Claude Max |

### 6.4 Architecture Preferences & Constraints

**All Platforms:**

| Field | Value | Hard Constraint or Preference? |
|---|---|---|
| **Primary language** | Python 3.12 | Hard constraint — steam-next and legendary are Python |
| **Data storage** | SQLite via aiosqlite, raw SQL, numbered migration files | Hard constraint |
| **Authentication** | Bearer token via Docker secret (API auth); platform-specific auth for Steam/Epic (CLI-driven) | Hard constraint |

**Web Applications:**

| Field | Value | Hard Constraint or Preference? |
|---|---|---|
| **Frontend framework** | N/A for orchestrator (single-file HTML status page). Game_shelf integration uses existing React/Vite stack. | Hard constraint |
| **Backend framework** | FastAPI | Hard constraint |
| **Hosting** | Self-hosted Docker container on DXP4800 NAS | Hard constraint |

**Desktop Applications:** N/A

**Mobile Applications:** N/A

**Cross-Cutting:**

| Field | Value | Hard Constraint or Preference? |
|---|---|---|
| **Monorepo or separate repos?** | Separate repo for orchestrator; Game_shelf integration is a PR to kraulerson/Game_shelf | Hard constraint |
| **Web + Desktop, Web + Mobile, or single platform?** | Single platform (Docker/Linux) | Hard constraint |

### 6.5 Existing Infrastructure to Integrate With

| System | Details | Integration Required? |
|---|---|---|
| **SSO / Identity Provider** | N/A | N/A |
| **Logging / SIEM** | structlog → JSON (in-container); optionally tail Lancache access.log | No |
| **Monitoring** | Status page + REST API. Prometheus endpoint is post-MVP. | No |
| **Data Warehouse** | N/A | N/A |
| **Backup Infrastructure** | Weekly SQLite backup via cron on DXP4800 | Yes — simple cron, not enterprise backup |
| **CI/CD Platform** | GitHub Actions | Yes |
| **Repository Platform** | GitHub (kraulerson) | Yes |
| **Lancache** | Docker container on DXP4800, same compose stack. Cache volume at `/data/cache`, logs at `/data/logs`. | Yes — core dependency |
| **Game_shelf** | Express/React app in LXC on ThinkStation (192.168.1.20). Consumes orchestrator REST API. | Yes — post-MVP integration (v1.1) |

---

## 7. Revenue Model (Standard+ Track — skip for internal tools)

N/A — personal project, no revenue model. Open-source (MIT or similar) if published.

---

## 8. Governance Pre-Flight (Organizational Deployments Only)

N/A — personal project.

---

## 9. Accessibility & UX Constraints

| Field | Value |
|---|---|
| **Accessibility requirements** | Minimal — status page should be readable and functional. No WCAG AA target for an internal diagnostics page. |
| **Color vision deficiency considerations** | Yes — the operator is colorblind. Never rely on color alone for meaning on the status page or in CLI output. Use text labels, shapes/icons, and position to convey status (cached, missing, blocked, etc.). This applies to Game_shelf integration components as well. |
| **Supported browsers** | Chrome (primary), Firefox, Safari. Status page is simple enough to work anywhere. |
| **Mobile responsive required?** | Nice-to-have for status page. Not required. |
| **Supported devices** | Desktop primarily. Status page viewable on tablet/phone is a bonus. |
| **Branding / style guide** | None — status page should be clean and functional. Game_shelf integration follows Game_shelf's existing Tailwind conventions. |
| **Dark mode required?** | Nice-to-have for status page. Game_shelf integration follows whatever Game_shelf supports. |

---

## 10. Distribution & Operations Preferences

**All Platforms:**

| Field | Value |
|---|---|
| **Notification preferences for alerts** | Logs only for MVP. Future: consider webhook/ntfy integration for auth expiry alerts. |
| **Uptime expectation** | Best effort. Container restarts automatically (`restart: unless-stopped`). |
| **Environment strategy** | Production only. Dev/test happens locally on the Mac mini. |

**Web Applications:** N/A (not a web app — Docker container with REST API)

**Desktop Applications:** N/A

**Docker/Container Distribution:**

| Field | Value |
|---|---|
| **Container registry** | GitHub Container Registry (ghcr.io) |
| **Image base** | Python 3.12-slim, multi-stage build |
| **Deployment** | `docker-compose.yml` alongside Lancache on DXP4800 |
| **Auto-update mechanism** | Manual `docker compose pull && docker compose up -d`. Watchtower or similar is a future consideration. |

---

## 11. Known Risks & Concerns

```
See the full risk register in lancache-orchestrator-brief.md (revision 3, §7) for 21
identified risks with impact, likelihood, and mitigations. Key risks summarized here:

R1: fabieu/steam-next is a single-maintainer, 3-star fork. If abandoned, Steam adapter
breaks. Mitigation: pin git SHA, fork into our namespace, keep .NET subprocess fallback
design on paper.

R4: Steam manifest request code has a 5-minute TTL. Long prefills for large games can
hit mid-run expiry. Mitigation: refresh per-depot code at 4.5 min, retry on 403.

R10: gevent monkey-patching in steam-next can clash with asyncio. Mitigation: isolate
steam-next in a dedicated thread, never call gevent from the event loop. Spike D validates
this before full build.

R13: Lancache cache eviction can invalidate previously-validated state. The orchestrator
may report "cached" for a game that was evicted. Mitigation: re-validate on schedule,
tail access.log for eviction signals, expose LRU pressure in stats endpoint.

Additional concern: the DXP4800 NAS has limited CPU (ARM-based). Python 3.12 + asyncio +
httpx should be lightweight, but the gevent thread for steam-next and disk-stat bursts
for validation need to be profiled on the actual hardware during spikes.

Reference document: ~/lancache-orchestrator-brief.md (revision 3)
```

---

## 11.5. Testing & Bug Tracking

| Field | Value |
|---|---|
| **Testing interval** | Every 2 features |
| **Bug tracking tool** | GitHub Issues |
| **Human tester count** | 1 (Karl) |
| **Beta tester coordination** | N/A — single tester |
| **Bug severity SLAs** | SEV-1: 24h / SEV-2: 7d / SEV-3: best effort |

---

## 12. Tooling Configuration

> Auto-generated by init.sh. Full machine-readable config: `.claude/tool-preferences.json`

**Resolved for:** Darwin / other / other / light track

### Installed
| Tool | Category | Version |
|---|---|---|
| Git | version_control | 2.50.1 |
| jq | json_processor | jq-1.7.1-apple |
| Node.js | runtime | 25.9.0 |
| Docker | containerization | 29.3.1 |
| Colima | containerization | 0.10.1 |
| GPG | commit_signing | 2.5.18 |
| Semgrep | SAST Scanner | 1.157.0 |
| gitleaks | Secret Detection | 8.30.1 |
| Snyk CLI | Dependency Scanner | 1.1304.0 |
| Claude Code | ai_agent | 2.1.114 (Claude Code) |
| Development Guardrails for Claude Code | dev_framework | ce9ec10 |
| Superpowers | claude_plugin | installed |
| Context7 MCP | mcp_server | configured |
| Qdrant MCP | mcp_server | configured |

---

## 13. Agent Initialization Prompt

```
You are the AI execution layer for a Solo Orchestrator project. I am the
Orchestrator. I define intent, constraints, and validation. You provide
architecture, code, and documentation within the constraints I set.

ATTACHED:
1. Project Intake Template (this document) — your primary constraint
2. Solo Orchestrator Builder's Guide v1.0 — your process reference
3. lancache-orchestrator-brief.md (revision 3) — technical evaluation
   and project brief with architecture, component design, data model,
   risk register, and delivery plan
4. No Platform Module — this is an "other" platform type (Docker container)

DOCUMENT RELATIONSHIP:
- The Intake is the DATA SOURCE. It contains decisions, constraints,
  requirements, and technical profile.
- The Builder's Guide is the PROCESS. It defines the phases, steps,
  quality gates, and remediation procedures you follow.
- The Brief is the TECHNICAL REFERENCE. Architecture decisions, protocol
  details, library evaluations, and the data model are already decided.
  Do not re-evaluate or second-guess them — build from them.

RULES:
- The Project Intake is the governing constraint. Do not suggest features,
  architectures, or tooling that contradict it.
- The Builder's Guide defines the phase-by-phase process. Follow it.
- The Brief defines the technical architecture. Follow it.
- If the Intake specifies a hard constraint, respect it absolutely.
- If the Intake specifies a preference, you may recommend against it with
  justification, but defer to my decision.
- If the Intake leaves a field as "no preference," make a recommendation
  based on the constraints and explain your reasoning.
- If the Intake leaves a field blank or incomplete, flag it immediately
  and ask for the specific missing information before proceeding past
  the step that requires it.
- For any domain where the Competency Matrix (Section 6.2) says "Partially"
  or "No," default to the most conservative, well-documented option and
  ensure automated validation tooling covers that domain.
- Do not add features not in the MVP Cutline (Section 4.1).
- Do not suggest dependencies without justification.
- Every feature must have tests before implementation.
- Flag any conflict between the Intake constraints and technical feasibility
  immediately — do not silently work around it.

ACCESSIBILITY:
Color vision deficiency: never rely on color alone for meaning. Use shape,
position, text labels, patterns, or icons. The operator is colorblind.

PROJECT TRACK: Light
PLATFORM: Other (Docker container with REST API + CLI)
TARGET PLATFORM: Linux (Docker on DXP4800 NAS)

BEGIN: Execute Phase 0, Step 0.1 using the "With Intake — Validation
Prompt" path from the Builder's Guide. Use Sections 2 and 4 of the
Intake as the primary data source. Generate the Functional Requirements
Document by expanding the business logic triggers and failure states.
Where the Intake is vague, make it specific and flag for review. Where
the Intake is contradictory, identify the contradiction and ask for
resolution. Where an implicit dependency is omitted (e.g., features that
require network access but it's not listed), flag it as a recommended
addition. Reference the Brief for technical details — do not re-derive
what's already documented there.
```

---

## Checklist Before Starting

- [x] Every field is filled in or explicitly marked N/A
- [x] Must-Have features all have business logic triggers (If X, then Y)
- [x] Must-Have features all have failure states defined
- [x] Will-Not-Have list has at least 3 items (has 7)
- [x] Data sensitivity classifications are assigned to all inputs
- [x] Competency Matrix is completed honestly
- [x] Budget constraints are realistic (not aspirational)
- [x] Timeline includes Orchestrator availability, not just calendar dates
- [x] For organizational deployments: N/A (personal project)
- [x] Success/failure exit criteria are defined and a decision-maker is named (self)
- [ ] This document has been saved as `PROJECT_INTAKE.md` in the project repository

---

## Document Revision History

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-04-02 | Initial release (template). |
| 1.1 | 2026-04-20 | Filled out for lancache_orchestrator project based on technical brief revision 3. |
