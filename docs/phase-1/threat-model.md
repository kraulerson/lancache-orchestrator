# Threat Model & Architecture Stress Test — lancache_orchestrator

**Phase:** 1
**Step:** 1.3
**Persona used:** Penetration Tester
**Generated from:** ADR-0001 (selected architecture) + `PRODUCT_MANIFESTO.md` + `lancache-orchestrator-brief.md` §7 + `docs/phase-0/data-contract.md`
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

---

## Pen Tester's Standing Rules

Before mapping threats, I assume what a hostile actor would assume:

1. **I will get LAN access.** Assume the trusted VLAN is not impenetrable. IoT devices get popped. Visitor Wi-Fi gets mis-segmented. A Pi-hole update ships with a backdoor. The "LAN-only" trust boundary is porous for ≤ 5 minutes per year. That's all I need.
2. **I will target the weakest service in the stack.** Lancache, Game_shelf, Pi-hole, MikroTik, pfSense — which is easiest to compromise first? Whichever it is becomes my pivot to the orchestrator.
3. **I will read code before attacking.** The orchestrator is open-sourced (per Intake §1) or will be. I pull the image, `cat` every Python file, grep for `getpass`, `hmac.compare_digest`, auth paths, bearer handling, file modes. I know what to target before I touch the network.
4. **Every data flow is an exfiltration route.** Outbound HTTP to Steam/Epic from the orchestrator container — can I get the orchestrator to exfiltrate its own session file through that? Logs to stdout → Docker → operator's machine — can I inject log lines that look like legitimate alerts to hide my activity?
5. **I am patient.** This orchestrator fires cycles every 6 hours. I will wait for my opportunity. I don't need to attack in real time.
6. **Concrete is the minimum bar.** If I cannot describe step 1, step 2, step 3 of an attack, it is not a threat yet — it is a wish.

---

## 1. Assets, Threat Actors, Trust Boundaries

### 1.1 Assets (what I am trying to reach)

| ID | Asset | Where it lives | Sensitivity | Why I want it |
|---|---|---|---|---|
| A1 | Steam CM refresh token | `/var/lib/orchestrator/steam_session.json` mode 0600, inside the container's state volume | Confidential | Authenticates as the operator to Steam; grants read access to their full owned library and any stored purchase history visible to their account |
| A2 | Epic OAuth access + refresh tokens | `/var/lib/orchestrator/epic_session.json` mode 0600 | Confidential | Same for Epic, plus any launcher-public-service scope leakage |
| A3 | Bearer API token | Docker secret `/run/secrets/orchestrator_token` | Confidential | Full authenticated access to every orchestrator API endpoint |
| A4 | SQLite DB (`state.db`) | `${STATE_DIR}/state.db`, WAL + SHM files alongside | Internal | Library enumeration, job history, block list — doxes the operator's gaming habits |
| A5 | Lancache cache volume | `/data/cache` bind mount (read-only for orchestrator) | Public (content itself) | Not valuable to steal but valuable to corrupt or evict |
| A6 | FastAPI endpoints on port 8765 | Inside the container, exposed at `<dxp4800>:8765` on the trusted VLAN | Mixed | Primary control plane — a successful auth lets me do everything Game_shelf can |
| A7 | Game_shelf's `.env` with `ORCHESTRATOR_TOKEN` | `/opt/game_shelf/.env` (or wherever it deploys) on the ThinkStation LXC | Confidential | Cheapest path to A3 if Game_shelf is easier to breach than DXP4800 |
| A8 | The operator's Steam account, indirectly | Steam's servers — not ours | Confidential | If I can make the orchestrator talk to my-Steam-CM, I can hijack |

### 1.2 Threat actors

