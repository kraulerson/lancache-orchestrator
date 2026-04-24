# User Journey Map — lancache_orchestrator

**Phase:** 0
**Step:** 0.2
**Persona used:** Skeptical Product Manager
**Generated from:** FRD v1 (`docs/phase-0/frd.md`) + Intake §2.2
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

---

## Skeptical PM's Standing Rules (applied to every journey below)

Before mapping any happy path, I assume the following. Every step must survive them.

1. **The user has never read the README.** They pull the image, hit a wall, and the wall must be self-explanatory or the project is abandoned.
2. **The user is distracted.** They start Steam auth, leave to answer a message, come back 20 minutes later. The Steam Guard code they have is expired.
3. **The user is tired.** They mis-type the bearer token into the status page `prompt()` dialog, see "401", and don't understand why the cached data isn't showing.
4. **The user is adversarial — or appears to be.** They refresh Game_shelf's Cache page 50 times in 5 seconds. They click "Prefill" 8 times in a row. They invoke `orchestrator-cli auth steam` twice concurrently in two SSH sessions.
5. **The user's environment is broken.** Their DNS doesn't route `lancache.steamcontent.com` to Lancache yet. Their pfSense rule blocks 8765. Their cache volume is full. Their Game_shelf env var is missing the orchestrator token.
6. **The user is colorblind (Intake §9 hard constraint).** Every status indicator across status page, CLI output, and Game_shelf must be legible without color.
7. **Every step that requires typing a secret WILL be copy-pasted with a trailing newline or leading space.** The software must normalize or fail clearly.
8. **The user's first impression happens in the first 10 minutes.** If they hit three confusing errors in a row during setup, the project is done.

---

## 1. Primary Persona (expanded from Intake §2.2)

**Canonical table format per `templates/generated/user-journey.tmpl`:**

| Field | Value |
|-------|-------|
| **Name** | Karl (single operator) |
| **Role** | Homelab operator — runs a Proxmox cluster, Docker stacks, pfSense firewall, MikroTik switch, Pi-hole DNS on a well-maintained home network |
| **Goal** | Set-it-and-forget-it: keep a ~2600-game multi-platform library cached and fresh without weekly manual intervention, with **trustworthy** cache-state reporting |
| **Context** | Deploys the orchestrator alongside an existing Lancache instance on a DXP4800 NAS. Uses evenings and weekends; interleaved with other projects. Consumes status via Game_shelf (primary UI) and the orchestrator's own status page/CLI (fallback). |
| **Technical Skill** | High — comfortable with SSH, Docker Compose, `jq`, structured JSON logs. Not a Python developer; expects the CLI and status page to explain failures clearly. Colorblind (Intake §9 hard constraint). |

**Narrative expansion below. The table is the canonical-format summary; the expansion is the Skeptical PM–quality detail that informed the journey designs.**

### Karl — the Homelab Operator (narrative).

- **Who:** Single user, colorblind, runs a well-maintained homelab (Proxmox cluster, Docker stacks, pfSense firewall, MikroTik switch, Pi-hole DNS). Technical peer to the system he's deploying.
- **Skill level:** Advanced. Comfortable with SSH, `docker compose`, `jq`, and reading structured JSON logs. Not a Python developer; will not read the source to diagnose a bug. Expects the CLI and status page to tell him what's wrong.
- **Goal:** Set-it-and-forget-it. Wants his ~2600-game multi-platform library to stay cached and fresh without weekly manual intervention. Wants **trustworthy** cache-state reporting — the reason he's doing this is that SteamPrefill's flat-file tracker lies.
- **Emotional state on arrival:** Skeptical. He has been burned by prefill tooling before. His default assumption is that any "up to date" indicator is lying until proven otherwise. He wants to verify.
- **Availability:** Evenings and weekends. If a setup step takes more than 20 minutes, he will abandon the session and come back another day — losing context each time.
- **What makes him abandon:** Silent failures. Color-only status indicators. Tokens that require him to guess their location. Vague error messages like "authentication failed" without telling him *which* auth, *why*, and *what to do next*.

---

## 2. Entry Points

