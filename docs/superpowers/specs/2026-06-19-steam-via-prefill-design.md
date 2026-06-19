# Sub-project ① — Steam via SteamPrefill — Design

**Date:** 2026-06-19
**Status:** Approved (design)
**Repo:** lancache_orchestrator. **Branch:** `feat/steam-via-prefill`
**Parent:** the re-architecture north-star (`docs/superpowers/specs/2026-06-19-re-architecture-design.md`, PR #174 merged). This is roadmap step ①.

> Scope: replace the orchestrator's fragile Steam layer with a wrapper around the already-installed, already-authed, modern **SteamPrefill** — **in-place** (still the monolith on the UGREEN; the control/data-plane *agent* split is step ②, the LXC move is step ④). This ships value on its own.

> **RE-SCOPE 2026-06-19 (Task-1 gate result — supersedes parts of §3.3/§4/§6/§9/§10 below).** The live gate found: ✅ `steam.core.manifest` imports standalone (gevent-free); ❌ **SteamPrefill's cached manifests are SteamKit2/protobuf-net format, NOT parseable by ValvePython's parser** (different field schema). So validate **cannot** source manifests from SteamPrefill's cache. Decision (Karl): **ship prefill + auth + owned-app enumerate via SteamPrefill NOW** (fixes the bulk auth-cascade — the disaster was the 2,484-game *prefill* sweep), and **KEEP the existing steam worker ONLY for `manifest_fetch` feeding F7 validate** (validate of already-known games already reads DB manifests via the kept parser). **Do NOT delete the worker and do NOT remove Steam auth in ①.** A follow-up sub-project gives validate a modern manifest source (parse SteamKit2's format, or steam.py) and *then* deletes the worker. Net ① deliverable: `SteamPrefillDriver` + rewire **prefill / enumerate / F8-version-diff / `/health` Steam-prefill-status** to it; the worker + Steam auth endpoints stay.

---

## 1. Problem (recap) + why SteamPrefill

The steam worker (`ValvePython/steam 1.4.4`, gevent subprocess) rides Steam's **deprecated legacy auth** → can't persist a session → a slow manifest wedges + restarts the worker → session lost → mass `NotAuthenticated`. **SteamPrefill (SteamKit2)** doesn't have this: modern `~6-month` refresh token, persisted. And it's **already installed, authed, lancache-native, and running** on the box (`/SteamPrefill`, v3.4.2, on cron). So we delegate Steam to it rather than re-implement Steam in Python.

## 2. Recon (established 2026-06-19 — the integration surface)

- Binary `/SteamPrefill/SteamPrefill` v3.4.2 (single-file .NET). Cron: `prefill --recently-purchased` then `prefill`.
- **Auth:** `Config/account.config` — ProtoBuf blob holding the username + a **modern JWT refresh token** (`{"iss":"ste...`). SteamPrefill owns Steam auth; persists ~6 months. Karl re-auths SteamPrefill (rarely), as today.
- **Prefill flags:** `--all`, `--recent`, `--recently-purchased`, `--top[N]`, `-f/--force` (re-prefill regardless of version; default only prefills newer), `--os`, `--verbose`, **`--no-ansi`** (plain output for parsing). **No `--app <id>` flag** → target specific apps by **writing `Config/selectedAppsToPrefill.json`** (a JSON **list of app IDs**) then running `prefill`.
- **Version state:** `Config/successfullyDownloadedDepots.json` — `{ "<app_id>": [<manifest_gid>, ...] }` (per-app, which depot-manifest versions were prefilled). SteamPrefill's own version-diff record.
- **Owned-app enumerate:** `select-apps` (interactive) / `select-apps status`.
- **Manifest cache:** `~/.cache/SteamPrefill/v1/` (root cron → `/root/.cache/SteamPrefill/v1/`). `clear-temp` purges it — **persistence is not guaranteed**.

## 3. Architecture