| ID | Actor | Position | Capabilities |
|---|---|---|---|
| T-EXT-LAN | Untrusted LAN user | Guest Wi-Fi, IoT device, compromised printer, neighbor who broke WPA2 | Network-level access to DXP4800 if firewall rule is misconfigured |
| T-VLAN | Trusted VLAN attacker (post-compromise) | Has breached one device on the trusted VLAN (most likely Game_shelf's host, Pi-hole, or another Docker service on DXP4800) | Unlimited network access inside the VLAN; can reach port 8765 if VLAN contains it |
| T-COMPOSE | Intra-compose-network peer | Compromised sibling container (e.g., Lancache itself, or a later-added container) | Direct TCP reach to orchestrator service by compose service name |
| T-HOST | DXP4800 host compromise | Root on the NAS | Game over; I exfiltrate session files, read the secret, read the DB, MITM compose traffic |
| T-DEP | Supply chain / compromised dependency | Patch in `fabieu/steam-next`, `legendary`, `httpx`, `fastapi`, `uvicorn`, or a transitive dep | Code execution inside the orchestrator process on next `docker compose pull` |
| T-UPSTREAM | Malicious upstream CDN or MITM on WAN | Controls or intercepts `*.steamcontent.com` or `*.epicgames.com` | Serves poisoned manifests or chunks |
| T-PHYS | Physical access | Stolen DXP4800 | Everything |

### 1.3 Trust boundaries (where something becomes less trusted crossing it)

1. **WAN → LAN.** pfSense separates them. Steam/Epic CDN traffic comes inbound through DNS redirection + Lancache.
2. **LAN → Trusted VLAN.** pfSense rule; port 8765 is restricted to trusted VLAN (R19 mitigation).
3. **Trusted VLAN → DXP4800 host.** SSH-key-only, non-root operator account; sudo for Docker.
4. **DXP4800 host → Docker compose network.** Host has Docker socket; compose network is one more layer.
5. **Compose network → orchestrator container.** Process isolation. Container runs as non-root user (Phase 2 decision per ADR-0001 consequences).
6. **Orchestrator container → state volume / cache volume.** File mode 0600 on session files; state volume is writable, cache volume is read-only bind mount.
7. **Orchestrator container → Steam/Epic APIs (egress).** TLS; library-enforced.
8. **Orchestrator container → Lancache (egress, intra-compose).** Plain HTTP over compose network. Host-header override.

Each boundary is a potential kill-chain step; each chain is an opportunity to detect.

---

## 2. STRIDE Threats

All threats are numbered `TM-###` for stable Phase 2.4 and Phase 3.2 traceability. Each threat references a specific component or data flow — not a generic OWASP category.

### 2.1 Spoofing (S)

**TM-001 — Bearer-token leak via Game_shelf `.env`**
- **Component / flow:** A7 → A6 (Game_shelf-to-orchestrator proxy auth).
- **Attack:** I compromise a vulnerable npm dep in Game_shelf (a likely weakest link; npm supply chain is a high-churn target). I read `.env`, obtain `ORCHESTRATOR_TOKEN`. I send authenticated requests to `http://dxp4800.local:8765/api/v1/*` from my compromised ThinkStation foothold.
- **Impact:** I can block/unblock games, enumerate the operator's library, trigger unbounded prefills to DoS Lancache or the WAN link.
- **Mitigation (concrete controls):**
  - The token is distributed as a **Docker secret** on DXP4800 (mode 0400 inside container) and as an env var only on the Game_shelf host (F14). It is never in a committed file.
  - Game_shelf's `.env` must be mode 0600 owned by the Game_shelf LXC user — documented in Phase 4 HANDOFF.md.
  - **F17 invariant** (CI grep): bearer token must NEVER appear in any file under `frontend/` in the Game_shelf repo. CI job rejects PRs that match the token's SHA256 prefix in the frontend build output.
  - Token rotation procedure documented in Phase 4 HANDOFF.md: change Docker secret on DXP4800 → update Game_shelf env var → restart both → stale tokens return 401.
  - pfSense rule restricts port 8765 to the trusted VLAN (R19); LAN-wide reach requires first compromising a trusted-VLAN device.

**TM-002 — Compose-network peer spoofs `http://lancache:80`**
- **Component / flow:** A6 → Lancache (orchestrator's egress to cache).
- **Attack:** T-COMPOSE (a compromised sibling container on the compose network) starts its own HTTP server and arranges for the orchestrator to resolve `lancache` to its IP instead of Lancache's. Practically requires control over Docker's embedded DNS or a network alias — not trivial. If succeeds, it serves fake chunks.
- **Impact:** Orchestrator writes "cached" status for content that is actually garbage; validator disk-stat passes because chunks are stored under correct keys (attacker writes valid chunks). However the operator's games fail to install (bytes don't match manifest hashes).
- **Mitigation:**
  - The startup self-test (ID2, F7) issues a HEAD to Lancache and asserts `X-LanCache-Processed-By` header is present. A spoofed peer that forgets to add this header fails the self-test.
  - Containers on the compose network run with well-known names; `docker compose ps` output is expected-state-checked in Phase 4 ops runbook.
  - No post-launch sibling containers should be added to Lancache's compose without explicit review.
  - Long-term hardening (Post-MVP): pin Lancache by IP after DNS resolution at startup, ignore subsequent DNS changes.

**TM-003 — DNS poisoning of `lancache.steamcontent.com`**
- **Component / flow:** Game clients on the LAN → Lancache. (Orchestrator does NOT use this DNS path per ADR-0007.)
- **Attack:** Attacker poisons Pi-hole or MikroTik DHCP to redirect `lancache.steamcontent.com` to attacker-controlled IP. Game clients download game content from the attacker.
- **Impact:** Not directly an orchestrator compromise, but compromises the system's purpose. Game clients install malicious binaries.
- **Mitigation:**
  - Pi-hole and MikroTik are operator-managed; their compromise is out of scope for the orchestrator.
  - The orchestrator's disk-stat validator **detects this indirectly:** if attacker's chunks are served to game clients but never reach Lancache's disk, the orchestrator never sees them; validation continues to show "cached" based on prior legitimate prefills — however, eviction pressure would eventually surface as `validation_failed`.
  - Documented in Phase 4 ops runbook: Pi-hole/MikroTik hardening is a LAN-level concern.

### 2.2 Tampering (T)

**TM-004 — Session-file tampering on the host**
- **Component / flow:** A1/A2 (session files in state volume) → orchestrator startup auth.
- **Attack:** T-HOST writes a valid-looking but attacker-controlled refresh token into `steam_session.json`. On next container start, the orchestrator authenticates to *attacker's* Steam account, enumerates attacker's library, caches attacker-chosen games (could be used to seed a malicious library).
- **Impact:** Orchestrator is now controlled indirectly through attacker's Steam/Epic account. Cache fills with attacker-selected games. Operator's legitimate library stops syncing.
- **Mitigation:**
  - File mode 0600, owned by the non-root container user.
  - State volume only mounted into the orchestrator container.
  - On host compromise, game over — this is not defended against at the orchestrator layer.
  - **Detection:** `auth_expires_at` stored in `platforms.*` row — on next startup, compare to claimed-new-token's expiry. If dramatically different, log WARN. (Weak, but a breadcrumb.) Not MVP hard requirement; track as a Phase 3 hardening idea.

**TM-005 — SQL injection through API path params**
- **Component / flow:** A6 → A4 (REST handlers → DB queries).
- **Attack:** Send `GET /api/v1/games/steam/' OR '1'='1` hoping path-param ends up concatenated into a SQL WHERE.
- **Impact:** If vulnerable, arbitrary read of DB including other platform rows, job errors, validation history. For this system, contents are Internal-not-Confidential, so impact is limited but still a clear bug.
- **Mitigation:**
  - **Pydantic path-param validation:** `platform` restricted to `Literal['steam', 'epic']`; `app_id` matches `^[A-Za-z0-9_\-]{1,64}$`. Requests violating the schema → 422 before the handler runs.
  - **`aiosqlite` parameterized queries only.** CI lint rule (Semgrep) rejects `execute(f"..."` / `execute("..." + var)` / string-formatting in SQL.
  - Negative test per endpoint: injection payload → 422, never 200 with unexpected data.

**TM-006 — Chunk-body MITM in compose network**
- **Component / flow:** A6 egress → Lancache (plain HTTP on compose network).
- **Attack:** T-COMPOSE intercepts chunk GETs and returns garbage bytes. Orchestrator stream-discards, so bytes aren't validated in memory. Lancache is the one actually caching — the attacker would have to get between orchestrator and Lancache, which means controlling nginx or the Docker network.
- **Impact:** If attacker controls Lancache directly, they can write garbage to the cache volume under any cache key. Game clients download garbage.
- **Mitigation:**
  - Compose network isolation: `docker network ls` shows only services from Lancache's compose file.
  - Lancache itself is a trusted peer — its compromise compromises the whole system regardless of what the orchestrator does.
  - Cache-content verification at install time is the responsibility of the game client (Steam / Epic launcher validate chunk hashes before install). Orchestrator is not the authority on whether bytes are correct; it only tracks whether files of the expected size are present at the expected paths.

**TM-007 — Poisoned manifest from upstream CDN**
- **Component / flow:** Orchestrator → Steam/Epic CDN (via Lancache).
- **Attack:** T-UPSTREAM serves a manifest claiming a game is 10 TB with 10 million chunks, each a URL pointing to attacker's domain. Orchestrator enters a fetch loop and either DoSs itself, Lancache, or attempts to fetch attacker-chosen URLs.
- **Impact:**
  - **Resource exhaustion:** manifest size cap (128 MiB per DQ7) bounds parsing memory.
  - **Fetch-loop:** `PER_PLATFORM_PREFILL_CONCURRENCY=1` and `PREFILL_CONCURRENCY=32` cap concurrency; a 10M-chunk fetch would take weeks and fail per-chunk first. Operator observes "job never completes" and intervenes.
  - **External URL fetch:** chunk URLs are constructed by the orchestrator as `http://{lancache_host}/...` — the URL path comes from the manifest but the host is always Lancache. A malicious manifest path could expose an SSRF vector within Lancache's nginx config (e.g., `../admin`), but Lancache is hardened upstream and its config is read-only from our side.
- **Mitigation:**
  - `MANIFEST_SIZE_CAP_BYTES = 128 MiB` (DQ7) hard limit.
  - Chunk URLs are constructed with the hostname pinned to Lancache via compose name; the manifest only supplies the path.
  - Path validation: chunk paths must match `^/[A-Za-z0-9/_\-.]+\.chunk$` for Epic or `^/depot/[0-9]+/chunk/[0-9a-f]{40}$` for Steam. CI-tested with malicious paths.
  - Per-chunk 10 s read timeout ensures a stalling response doesn't hold the semaphore forever.

### 2.3 Repudiation (R)

**TM-008 — Operator denies blocking a game**
- **Component / flow:** A4 `block_list` table.
- **Attack:** Not an outside attacker — this is the repudiation scenario where the operator later disputes an action.
- **Impact:** Single-user system, so impact is low — but diagnosis becomes harder.
- **Mitigation:**
  - `block_list.source` records `'cli' | 'gameshelf' | 'api' | 'config'` per row.
  - `block_list.blocked_at` timestamp recorded at `CURRENT_TIMESTAMP`.
  - Structured log entry at every block/unblock with correlation ID, source, and reason.
  - Phase 4 log retention: operator-configured Docker logging driver retains logs for ≥ 30 days.

**TM-009 — Scheduler activity denied**
- **Component / flow:** APScheduler → `jobs` table.
- **Attack:** Operator claims "the sync didn't run." Without audit, no proof.
- **Impact:** Minor — single-user.
- **Mitigation:**
  - `jobs` rows persisted with `source='scheduler'`, `started_at`, `finished_at`, `state`, `error`.
  - `sync_cycle_complete` log line per cycle with counts.
  - `/api/v1/platforms.*.last_sync_at` is a positive observation.

### 2.4 Information Disclosure (I)

**TM-010 — Bearer-token leak via frontend**
- **Component / flow:** F15/F16 (Game_shelf frontend) → browser → attacker.
- **Attack:** A developer accidentally passes `ORCHESTRATOR_TOKEN` to the frontend (e.g., baked into a bundler's environment expansion, or sent in a response body). Attacker with any user-agent access (browser extension, DevTools, proxy) reads it.
- **Impact:** Full API access → A3 compromised.
- **Mitigation (concrete controls):**
  - **F17 CI invariant:** grep the Game_shelf frontend bundle for the token's SHA256-prefix during CI; if found, fail the build. (Token itself won't match literal grep, but SHA256-prefix of test fixtures would catch the pattern.)
  - **F14 architectural invariant:** the Express backend is the ONLY place that sees the token. All proxy calls to the orchestrator inject `Authorization` server-side.
  - Frontend `fetch()` calls go to `/api/cache/*` on the Game_shelf origin, never to `<dxp4800>:8765` directly. Backend strips any `Authorization` header from incoming frontend requests before forwarding (defense in depth).
  - No `process.env.ORCHESTRATOR_TOKEN` reference in any `frontend/` file — CI grep.

**TM-011 — Stack-trace disclosure in 500 responses**
- **Component / flow:** FastAPI exception handlers.
- **Attack:** Send malformed input chosen to trigger a Python exception inside the handler. Read the response body for stack trace (file paths, Python versions, library versions — useful for targeted CVE exploitation).
- **Mitigation:**
  - FastAPI `exception_handler` middleware catches all `Exception` subclasses and returns `{"error": "internal_error", "correlation_id": "..."}`. **Never** the traceback.
  - Full traceback logged at ERROR with correlation ID.
  - Negative test: handler that `raise ValueError("secret key = abc")` returns response without the string `"secret"`.

**TM-012 — Log-stream credential leak**
- **Component / flow:** structlog → stdout → Docker → operator machine.
- **Attack:** Accidental logging of `steam_session.json` contents, bearer token, or Steam password during an exception path.
- **Mitigation:**
  - **Convention:** all credential logs use `token_sha256_prefix=<first 8 hex>` format, never the raw value.
  - **Semgrep rule:** pattern-match `log.*(password|refresh_token|auth_code|orchestrator_token)[^_]` in source. Fails CI if matched.
  - Test: raise in auth flow with token in locals; verify ERROR log entry does not contain token substring.
  - `structlog.processors.format_exc_info` is on — tracebacks go to logs — but local-variable inspection (`exception.__traceback__.tb_frame.f_locals`) is NOT in our processor chain, so local variables don't get logged.

**TM-013 — Public `/api/v1/health` fingerprinting**
- **Component / flow:** F9 health endpoint.
- **Attack:** Unauthenticated `GET /api/v1/health` returns `{"version": "...", "git_sha": "..."}` — attacker identifies the exact orchestrator version and searches for known CVEs in its dependencies.
- **Mitigation:**
  - For LAN-only single-user, low priority.
  - Phase 3 hardening: make `git_sha` conditional on bearer auth; leave `version` + `status` unauthenticated for Game_shelf health checks.
  - CVE monitoring via Snyk CLI + Dependabot on the repo.

**TM-014 — DB file readable on host compromise**
- **Component / flow:** A4 on host filesystem.
- **Attack:** T-HOST reads `state.db` + WAL + SHM files.
- **Impact:** Game library metadata + job history + block list exposed. No credentials (those are in session files + Docker secrets, addressed elsewhere).
- **Mitigation:**
  - Container runs as non-root user; state volume mode 0700 owned by that user.
  - On host compromise, game over — this is not a defense-in-depth layer we add.

### 2.5 Denial of Service (D)

**TM-015 — Connection-pool exhaustion on `/api/v1/games`**
- **Component / flow:** F9 API layer.
- **Attack:** T-VLAN sends thousands of concurrent requests to `/api/v1/games`, exhausting uvicorn's connection pool or aiosqlite's connection pool.
- **Mitigation:**
  - LAN-only trust boundary; pfSense rule restricts port 8765 to trusted VLAN.
  - uvicorn default `limit_concurrency` sized appropriately (Phase 2 decision, target 256).
  - aiosqlite pool size configured to 10 concurrent connections; requests queue beyond that.
  - Phase 3 hardening: rate-limit middleware on `GET /api/v1/*` — e.g., 100 req/sec per client IP. Not MVP.

**TM-016 — Prefill-triggered WAN/Lancache DoS**
- **Component / flow:** F5/F6 prefill path triggered from API.
- **Attack:** Attacker with A3 enqueues `POST /api/v1/games/steam/*/prefill` on the largest games to saturate Lancache's upstream bandwidth.
- **Impact:** Lancache's WAN link saturates; legitimate traffic stalls.
- **Mitigation:**
  - `PER_PLATFORM_PREFILL_CONCURRENCY=1` caps in-flight prefills.
  - Concurrent-job dedupe: POST-prefill on an already-running game returns 409, doesn't queue a parallel one.
  - Phase 3 hardening: rate-limit mutations (e.g., 1 prefill POST per minute), explicit `force` flag to bypass.

**TM-017 — Scheduler death via malformed cron**
- **Component / flow:** APScheduler registration from env.
- **Attack:** Operator or config-management sets `SCHEDULE_CRON` to garbage. APScheduler crashes on init.
- **Mitigation:**
  - `pydantic-settings` validates `SCHEDULE_CRON` at load using `APScheduler.util.obj_to_ref` + cron-parser; fails-fast with clear error before uvicorn starts.
  - Scheduler health surfaced in `/api/v1/health` (JQ3).

**TM-018 — Memory exhaustion via oversized manifest**
- **Component / flow:** F5/F6 manifest fetch.
- **Attack:** T-UPSTREAM returns a 10 GB manifest response. Orchestrator attempts to buffer.
- **Mitigation:**
  - `MANIFEST_SIZE_CAP_BYTES = 128 MiB` (DQ7). Enforced by reading in a size-bounded loop; abort + log `upstream_manifest_oversize` + mark job failed.
  - Streaming parse (don't read entire response into memory before parsing).

### 2.6 Elevation of Privilege (E)

**TM-019 — Container escape via a Python CVE**
- **Component / flow:** Orchestrator process → container → host kernel.
- **Attack:** RCE in an upstream library (httpx, uvicorn, Python itself) + known escape technique in Docker runtime.
- **Mitigation:**
  - Container runs as non-root user (`USER orchestrator` in Dockerfile, UID 1000).
  - Dockerfile sets `security_opt: [no-new-privileges:true]` in compose.
  - Root filesystem read-only with tmpfs for `/tmp` and writable bind mount only for `${STATE_DIR}`.
  - `cap_drop: [ALL]` in compose; add back only `NET_BIND_SERVICE` if needed (8765 is > 1024 so not needed).
  - Snyk CLI dependency audit CI-gated.
  - Dependabot on the repo flags CVEs in Python deps within 24 h.

**TM-020 — Supply-chain compromise (multi-step chain)**
- **Component / flow:** Upstream `fabieu/steam-next` (R1) or transitive dep → `docker compose pull` → orchestrator process → A1.
- **Attack chain:**
  1. Attacker submits a PR to `fabieu/steam-next` that adds innocuous-looking code; maintainer (single person) merges.
  2. Attacker waits for the orchestrator to bump the pinned SHA.
  3. On next `docker compose pull`, the new image includes the compromised steam-next.
  4. Malicious code inside the orchestrator process reads `/var/lib/orchestrator/steam_session.json`, exfiltrates the refresh token via an outbound HTTP to attacker-controlled `lancache-stats.example.com` (the name and TLS cert are crafted to look legitimate).
  5. Outbound request passes through Lancache (attacker chooses a `Host:` that matches Lancache's allow-list) or directly to WAN.
  6. Attacker now has A1 and can authenticate as the operator to Steam.
- **Mitigation:**
  - **OQ4 fork policy:** steam-next is monitored weekly; upstream silence >15 days triggers immediate fork to `kraulerson/steam-next`. 15 days is short enough to deny a casual-takeover attacker.
  - **SHA pinning:** `requirements.txt` pins by git SHA, not branch. A PR that modifies the requirements file shows the SHA change explicitly in diff — human review catches unplanned bumps.
  - **Snyk CLI** dep audit CI-gated; flags known-malicious packages.
  - **Egress restriction (Phase 3 hardening):** container `network_mode` limited to compose network + WAN; no direct DNS to arbitrary hosts. Harder than it sounds in Docker, but `iptables` egress rules at the host level are feasible.
  - **Outbound monitoring (Phase 4):** Lancache access logs already show every egress by host. Unusual destinations (non-Steam, non-Epic, non-Lancache) in the orchestrator's container logs stand out. Covered by FG3 notification in Post-MVP.

**TM-021 — CLI argument injection**
- **Component / flow:** F11 CLI → local API.
- **Attack:** `orchestrator-cli game steam/"; rm -rf /; "` — if the CLI shell-interpolates arguments.
- **Mitigation:**
  - **Click** parses args as structured values (typed parameters), not shell strings.
  - No `subprocess.run(shell=True)` anywhere in CLI. CI lint rule (Semgrep) rejects `shell=True`.
  - No string-interpolated URL construction — URLs built with `httpx.Request(..., url=f'.../{platform}/{app_id}')` where `platform` and `app_id` are validated by Click first.

**TM-022 — Setuid escalation in image**
- **Component / flow:** Orchestrator container user → root inside container.
- **Attack:** If the image contains a setuid binary (left over from a base image), exploit a known setuid binary vulnerability to escalate.
- **Mitigation:**
  - Python 3.12-slim base image is audited; CI step `find / -perm -u=s -type f` and fail build if any setuid binary is present that isn't explicitly allowlisted. Most -slim images have none.
  - `security_opt: [no-new-privileges:true]` prevents setuid-effective escalation even if a setuid binary exists.

### 2.7 Multi-step Attack Chain (required by Builder's Guide checklist)

**TM-023 — Credential theft via compromised Game_shelf LXC**

This is the full chain I would execute given the assets and actors listed above:

1. **Recon** (1 day). Port-scan the trusted VLAN from my initial foothold (say, a compromised Roomba on guest Wi-Fi that later got promoted to trusted VLAN by mistake). I identify: DXP4800 with 8765 open, ThinkStation with 3001 (Game_shelf backend) and 80 (Game_shelf frontend). I pull Game_shelf's publicly-documented code from GitHub (it's open source) and note it uses Express + Node.js + React with TanStack Query.
2. **Vulnerability identification** (2 days). I check Game_shelf's `package.json` for known-vulnerable deps. One transitive dep has a known prototype-pollution RCE from 3 months ago that hasn't been patched in a published Game_shelf release.
3. **Exploit Game_shelf** (1 hour). I craft a request to Game_shelf's backend that triggers the prototype pollution and gets me shell-equivalent access inside the LXC. I confirm by writing a tiny beacon to `/tmp/pwn`.
4. **Harvest A7** (5 minutes). I `cat /opt/game_shelf/.env` and read the `ORCHESTRATOR_TOKEN` and `ORCHESTRATOR_URL` values (e.g., `http://dxp4800.local:8765`).
5. **Pivot to A6** (immediate). From the Game_shelf LXC (which is on the trusted VLAN), I send an authenticated GET to `/api/v1/platforms` using the stolen bearer. Response reveals both platforms with `auth_status: "ok"` and recent sync timestamps.
6. **Library dox** (immediate). I GET `/api/v1/games` with the same bearer and receive the operator's full library — 2,600 games. I cross-reference against their public Steam profile (visible at `steamcommunity.com/id/their-handle`). I now know their real identity, their playtime, their social graph.
7. **Disruption**. I `POST` prefill requests on the operator's largest games (Cyberpunk 2077, Baldur's Gate 3) in a loop through the authenticated API. This saturates the WAN link for anyone on the home network. If I repeat with POST /block on every owned game, I destroy their prefill workflow.
8. **Persistence**. I `POST /api/v1/games/steam/*/block` with `reason="scheduled maintenance"` to make it look legitimate. When the operator investigates, log entries show `source=api` blocks — but no host-level IP logging tells them which client performed the POST unless they correlate with pfSense flow logs (which they probably don't).

**Per-step mitigations:**
- Step 2: Game_shelf's CI must include `npm audit --audit-level=high` with a commit-blocking threshold; Dependabot PRs must be merged within 14 days.
- Step 3: Game_shelf's auth middleware should rate-limit failed-auth attempts.
- Step 4: `ORCHESTRATOR_TOKEN` stored via the LXC user's secret manager (e.g., systemd `LoadCredential=`) rather than `.env`. Beyond MVP scope but in Phase 4 HANDOFF.
- Step 5: pfSense rule further restricts port 8765 not just to VLAN but to specific hosts (Game_shelf's IP) — removes reach from other trusted-VLAN devices.
- Step 6: the orchestrator has no defense against a valid-bearer-token request. This is the weakest link.
- Step 7: concurrent-job dedupe returns 409 on already-running prefills, preventing the loop attack's amplification. POST rate-limit in Phase 3.
- Step 8: orchestrator logs include `source` and correlation_id but NOT the client IP. Phase 3 hardening: FastAPI access-log middleware logs `request.client.host` per request, joined to `correlation_id` at ERROR level for auditability.

**Kill chain summary:** TM-023 makes Game_shelf the pivot and `.env` the primary secret to protect. Mitigation priorities in order of impact: (a) Game_shelf auth + dep hygiene, (b) `.env` handling, (c) orchestrator's access logs with client IPs, (d) pfSense host-specific rules, (e) mutations rate-limit.

---

## 3. STRIDE Coverage Table

| STRIDE | Threats | Mitigation status |
|---|---|---|
| Spoofing | TM-001, TM-002, TM-003 | Mitigated by architecture + Phase 4 ops docs; TM-003 partially out of scope |
| Tampering | TM-004, TM-005, TM-006, TM-007 | Mitigated at code + compose layer; TM-004 residual risk on host compromise |
| Repudiation | TM-008, TM-009 | Mitigated by `source` column + structured logs + DB timestamps |
| Information Disclosure | TM-010, TM-011, TM-012, TM-013, TM-014 | Mitigated by F17 invariants + Semgrep rules + Phase 3 hardening for TM-013 |
| Denial of Service | TM-015, TM-016, TM-017, TM-018 | Bounded by LAN trust + concurrency caps + manifest size cap; Phase 3 adds rate-limiting |
| Elevation of Privilege | TM-019, TM-020, TM-021, TM-022 | Mitigated by container hardening + CLI framework choice + SHA pinning + fork policy |
| Multi-step chain | TM-023 | Requires Game_shelf-side dep hygiene + pfSense host-specific rules + Phase 3 access logs |

---

## 4. Architecture Stress Test

### 4.1 Five edge cases where the stack would fail

1. **`MemoryJobStore` cron loss after partial startup.** If the container restarts mid-startup (e.g., OOM kill after DB migration but before scheduler registration), APScheduler never re-registers the cron. Cycle silently stops. `/api/v1/health` would show `scheduler_running: true` if the watchdog is per-thread; if per-scheduler-instance, would show `false`. Trigger: restart during first 1–3 s of boot, with no successful prior completion to the ready log line.
2. **WAL contention during F13 × F12 overlap.** F13 weekly sweep writes to `validation_history` for all 2,600 games over ~30 min. F12 daily cycle writes to `jobs` + `games` during the same window. With WAL + `aiosqlite` default `DEFERRED` transactions, readers don't block writers, but two writers can collide and produce `SQLITE_BUSY` retries. Trigger: F13 Sunday-3AM cron overlaps with F12's 06:00 tick (if F13 runs long).
3. **`httpx` file-descriptor exhaustion.** DXP4800 runs with `ulimit -n 1024` by default in some Docker configurations. 32 concurrent chunk connections × 2 (TCP + socket accounting) + uvicorn's listener + aiosqlite's fd × 10 + logs + misc ≈ 100 fds. Acceptable for 1024, but a leaked `httpx.AsyncClient` instance (created-not-closed) accumulates connections over hours. Trigger: adapter code that spawns a new `AsyncClient` per request instead of reusing a module-level client.
4. **Gevent monkey-patch leaks to main thread.** `gevent.monkey.patch_minimal()` is called on the dedicated Steam thread. But Python's `socket` module is imported at process startup; if `steam-next` imports the pre-patch `socket` lazily on first use in the main thread, it may capture the un-patched socket globally. Trigger: a test suite that does cross-thread socket operations for the first time after the Steam thread has started.
5. **Steam CM session hang on OAuth device-code flow.** If Steam changes auth flow mid-cycle (e.g., server-side migration to device codes), `steam-next` may hang for 120 s on a prompt that never comes. The CLI has its own 120 s timeout, but the scheduled sync would hang until `orchestrator-cli auth steam` is re-run. Trigger: Steam adds or removes a 2FA mechanism without updating steam-next.

### 4.2 Three security vulnerabilities inherent to this design

1. **Discipline-bound event-loop hygiene.** Accidentally importing `requests` or calling `time.sleep()` on the main loop blocks all API traffic. The lint rule + PR review is a process control, not a runtime control. Runtime detection (e.g., `asyncio.get_event_loop().set_debug(True)` in dev) is an extra safety but not foolproof. *Inherent to Option A's choice of mixing frameworks in one process.*
2. **Single bearer token scope.** The entire API shares one token. Compromising it grants all capabilities — no separation between read-only (e.g., Game_shelf's library view) and write (e.g., block/unblock). A compromised Game_shelf host has the same privilege as a compromised admin session. *Inherent to the LAN-only single-user simplification.*
3. **Upstream dependency trust.** `fabieu/steam-next` (3-star fork, single maintainer) is a code path inside the orchestrator's process. Vendoring `legendary` reduces but does not eliminate this for Epic. The `15-day fork policy` (OQ4) reduces the window but doesn't prevent a single malicious release from being pulled before anyone notices. *Inherent to reusing community Python libraries for proprietary game protocols — no vendor SDK exists.*

### 4.3 Two data-storage bottleneck risks with trigger conditions

1. **`manifests.raw` BLOB growth → slow `VACUUM`.** At 2,600 games × ~200 KB × 3 retained versions per game ≈ 1.56 GB BLOB across one table. SQLite `VACUUM` rewrites the entire DB; at 1.5 GB on spinning rust (DXP4800 HDD array with SSD cache), VACUUM could take several minutes and holds an exclusive lock. **Trigger:** first monthly VACUUM after 12 months of operation. **Contingency:** if VACUUM exceeds 5 s, re-open DQ3 — move `manifests.raw` to external `${STATE_DIR}/manifests/*.bin.zst` files with DB rows holding only paths.
2. **`jobs` + `validation_history` table growth without pruning.** At 2,600 games × 8 jobs/month + 15 validation_history rows/game/month × 12 months = ~340,000 rows each. SQLite handles this easily, but `SELECT ... ORDER BY started_at DESC` without proper indexes becomes slow at 1M+ rows. **Trigger:** pruning step (90-day retention) is implemented but not wired to a scheduled task; rows accumulate beyond design. **Contingency:** prune cron built into F12 cycle (enqueue deletes alongside prefills). Add `idx_jobs_started_at DESC` index in `0001_initial.sql`.

### 4.4 One limitation that could force a rewrite in 12 months

**If both Option A (Spike F fails) and Option B (subprocess isolation proves inadequate on ARM hardware) cannot deliver the API responsiveness target under sustained prefill load, Option C (multi-container split) would be required — a rewrite that crosses the Intake's hard single-container constraint.** This is low-probability (ARM DXP4800 has enough cores for 32-concurrent HTTP I/O at ≥300 Mbps per Spike E), but it's the one rewrite risk that is not mitigated by incremental refactor.

Secondary concern: if the operator ever opens this to multi-user (e.g., family members with separate Steam accounts), the single-bearer-token + single-user data model is structurally wrong. Conversion would be a schema migration plus an auth rewrite — not 12 months of pain but meaningful work. Current scope explicitly rejects multi-user, so this is tracked, not mitigated.

---

## 5. Risk / Mitigation Matrix

Cross-reference for Phase 2.4 security audits and Phase 3.2 verification.

| Threat ID | Asset at risk | Mitigation location | Who enforces | Phase 3 verification |
|---|---|---|---|---|
| TM-001 | A3, A7 | Game_shelf `.env` mode 0600; F17 CI grep | Orchestrator code + Game_shelf CI + operator | Simulate compromised Game_shelf host; confirm token not in frontend bundle; confirm pfSense blocks non-Game_shelf hosts |
| TM-002 | A5, A6 | Startup self-test `X-LanCache-Processed-By` check | Orchestrator boot code | Simulate mis-named sibling container; confirm self-test fails |
| TM-003 | LAN clients | Pi-hole/MikroTik operator responsibility | Operator | Out of orchestrator scope; document in HANDOFF |
| TM-004 | A1, A2 | File mode 0600; non-root container user | Dockerfile + app boot | Audit state-volume permissions in running container |
| TM-005 | A4 | Pydantic path-param validation; parameterized SQL | FastAPI + aiosqlite + Semgrep lint | Automated SQLi probes against every endpoint |
| TM-006 | A5 | Compose network isolation; no post-launch peers | Operator + compose review | `docker network inspect` spot check |
| TM-007 | Memory / CPU | 128 MiB manifest cap; chunk-path regex validation | App code | Fuzz test with oversize manifests; URL-traversal payloads |
| TM-008 | Audit trail | `source` column + logs | DB schema + structlog | Block/unblock event visible in both DB and logs |
| TM-009 | Audit trail | `jobs` rows + `sync_cycle_complete` log | Scheduler + structlog | Log line present per cycle |
| TM-010 | A3 | F17 CI grep; F14 server-only token; backend strips incoming Authorization | Game_shelf CI + app middleware | Review bundle; curl frontend with `Authorization` → backend doesn't forward |
| TM-011 | Version metadata | FastAPI exception middleware | App middleware | Trigger deliberate exception; assert response has no stack trace |
| TM-012 | A1, A2, A3 | SHA256-prefix convention; Semgrep log-scan rule | App code + Semgrep | Semgrep CI run; scan 1 week of logs for patterns |
| TM-013 | Version fingerprint | Restrict `git_sha` to authenticated responses (Phase 3) | App code | Curl health, inspect fields |
| TM-014 | A4 | Non-root user; state volume 0700 | Dockerfile | In-container `ls -la` check |
| TM-015 | A6 availability | LAN-only + pfSense + uvicorn/aiosqlite limits | Infrastructure + app config | Load test with 256 concurrent clients; no 5xx |
| TM-016 | Lancache, WAN | Per-platform concurrency=1; dedupe 409; mutations rate-limit (Phase 3) | App code | Burst-POST test; confirm only 1 job runs |
| TM-017 | Scheduler | pydantic-settings cron validation | App startup | Bad cron → fail-fast with clear error |
| TM-018 | Memory | 128 MiB cap; streaming parse | App code | Fuzz with oversize response |
| TM-019 | Container integrity | Non-root user, read-only rootfs, no-new-privileges, drop caps | Dockerfile + compose | Phase 3 container escape pentest |
| TM-020 | A1, A2, A3 | OQ4 fork policy; SHA pinning; Snyk CLI | Vendored deps + CI | Monitor upstream commits; periodic audit |
| TM-021 | Container integrity | Click structured args; no shell=True; Semgrep lint | CLI code + Semgrep | CLI test with injection payloads |
| TM-022 | Container integrity | Setuid audit in CI; no-new-privileges | Dockerfile + CI | `find / -perm -u=s -type f` clean |
| TM-023 | A3, A6, privacy | All of A1-A22's mitigations + Game_shelf auth + pfSense host rules + Phase 3 access logs | Cross-system | Red-team exercise in Phase 3 |

---

## 6. Review Checklist (per Builder's Guide §1.3)

- [x] Every STRIDE category has at least one threat — ✅ S: 3, T: 4, R: 2, I: 5, D: 4, E: 4 (22 atomic + 1 chain = 23 threats)
- [x] Every threat references a specific component or data flow in this architecture (not generic OWASP) — ✅ Component/flow is the first bullet of each TM
- [x] Every mitigation is a concrete technical control (not "validate input" or "be careful") — ✅ file modes, CI lint rules, regex patterns, timeout values, specific middleware
- [x] At least one threat describes a multi-step attack chain — ✅ TM-023
- [x] Threats use stable IDs (TM-001, TM-002...) for Phase 3 traceability — ✅
- [x] 5 edge cases where the stack would fail — ✅ §4.1
- [x] 3 security vulnerabilities inherent to this design — ✅ §4.2
- [x] 2 data storage bottleneck risks with trigger conditions — ✅ §4.3
- [x] 1 limitation that could force a rewrite in 12 months — ✅ §4.4
- [x] Risk/mitigation matrix with Phase 2 + Phase 3 hooks — ✅ §5

---

## 7. Sign-off

**Orchestrator review required.** This threat model is referenced during every Phase 2.4 security audit and every Phase 3.2 verification. Any architectural change in Phase 2 that invalidates a mitigation must trigger a threat-model update + new ADR.

**Next Phase 1 step:** 1.4 — Data Model (migrations + rollback).