| # | Entry point | Features engaged | Failure cost if this step fails |
|---|---|---|---|
| E1 | First `docker compose up -d` on the DXP4800 with the new orchestrator service | Implicit-Deps ID1 (migrations), ID2 (lancache reachability), ID4 (secret loading), F9 (API), F12 (scheduler) | **Project abandonment.** User decides the orchestrator is broken and keeps SteamPrefill. |
| E2 | First auth flow: `docker compose exec orchestrator orchestrator-cli auth steam` | F11 (CLI), F1 (Steam auth) | User retries once; if confused, gives up and tries EpicPrefill instead. |
| E3 | Opening the status page at `http://<dxp4800>:8765/` for the first time | F10 (status page), F9 (API) | User thinks service is broken; SSH's in to check logs. Not fatal but burns trust. |
| E4 | Opening Game_shelf's library page after the cache integration is deployed | F14, F15 (proxy + badges) | Cache column shows "—" for every game; user can't tell if orchestrator is broken, Game_shelf is broken, or the feature just isn't live yet. |
| E5 | Opening Game_shelf's new Cache dashboard page for the first time | F16 | If orchestrator is offline, page must still render with clear offline state or user assumes the feature itself is broken. |
| E6 | Steady-state: next scheduled cron fires 6 hours after deploy | F12, F3, F4, F5, F6, F7 | Silent failure mode — if this fails and user doesn't open the status page, they won't notice for days. |
| E7 | Receiving a Steam "Security Alert" email at 02:00 because a new login happened | Out-of-band side effect of F1 | User panics, thinks account was compromised, changes password — now orchestrator auth is permanently broken until next CLI flow. |
| E8 | Weekly F13 validation sweep finds evicted content | F13, F7 | Games move to `validation_failed` en masse; if surfacing is unclear, user thinks the cache "broke." |

**Skeptical PM flag.** E7 is not in the FRD. If a user enables this orchestrator's Steam auth, Valve sends a "new device logged in" security email to their registered address. This is **expected Steam behavior**, not a bug, but an unprepared user may interpret it as a breach and respond by rotating their password — which immediately invalidates the orchestrator's session. **Recommendation:** README (Phase 3/4 deliverable) must prominently document this. Consider surfacing in the CLI `auth steam` flow itself: "You will receive an email from Valve about a new device login. This is expected. Do not change your password."

---

## 3. Journey A — First-Time Setup (Entry Point E1 → E3)

**Coherent experience:** User pulls the image, writes compose file, starts the stack, authenticates both platforms, and sees their first data in the status page — all within 30 minutes.

### A.1 Compose file assembly

**What user does.** Writes the orchestrator service block from the README into their existing Lancache `docker-compose.yml` (per Brief §2.2). Creates `secrets/orchestrator_token.txt` with a pasted random string.

**What user sees.** Nothing yet — this is file editing.

**System response.** None until `up -d`.

**Failure modes.**
- User's `docker-compose.yml` uses an older compose format that doesn't support top-level `secrets`. → `docker compose config` rejects the file with a cryptic schema error. **Mitigation:** README must state minimum Docker Compose version (likely `v3.1+`) and ship a complete sample file, not a fragment.
- User pastes a token with a trailing newline from `echo "..." > token.txt`. → Auth will fail later because the orchestrator's secret-loader must `.strip()` the file contents. **Mitigation:** ID4 (secret loading) must strip whitespace. Add to F9 acceptance: "token file containing trailing whitespace/newline is accepted equivalently to stripped version."
- User creates the token file world-readable (`0644`). → Not a security failure by itself (Docker reads it as root), but a smell. **Mitigation:** CLI's `config show` warns if the secret-file inode was world-readable when read.

### A.2 First `docker compose up -d`

**What user does.** `docker compose up -d orchestrator` (or `up -d` for the whole stack).

**What user sees.** `docker compose logs -f orchestrator` output in the terminal.

**System response (success path).**
1. Container starts; structured JSON logs begin streaming.
2. Secret loader reads `/run/secrets/orchestrator_token`, validates non-empty, strips whitespace. Logs `orchestrator_token_loaded` (with SHA256 prefix, never the token itself).
3. Migration script runs. Logs `migration_applied migration=0001_initial`. Creates SQLite file at `/var/lib/orchestrator/state.db`.
4. Pre-flight: HEAD `http://lancache/lancache-heartbeat`. Logs `lancache_reachable=true response_header_present=true`.
5. Pre-flight: `os.access('/data/cache', os.R_OK)`. Logs `cache_volume_mounted=true`.
6. Scheduler registers F12 cron and F13 cron.
7. uvicorn starts on `:8765`. Logs `api_ready`.
8. Status page is now live at `http://<dxp4800>:8765/`.

**Feedback mechanism.** Structured JSON log stream. No UI feedback yet — the user's first sign of "working" is `api_ready` in the logs.

**Failure modes.**

- **Secret missing.** Loader logs `orchestrator_token_missing` at CRITICAL, container exits 1. User sees restart loop.
  - **Mitigation.** Log line must include the exact expected file path and the compose section that should declare it. Example: `CRITICAL orchestrator_token_missing expected=/run/secrets/orchestrator_token fix="add 'orchestrator_token' to your compose file's secrets section; see README §3.2"`.

- **Lancache unreachable.**
  - **Skeptical PM case:** user has Lancache in the same compose, but the `depends_on` ordering hasn't fired yet; the first heartbeat check fails because Lancache isn't listening.
  - **Mitigation.** ID2 says container does NOT refuse to start, but surfaces loudly. Retry the heartbeat every 5s for up to 60s before declaring Lancache unreachable. If still failing at 60s: `lancache_reachable=false` persists; status page shows a red banner at top: "Lancache unreachable. Scheduled prefills will fail until this is fixed."
  - **Additional mitigation.** `/api/health` returns 503 with `{"status": "degraded", "lancache_reachable": false}` — Game_shelf's `/api/cache/health` proxy will reflect this as a degraded state.