A new **`SteamPrefillDriver`** (`platform/steam/prefill_driver.py`) — the orchestrator's only Steam touchpoint — invokes the SteamPrefill binary as a subprocess and reads its state files. It replaces the entire `platform/steam/` worker stack and `prefill/downloader.py`.

### 3.1 Operations (what the driver exposes)
- **`prefill_apps(app_ids: list[int], *, force: bool = False) -> PrefillResult`** — write `selectedAppsToPrefill.json` = `app_ids`, run `SteamPrefill prefill --no-ansi [--force]`, parse stdout/exit for per-app success/failure + bytes. (Per-app targeting via the config file; the driver owns writing it.)
- **`list_owned() -> list[OwnedApp]`** — owned-app enumerate (drive `select-apps status` / parse, or read SteamPrefill's owned-apps cache). Feeds the orchestrator's library/Game_shelf.
- **`downloaded_state() -> dict[int, list[int]]`** — read `successfullyDownloadedDepots.json` (app → manifest GIDs). The version-diff truth (replaces the orchestrator's F8 manifest-diff bookkeeping for Steam).
- **`auth_status() -> SteamAuthStatus`** — `account.config` present + the embedded JWT `exp` not passed ⇒ OK; else `needs_reauth`. (No Steam login in the orchestrator at all.)

### 3.2 Auth — fully delegated
The orchestrator **stops authenticating Steam entirely.** Removed: the steam-worker auth handlers, the OQ2 `POST /platforms/steam/auth` + 2FA endpoints, the CLI `auth steam` path, the encrypted-password approach (already superseded). `/health` + the platforms auth-status report Steam status by reading `account.config`'s token validity. **Re-auth, when needed, is `SteamPrefill` itself** (Karl runs it; rare, ~6-monthly).

### 3.3 Validate (F7) — the one entangled piece, resolved
F7 validate (read-only "is this game still actually in the lancache?", for eviction detection + Game_shelf badges) needs the depot **manifest** (chunk list → lancache cache-keys → disk-stat). It does **not** need Steam auth (it's pure disk-stat given a manifest). Resolution:
- **SteamPrefill already fetches + caches manifests** (`~/.cache/SteamPrefill/v1/`) during prefill. The orchestrator reads those and **parses them with `steam.core.manifest.DepotManifest`** — kept as an **auth-free, gevent-free manifest *parser* only** (a tiny slice of the ValvePython lib; the worker/client/CDN/auth are all deleted). Validate stays read-only disk-stat, fed by SteamPrefill's manifests.
- **GATE (resolved in the ① plan's first task, live):** confirm SteamPrefill's cached manifests are (a) **retained** long enough (don't `clear-temp` them, or have the driver fetch-and-retain), (b) **locatable per app/depot**, and (c) **parseable** by `steam.core.manifest.DepotManifest`. **If any fails**, fallback: the driver triggers a SteamPrefill manifest-only/prefill pass to materialize them, or — last resort — validate is reframed (coarser: trust `successfullyDownloadedDepots.json` + lancache eviction-pressure heuristics) and tracked as a follow-up. The plan does not delete the worker until validate's manifest source is proven.

### 3.4 Jobs rewire
- `prefill` job → `SteamPrefillDriver.prefill_apps`.
- `library_sync` (Steam) → `SteamPrefillDriver.list_owned`.
- `manifest_fetch` (Steam) → removed as a standalone job (manifests come from SteamPrefill); validate reads SteamPrefill's manifests.
- `validate` job → unchanged disk-stat logic, now fed by SteamPrefill manifests (per 3.3).
- F8 version-diff driver (Steam) → uses `downloaded_state()` instead of the orchestrator's own manifest-diff.

### 3.5 Where it runs
In-place: the driver invokes the SteamPrefill binary **on the lancache host** (same as today's monolith — no hairpin). Step ② later moves the *invocation* behind the data-plane agent; this spec doesn't require the agent yet. The driver shells out via the existing job worker.

## 4. Deletions (after validate is proven, §3.3 gate)
`platform/steam/worker.py`, `client.py`, `session.py`, `credentials.py`; `prefill/downloader.py` (Steam); the steam-worker subprocess + IPC + restart-storm machinery; `requirements-steam-worker.txt` **minus** the `steam.core.manifest` parser dependency (keep a minimal `steam` install for parsing only, or vendor the manifest protobuf). The encrypted-password spec/plan stays abandoned.

## 5. Security
- The orchestrator no longer holds Steam credentials at all — **SteamPrefill owns them** (`account.config`, 0644 today on the box; the driver only reads `account.config`'s token `exp`, never the token). Smaller credential surface than today.
- The driver runs a trusted local binary (`/SteamPrefill`) with a config it writes; validate `selectedAppsToPrefill.json` content (app-id ints only) before writing. Never log SteamPrefill output that could contain account identifiers beyond what's already logged.
- Throttling: invoke SteamPrefill at low priority (nice/ionice) on the 4-vCPU NAS; don't overlap its own cron (detect a running SteamPrefill / honor a quiet window).

## 6. Error handling / edge cases
- SteamPrefill not installed / wrong version → driver surfaces a clear `steam_engine_unavailable`; `/health` degraded.
- `account.config` missing/expired → `auth_status = needs_reauth`; jobs that need Steam fail fast with that signal (no crash loop).
- A `selectedAppsToPrefill.json` the orchestrator writes must be **restored/owned** so it doesn't clobber Karl's manual selections destructively — the driver snapshots/uses a dedicated selection or restores afterward (decide in plan: dedicated config vs save/restore).
- Concurrent SteamPrefill (the cron + the orchestrator) → serialize (lockfile / detect running process), since SteamPrefill isn't built for concurrent invocations sharing one auth/cache.
- Parsing: pin to `--no-ansi` output; tolerate version-string drift; treat unparseable output as failure, not silent success.

## 7. Testing
- `SteamPrefillDriver` unit tests with the binary **mocked** (a fake `SteamPrefill` script / subprocess stub): `prefill_apps` writes the right `selectedAppsToPrefill.json` + parses sample `--no-ansi` output (success, partial, failure); `downloaded_state` parses a sample `successfullyDownloadedDepots.json`; `auth_status` reads a sample `account.config` (token exp valid/expired); `list_owned` parses sample output.
- Validate path: F7 disk-stat fed by a sample SteamPrefill manifest, asserting the same cache-key/disk-stat results as today (regression against the existing validator tests).
- Jobs rewire: the prefill/library_sync/validate handlers call the driver (driver mocked); no Steam-auth endpoints remain (assert removed).
- Live (the §3.3 gate + a smoke): on the box, drive a single-app prefill via the driver + confirm a manifest is readable/parseable for validate. Operator-collaborative.

## 8. Migration / continuity
- **Live system:** the orchestrator API stays at `192.168.1.40:8765` (no Game_shelf change — that's step ④). F14–F17 keep working; the Steam *status* the API serves now comes from SteamPrefill.
- **No re-auth churn:** Steam auth moves to SteamPrefill, which is already authed — so this *removes* the auth pain immediately.
- Sequence within ①: (1) **gate** — prove SteamPrefill manifests are usable for validate (live); (2) build `SteamPrefillDriver` (TDD, mocked); (3) rewire jobs; (4) delete the worker stack; (5) live smoke on the box.

## 9. Scope boundary (YAGNI)
Out: the data-plane agent (step ②) — driver shells out in-process for now; the LXC move (step ④); a login UI; multi-account; changing Epic (untouched). Not building a Steam library — we *drive a binary*.

## 10. Open question for the gate (plan task 1)
Are SteamPrefill's `~/.cache/SteamPrefill/v1/` manifests retained + per-app-locatable + parseable by `steam.core.manifest.DepotManifest`? If yes → clean full deletion. If no → driver materializes manifests on demand, or validate is reframed (follow-up). **This gates worker deletion.**