- **Cache volume not mounted.** Startup self-test for F7 will fail on first prefill attempt, not at boot. This is a latent failure.
  - **Mitigation.** ID2 includes `cache_volume_mounted` check at startup. If `/data/cache` isn't mounted, log at ERROR and expose in `/api/health`.

- **Pre-existing DB with newer schema.** User downgraded the image. Migration script sees `MAX(id)` in `schema_migrations` > number of migration files. Ambiguous — the code doesn't know what the future schema removed.
  - **Mitigation.** Migration script fails loudly: `ERROR schema_version_ahead db=N migrations_file_count=M fix="restore from backup or use matching image version"`. Does not attempt to downgrade.

- **Port 8765 already in use on the host.** uvicorn fails to bind.
  - **Mitigation.** Compose surfaces this as a start error. README notes that the port is configurable via the compose `ports:` mapping.

**Exit point.** User sees an incomprehensible error, concludes the project is broken, removes the service from compose. **Project abandonment cost: complete.**

**Recovery strategy.** Every fatal startup error must include: (a) a short error code, (b) the exact file path or config key at fault, (c) a pointer to the README section that addresses it.

### A.3 Opening the status page for the first time

**What user does.** Opens `http://<dxp4800>:8765/` in a browser.

**What user sees.**
1. Page loads (< 20 KB HTML + JS, target < 1 s).
2. Browser `prompt()` dialog: "Enter orchestrator bearer token."
3. User switches to SSH, runs `cat secrets/orchestrator_token.txt`, copies, pastes into prompt.
4. Page renders. Platforms section shows two rows (Steam, Epic) with `auth_status = never` — both require CLI reconnect. No jobs yet. Disk stats show cache volume usage.

**Feedback mechanism.** Color + icon + text label on each platform row. For `never`: gray circle icon + text "No auth — run `orchestrator-cli auth steam` on DXP4800."

**Failure modes.**

- **User doesn't know where the token lives.**
  - **Skeptical PM case:** User wrote it to `secrets/orchestrator_token.txt` during setup. Remembers it's "somewhere in the secrets folder." But what if they set up the stack months ago? Or a friend did?
  - **Mitigation.** Status page prompt() includes helper text: "Token is in the `orchestrator_token` Docker secret on the DXP4800 host. Get it with: `cat secrets/orchestrator_token.txt`."
  - **Note.** `prompt()` doesn't easily support multi-line helper text. **Flag as journey-discovered feature gap FG1 below.**

- **User copies the token with a trailing space/newline.**
  - **Mitigation.** Client-side JS `.trim()` before sending Authorization header. Document in status.html source comment.

- **401 from API on first page load.**
  - **Skeptical PM case:** User thinks token is right, API rejects. Status page shows red banner "Invalid token." User stares at it. Where does it think I was supposed to get the token? Is this the same file I edited?
  - **Mitigation.** 401 banner includes the exact command to re-check the token: "If this is unexpected, run `docker compose exec orchestrator cat /run/secrets/orchestrator_token | xxd | head` to verify the file is intact."

- **Status page renders but every API call 500s.**
  - **Skeptical PM case:** DB not writable, scheduler thread crashed, structlog misconfigured.
  - **Mitigation.** Every section has its own loading/error state. At least one section (`health`) must render even if others error, so the user can see *which* parts are broken.

### A.4 `orchestrator-cli auth steam`

**What user does.** SSH's to DXP4800, runs `docker compose exec orchestrator orchestrator-cli auth steam`.

**What user sees (success path).**
```
Steam authentication
Enter Steam username: karl
Enter password: [hidden]
Steam Guard code (if prompted): ABCDE

[INFO] Connecting to Steam CM...
[INFO] Steam Guard required.
[INFO] Steam Guard validated.
[INFO] Refresh token persisted to /var/lib/orchestrator/steam_session.json
[INFO] Expires: 2027-04-20 (365 days from now)
SUCCESS: Steam authenticated.

Note: Valve will send a "new device login" email to your registered address.
This is expected. Do not change your password.
```

**System response.** Persists refresh token file at mode 0600. Updates `platforms.steam.auth_status = 'ok'`. Logs `steam_auth_completed` with correlation ID.

**Feedback mechanism.** Line-by-line progress in CLI; final SUCCESS line. Status page updates within 2 s on next poll.

**Failure modes.**

- **Wrong password.** CLI prints "Steam rejected credentials — check username/password." Exit 1.
  - **Skeptical PM case:** User has a unique password per site and mistyped. They retry. **Mitigation:** CLI supports re-invocation without restart; no lockout on our side (Steam's side may rate-limit — surface that).

- **Steam Guard 2FA source ambiguity.**
  - **Skeptical PM case:** User has both Steam Mobile Authenticator and email 2FA. The CLI just says "Steam Guard code." User has a 6-digit code in their email and a 5-character alphanumeric code in the app. Which one?
  - **Mitigation.** The CLI must prompt based on what Steam actually requested. If Steam's challenge includes a type hint, surface it: "Steam Guard (mobile authenticator): " vs "Steam Guard (email): ". Verify via steam-next whether this hint is available. **Flag as FG2.**

- **Steam Guard code expired before user pasted it.** Steam-next returns an error. CLI prints "Steam Guard code expired — run the command again and paste the code within 30 seconds."
  - **Mitigation.** Explicit timeout message. Do not just say "authentication failed."

- **User is on a new device from Steam's perspective and Steam requires mobile approval.** Steam-next may hang or return a different error.
  - **Skeptical PM case:** User's phone is charging in another room. They see the CLI hang. They Ctrl-C. Now the Steam session is in an indeterminate state.
  - **Mitigation.** CLI gives up after 120 seconds with "Steam is waiting for mobile approval. Approve in your Steam mobile app, then re-run this command." Clean exit, no orphaned session.

- **User pastes the token with an emoji or whitespace accidentally from a password manager that adds formatting.**
  - **Mitigation.** CLI strips input. If input contains non-ASCII, warns before attempting.

- **User runs `auth steam` while a sync cycle is in progress.**
  - **Skeptical PM case:** User starts auth at 06:00:03, the 06:00 cron is still running.
  - **Mitigation.** CLI detects an in-flight job and prompts: "A Steam sync is currently running (job #42). Auth re-login during a running sync may abort the sync. Continue? [y/N]". Or rejects outright if the sync itself requires the current session. Simplest: reject with guidance to wait or cancel the job.

**Exit point.** User abandons after 3 failed 2FA attempts. **Recovery:** document on the README exactly where to find Steam Guard, how it differs from email 2FA, and that a 1-minute window applies.

### A.5 `orchestrator-cli auth epic`

**What user does.** Runs `docker compose exec orchestrator orchestrator-cli auth epic`.

**What user sees (success path).**
```
Epic authentication
Open this URL in a browser, log in to Epic, and paste the auth code below:
  https://legendary.gl/epiclogin

Auth code: abc123def...

[INFO] Exchanging auth code...
[INFO] Refresh token persisted to /var/lib/orchestrator/epic_session.json
[INFO] Token rotates silently after first use (no further re-login required unless revoked).
SUCCESS: Epic authenticated.
```

**Failure modes.**

- **`legendary.gl/epiclogin` URL is down.**
  - **Skeptical PM case:** The URL is a community redirect. It has been up for years but it's not Epic-controlled.
  - **Mitigation.** Document the alternate URL (the underlying Epic OAuth authorize URL) in the README. CLI offers `--code-url-alternate` flag for the rare failure case.

- **User pastes the full URL instead of just the code.**
  - **Skeptical PM case:** "Auth code: " is ambiguous. User copies the whole `?code=abc123` URL.
  - **Mitigation.** CLI parses the input: if it matches `http[s]?://.*[?&]code=...`, extract `code`. If input contains `=`, warn.

- **User pastes the code but 90 seconds have passed (code TTL is short).**
  - **Mitigation.** CLI surfaces Epic's error verbatim: "Epic rejected the code — it may have expired. Get a fresh code at https://legendary.gl/epiclogin."

### A.6 First library sync (automatic, triggered ~6h after boot)

**What user does.** Nothing — this fires on cron. User might check the status page out of curiosity.

**What user sees.** Status page polling shows:
- Platform "Steam": last_sync_at updates to now. Message "Syncing 1,847 owned games…" disappears after ~5 min.
- Platform "Epic": same pattern.
- Game count appears in stats panel.

**Feedback mechanism.** Live updates on status page. Structured logs emit per-batch.

**Failure modes.**

- **Scheduled cron didn't actually fire.**
  - **Skeptical PM case:** APScheduler silently died on an uncaught exception in a handler. Status page still renders (uvicorn is fine), but `last_sync_at` never changes.
  - **Mitigation.** `/api/health` exposes `scheduler_running` boolean derived from checking APScheduler's `scheduler.state`. Status page surfaces this. F12 acceptance: add `"scheduler alive check reflected in /api/health"` as an explicit criterion.

- **User expects faster than 6h.**
  - **Skeptical PM case:** User just completed auth, refreshes status page every 10s for an hour, sees no game list. Thinks it's broken.
  - **Mitigation.** Status page's platform row shows "Next sync: in 5h 42m" after auth completes. CLI `orchestrator-cli library sync --platform steam` is documented prominently as the "I want this to happen now" command.

**Exit point.** User is impatient and runs `library sync` from the CLI — this is the intended experience but must be discoverable.

---

## 4. Journey B — Steady-State Operation (Entry Point E6)

**Coherent experience:** Zero user interaction. Cycle fires every 6 hours. User confirms via status page or Game_shelf Cache page whenever curious.

### B.1 Steady-state cycle

**What user does.** Nothing for weeks.

**What system does.**
- Every 6 hours: F12 cycle runs. Steam sync → diff → prefill new/updated games → F7 validate each → Epic sync → same.
- Every Sunday 03:00: F13 full-library validation sweep.
- Logs emit as structured JSON; operator is not watching.

**Feedback mechanism.** Logs. The status page shows "Last successful sync: 3h 17m ago." The Game_shelf Cache page shows the same.

**Failure modes.**

- **A single game fails to prefill repeatedly.**
  - **Skeptical PM case:** Some cursed game has a manifest that Steam returns inconsistently. Every cycle, the prefill fails, `games.status = 'failed'`. User never notices because there's no notification.
  - **Mitigation.** Status page and Cache page show `last_error` per game. Aggregate "N games in `failed` status" counter on the stats panel with a click-through. **But the user must open the page to see it.** No notification mechanism exists in MVP. **FG3 flag below.**

- **Scheduler lag.**
  - **Skeptical PM case:** DXP4800 is under heavy load from another container; APScheduler misses a trigger by 30 minutes; `misfire_grace_time` eats it, runs late. User sees "last sync: 6h 43m ago" on the next day, thinks something is wrong.
  - **Mitigation.** Status page distinguishes "last sync: 3h ago" (healthy) from "last sync: 7h+ ago and next expected: 5m" (normal catching-up) from "last sync: 7h+ ago and no scheduler activity" (broken). Requires surfacing scheduler health.

### B.2 New purchase

**What user does.** Buys a new game on Steam.

**What system does.**
- Within the next ≤6 hours: F3 library enumeration picks up the new game. Upserts with `current_version=<gid>, cached_version=NULL, status='not_downloaded'`.
- Next F12 cycle enqueues F5 prefill. Downloads the game through Lancache. Runs F7 validation.
- Game's Cache badge in Game_shelf transitions: `unknown` → `missing` → `downloading` → `cached`.

**Feedback mechanism.** Game_shelf library page updates on next user navigation. Status page within 2 s of DB write.

**Failure modes.**

- **User buys a game and immediately goes to download it on their gaming PC, triggering a cache MISS before the orchestrator has run its cycle.**
  - **Skeptical PM case:** This is exactly the problem the orchestrator is supposed to prevent. If the orchestrator's cycle is 6 hours, a brand-new game purchase can still miss the cache.
  - **Mitigation (MVP).** Document this: "Immediately-played games may miss the cache. Run `orchestrator-cli library sync --platform steam` before starting the download if you want to force an immediate prefill." **FG4 flag below.**
  - **Post-MVP enhancement:** Steam's API exposes recently-purchased games; a separate `purchase_watcher` fast-cycle (every 15 min) could narrow the window.

### B.3 Game update published

**What user does.** Nothing.

**What system does.** Next F12 cycle sees `current_version != cached_version`, re-prefills. F7 validates.

**Failure modes.** Same as B.2.

---

## 5. Journey C — Block / Unblock

### C.1 Block a game via Game_shelf UI (preferred path)

**What user does.** Navigates to a game detail page in Game_shelf, sees the CachePanel, clicks "Block."

**What user sees.**
- Optimistic update: badge changes to `blocked` (slate + prohibition icon + text "Blocked").
- Success toast: "Blocked [Game Title] from automatic prefill."
- On any 4xx/5xx from the API: optimistic rollback + error toast with actionable message.

**Failure modes.**

- **User accidentally blocks the wrong game.**
  - **Mitigation.** Toast has "Undo" button (calls DELETE block) within 10 seconds.

- **Orchestrator is offline at the moment of click.**
  - **Mitigation.** Button disabled with tooltip "Cache orchestrator unreachable." No optimistic update attempted.

- **Concurrent block/unblock race between two browser tabs.**
  - **Skeptical PM case:** User has Cache dashboard open in two tabs. Blocks in one, unblocks in other. Which wins?
  - **Mitigation.** Last write wins at SQLite level (F8 acceptance). Both tabs reconcile on next poll. Tolerable.

### C.2 Block a game via CLI

**What user does.** `orchestrator-cli game steam/12345 block --reason "region-locked, not interested"`.

**What user sees.** `Blocked steam/12345 (The Elder Scrolls VI).` Exit 0.

**Failure modes.**

- **User types the wrong app_id.**
  - **Skeptical PM case:** `steam/12345` returns 404. User gets "game not found" and doesn't realize they typo'd.
  - **Mitigation.** CLI accepts app_id-only argument with `--platform` and does fuzzy title match: `orchestrator-cli game block "elder scrolls"`. If ambiguous, lists matches and prompts. **FG5 flag.**

### C.3 Unblock from the Cache dashboard block-list

**What user does.** Navigates to `/cache` in Game_shelf, scrolls the block list, clicks "Unblock" on a row.

**What user sees.** Row disappears with fade animation. Success toast.

**Failure modes.** Same as C.1.

---

## 6. Journey D — Auth Expiration Recovery

### D.1 Epic silent rotation fails during a sync cycle

**What user does.** Nothing — they don't know it happened.

**What system does.**
- F2 silent rotation fails at cycle start.
- `platforms.epic.auth_status = 'expired'`.
- F4/F6 skipped for this cycle.
- Status page platform row turns amber + icon + text "Epic: Expired — run `orchestrator-cli auth epic`."
- Game_shelf's Cache dashboard platform card shows the same.

**Feedback mechanism.** Visible only when user opens the page. No email, no webhook.

**Failure modes.**

- **User doesn't open the page for 2 weeks.**
  - **Skeptical PM case:** New Epic purchases missed. User discovers at game-night when buddy's game has to pull from WAN.
  - **Mitigation (MVP).** Accept. Document that manual status checks are required until notification infrastructure ships.
  - **Post-MVP.** Should-Have feature — webhook/ntfy notification on any platform transitioning to `expired`. Intake §10 "Notification preferences" notes this deferral.

### D.2 Steam mobile-approval required during re-auth

**What user does.** Runs `orchestrator-cli auth steam`. Steam presents a mobile approval challenge.

**What user sees.**
```
Steam requires approval on your Steam mobile app.
Waiting... (this times out in 120s)
```

**What user does.** Opens Steam mobile app, taps approve.

**System response.** CLI detects success, persists token, prints SUCCESS.

**Failure modes.**

- **User's phone is dead.**
  - **Mitigation.** 120s timeout, clean exit, guidance to re-run. No orphaned session state.

- **User approves but CLI has already timed out.**
  - **Mitigation.** User re-runs; new session request is issued; Steam treats it as a fresh challenge.

---

## 7. Journey E — Fault Diagnosis

User suspects something is broken. Where do they look?

### E.1 Primary surface: Game_shelf Cache dashboard

1. Open `/cache` in Game_shelf.
2. Check "Overall stats" — is disk usage sensible? Is queue depth growing without bound?
3. Check platform cards — is either Expired?
4. Check recent jobs — are jobs going `failed` repeatedly? Click in for error detail.
5. Check block list — is a game blocked that shouldn't be?

### E.2 Fallback: Orchestrator status page at port 8765

Used when Game_shelf itself is offline or the user suspects the orchestrator is unreachable from ThinkStation.

### E.3 Last-resort: CLI on DXP4800

`orchestrator-cli jobs --active`, `orchestrator-cli jobs <id>`, `orchestrator-cli game steam/123`, `orchestrator-cli auth status`, `orchestrator-cli config show`. For anything deeper: `docker compose logs orchestrator | jq 'select(.level=="error")'`.

**Failure modes.**

- **User can't tell whether the issue is orchestrator, Lancache, or their gaming PC's DNS pointing at the wrong place.**
  - **Skeptical PM case:** `/api/stats` and `/api/health` must expose Lancache reachability, cache volume mount state, cache disk usage, and last N errors. Every failure mode should be inspectable from the status page before SSH is needed.
  - **Mitigation.** Expand F10 and F16 to include:
    - Lancache reachability indicator (green HIT, red UNREACHABLE, amber DEGRADED).
    - Cache volume mount indicator with free space.
    - Last 5 errors across all platforms/jobs.
    - Link to "Copy diagnostic bundle" button that assembles logs + config + db-row-counts into a clipboard JSON for paste into a GitHub issue. **FG6 flag.**

---

## 8. Journey F — Cache Eviction Discovered by F13 Sweep

**What user does.** Nothing — F13 runs Sunday 03:00.

**What system does.**
- Full sweep runs. 84 games transition from `cached` → `validation_failed` because Lancache's LRU evicted them.
- Summary log: `validation_sweep_complete games_checked=2617 cached=2533 partial=12 missing=72 error=0 elapsed_sec=942`.
- Next F12 cycle re-prefills all 84 games. Disk usage grows.

**What user sees.**
- Monday morning: Cache dashboard shows "84 games in validation_failed" counter, disk usage ticking up.
- Jobs feed shows 84 new `prefill` jobs running.

**Failure modes.**

- **Eviction is faster than prefill can keep up.**
  - **Skeptical PM case:** Cache is full. Every F12 cycle re-prefills games, which evicts other cached games, which F13 flags next week. Thrashing.
  - **Mitigation.** F16 Cache dashboard shows "LRU headroom" prominently. If headroom < 10 GB, red banner: "Cache is full and eviction is likely. Consider expanding volume or blocking large games you don't play."
  - **Not in MVP: automatic LRU pressure alert via notification.**

- **F13 sweep reports lots of `error` outcomes.**
  - **Skeptical PM case:** Cache volume corrupted, or the formula drifted.
  - **Mitigation.** F7 self-test at boot catches the formula drift case. If post-boot self-test passed but sweep reports many errors: `/api/health` exposes `validator_error_rate`; dashboard surfaces it. Operator has clear signal.

---

## 9. Journey G — Orchestrator Unreachable from Game_shelf (E4, E5 degraded)

**What user does.** Opens Game_shelf library page. Orchestrator is offline (DXP4800 rebooted for maintenance).

**What user sees.**
- Library renders normally.
- Cache badges render as "—" with tooltip "Cache status unavailable."
- Dismissible banner at top: "Cache orchestrator unreachable — cache state hidden. Library browsing is unaffected. [Retry]"
- Action buttons on CachePanel (Validate, Prefill, Block) disabled with tooltips pointing at the banner.

**Failure modes.**

- **Retry storm.**
  - **Skeptical PM case:** Naive implementation: every 10s retry until success. Orchestrator is down for 2 hours; Game_shelf makes 720 failing requests during that time, each waiting 5s to time out. That's hours of blocked HTTP connections.
  - **Mitigation.** One check on initial page load; then only retry on explicit Retry button click. F17 already specifies this.

- **User clicks Retry 50 times in 10 seconds.**
  - **Mitigation.** Debounce Retry button 2s between clicks.

---

## 10. Journey H — Game_shelf Unreachable from User

**What user does.** Can't reach Game_shelf. ThinkStation rebooting, LXC broken, network down.

**What user sees (fallback path).** Opens `http://<dxp4800>:8765/` directly. Status page still works. All orchestrator functionality is accessible via CLI on the DXP4800.

**Failure modes.**

- **User didn't bookmark the orchestrator's status page URL.**
  - **Mitigation.** README documents both URLs clearly. CLI `config show` prints them.

- **User doesn't know what the status page's URL is (forgot DXP4800's hostname/IP).**
  - **Mitigation.** Beyond scope — this is a general homelab knowledge issue. Document in project README.

---

## 11. Exit Points (where users abandon)

Collated from all journeys. Each exit point has a recovery strategy.

| # | Exit point | When it happens | Recovery strategy |
|---|---|---|---|
| X1 | Setup wall at secret-file creation | Container exits 1 with unclear error | Error messages must include exact file path + fix command |
| X2 | 2FA confusion during Steam auth | User mistakes email 2FA code for mobile authenticator code (or vice versa) | CLI must surface which type Steam is challenging |
| X3 | Bearer token lost, can't use status page | User rebuilds stack, new token, old browser has stale sessionStorage | Status page's 401 banner includes recovery command |
| X4 | Silent auth expiry goes unnoticed for days/weeks | No notification mechanism in MVP | Document that status page check is required; Post-MVP webhook |
| X5 | "The cache is broken!" after F13 sweep | User sees 84 games in `validation_failed` without context | Sweep summary banner with explanation and "re-prefill in progress" indicator |
| X6 | User thinks orchestrator is down when it's just waiting for next cron | Right after first auth, no games appear for 6 hours | Status page shows "Next sync in X" timer; CLI `library sync` command documented |
| X7 | Steam security email triggers panic-rotation of password | Out-of-band — not caught by our UX | CLI auth flow prints warning about expected email; README prominently |
| X8 | pfSense rule blocks port 8765 from ThinkStation's VLAN | Game_shelf proxy times out; frontend shows offline state | Documentation step in deployment guide; orchestrator logs failed inbound attempts for diagnosis |

---

## 11a. Secondary Personas

**Single-persona product — no secondary journeys.** The orchestrator has exactly one operator (Karl). Game_shelf is also single-user. There is no multi-tenant, multi-user, admin-vs-end-user, or guest-vs-registered distinction. Future "community deployment" would be a separate research question, not a secondary persona of the MVP.

---

## 12. Feedback Loops

Every user action produces visible feedback. Summarized:

| User action | Feedback mechanism | Latency |
|---|---|---|
| `docker compose up -d` | Structured JSON logs | Immediate |
| CLI `auth steam`/`auth epic` | Line-by-line CLI output + final SUCCESS/FAILURE | 1–30 s |
| Status-page `prompt()` | Full page render or 401 banner | < 1 s |
| Click Block/Unblock in Game_shelf | Optimistic badge update + toast | < 100 ms perceived |
| Click Validate in CachePanel | Button → spinner → result badge (green/red/amber) | 5 s for 50 GB game |
| Click Prefill in CachePanel | Button → "queued" toast → job appears in Jobs feed | < 1 s to queue; minutes–hours to complete |
| New purchase on Steam | Badge appears on next library refresh (≤6h) | ≤6h passive, immediate with CLI `library sync` |
| Auth expiry | Status page + Cache dashboard platform card color + icon + text | Up to 2 s poll latency |

---

## 13. Feature Gaps Discovered by Skeptical PM

Gaps surfaced while mapping journeys, not captured in FRD §5 (Implicit Dependencies). These are **smaller than full features** but real UX gaps. Recommending tracking as Post-MVP refinements or Phase 3 hardening.

| # | Gap | Severity | Recommendation |
|---|---|---|---|
| FG1 | Status page `prompt()` has no room for helper text ("where to find the token") | Low | Post-MVP: replace with minimal HTML login form that includes helper line and a "View command to retrieve token" collapsible section |
| FG2 | CLI Steam-auth prompt doesn't distinguish mobile vs email 2FA type | Medium | **MVP (JQ1 resolution 2026-04-20):** if `steam-next` exposes challenge type, CLI prompt discriminates. Otherwise prompt text lists both possibilities and README documents. Verified in Phase 1 Step 1.2. |
| FG3 | No notification mechanism for repeated per-game prefill failures | Medium | Post-MVP (Intake §10): webhook/ntfy integration on `failed` status transitions and on platform auth expiry |
| FG4 | New purchases may miss cache if played immediately after purchase (6h cycle window) | Medium | MVP: document CLI `library sync` as "run me before starting a fresh download." Post-MVP: purchase-watcher fast cycle |
| FG5 | CLI `game` subcommand requires exact app_id; no fuzzy title search | Low | Post-MVP: `orchestrator-cli game search "elder scrolls"` returns candidates |
| FG6 | No "Copy diagnostic bundle" UX for easy issue filing | Medium | Post-MVP or Phase 4: `orchestrator-cli diagnostics` outputs a sanitized JSON bundle |
| FG7 | No mechanism to detect LRU pressure and prevent cache thrashing proactively | Medium | Post-MVP: Cache dashboard surfaces headroom; future-future: auto-suggest block candidates based on play history |
| FG8 | Steam's "new device login" email is a first-run surprise that isn't documented | Low | Add to CLI `auth steam` flow output and README prominently |

**Escalated to Orchestrator decision:** Should FG2 (Steam 2FA type disambiguation) move into MVP F1 acceptance, or is the "document both types in README" workaround acceptable? Skeptical PM recommendation: **move into F1 if steam-next supports it**, otherwise document workaround.

---

## 14. Review Checklist (per Builder's Guide Step 0.2)

- [x] Every step has success and failure responses — ✅
- [x] Every action produces visible user feedback — ✅ (feedback loops table §12)
- [x] At least one exit point and recovery mechanism identified — ✅ (8 exit points with recovery strategies in §11)
- [x] Skeptical PM mindset applied throughout — ✅ (standing rules §0, failure cases in every journey)
- [x] All 17 Must-Have features covered by at least one journey — ✅ (A covers F1–F2, F9–F12; B covers F3–F7, F13; C covers F8; D covers F1, F2 recovery; E covers F10, F11, F16; F covers F13, F7; G and H cover F14–F17)

---

## 15. Open Questions — Resolved by Orchestrator 2026-04-20

### JQ1. 2FA type disambiguation in MVP F1 — **RESOLVED: yes if steam-next supports it**
**Decision.** F1 MVP acceptance criterion added: CLI prompt must discriminate mobile-authenticator vs email-code 2FA **if `steam-next` exposes the challenge type**. If not, the orchestrator prints both possibilities in the prompt text and documents the distinction in the README. Verification of `steam-next` support happens in Phase 1 Step 1.2 (architecture) during library evaluation; the acceptance criterion is written as a conditional in the FRD.

### JQ2. Silent auth expiry for MVP — **RESOLVED: accepted**
**Decision.** No notification mechanism in MVP. Intake §10 already defers this. Phase 4 handoff documentation must list "check status page or Cache dashboard weekly" as an explicit maintenance task. Post-MVP FG3 (webhook/ntfy) tracks the follow-up.

### JQ3. Scheduler-health in /api/health — **RESOLVED: yes**
**Decision.** F12 MVP acceptance gains a hard criterion: `/api/health` exposes `scheduler_running: bool` derived from `APScheduler.state`. If the scheduler has died, `/api/health` returns 503 with a body that includes `scheduler_running: false` and the last exception message if captured. Status page and Cache dashboard must both render this as a prominent red banner.

---

## 15a. Carry-Forward Notes

- **FG2 elevated to MVP-conditional** (see §13 table — re-row'd with MVP tag).
- **FG3, FG4, FG6, FG7 remain Post-MVP.** FG1, FG5 remain Post-MVP. FG8 is a Phase 2 README/CLI output task, not a feature.
- **Steam "new device login" email warning** (standalone, referenced in Journey A.4) formalized as a Phase 2 CLI-output requirement + a README Security/Operations section item.
