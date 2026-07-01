# Steam Manifest-Only Fetcher (DepotDownloader) — Design

**Date:** 2026-06-30
**Status:** Approved (design)
**Closes:** the Steam validation-coverage gap ([[project_validation_manifest_gap]])
**Builds on:** durable manifest store (PR #200, `docs/superpowers/specs/2026-06-24-durable-manifest-store-design.md`), validate gid-match (PR #209, [[project_validate_gid_match_fix]]), capture-path hardening (PR #212, [[project_uat13_closed]])
**Supersedes** (a) the seed-via-force-prefill runbook (R2) in the 2026-06-24 durable-store design, and (b) the 2026-06-24 *independent fetcher* design + plan on the stale `feat/steam-manifest-fetcher` branch, which chose **ValvePython** — replaced here by DepotDownloader (out-of-process; no gevent dependency at all). That branch's only code (`.shas` validator support) already landed on `main` independently, so nothing is lost by abandoning it. See Decision Record.

<!-- Last Updated: 2026-06-30 -->

## Problem (live, 2026-06-30)

The F7 validator checks a Steam game by reading that app's manifest (chunk list) from the manifest cache/archive union. An app with no manifest returns `outcome="error", error="no_manifest_in_cache"`, and the control plane leaves its status unchanged — so it is **structurally unvalidatable**.

Live facts on the NAS agent (192.168.1.40, inside `orchestrator-agent`):

- **1077** selected apps (`/SteamPrefill/Config/selectedAppsToPrefill.json`), **2248** downloaded depots (`successfullyDownloadedDepots.json`) ≈ ~13 TB cached.
- Durable manifest archive (`/manifest-archive/v1`) covers only **451 distinct apps** (2529 `.bin`); the live host cache (`/steamprefill-cache/v1`) holds the same 451.
- **Gap = ~626 selected apps with cached chunks but no manifest** to validate them against.

**Root cause (structural).** SteamPrefill only *writes* a depot manifest `.bin` when it actually downloads that depot. An already-up-to-date app is skipped (it compares manifest GIDs from cheap product-info and never fetches the full manifest), so its manifest never (re)appears in the live cache for the periodic archive-sync or the PR #209 post-prefill capture to grab. SteamPrefill has **no manifest-only mode**; `prefill --force` is the only lever and it **re-reads the app's chunks**. The gap therefore cannot be closed durably by SteamPrefill alone, and it **recurs** as apps update.

## Goal

Get a current, **gid-aligned** manifest into the durable archive for **every selected Steam app** — **without re-downloading chunks** — so the validator can validate the whole prefilled library. Re-runnable **weekly, unattended**, to stay current as the library drifts.

## Non-goals

- **Epic.** No disk-stat validator exists for Epic; deferred to a separate feature.
- **Replacing SteamPrefill.** The fetcher **complements** it: SteamPrefill stays the chunk-prefill engine; the fetcher only fills manifest gaps for validation.
- **Pruning owned-but-not-selected clutter rows** from `games`. Out of scope ([[#366]]).
- **Reviving in-process ValvePython/SteamKit.** Explicitly rejected — see Decision Record.

## Decision Record (Karl's calls)

1. **Tool: DepotDownloader `-manifest-only`** (not seed-via-force-prefill; not ValvePython).
   - *Why over seed-via-force:* force-prefilling the 626 missing apps re-reads ~7.5 TB from the lancache (LAN HITs, but tens of hours of load on the CPU-steal-bound NAS, recurring). `-manifest-only` fetches only the manifest (KB–MB each) in minutes, no chunk re-read, and stays cheap to repeat.
   - *Why over ValvePython:* re-arch ③ deleted ValvePython because a `gevent.Timeout` (a `BaseException`) escaped every `except Exception` and killed the worker (UAT-11). DepotDownloader is an out-of-process binary — no gevent in the agent — so it gets the manifest-only benefit without that failure mode. (It is also SteamKit2-based, the same family as SteamPrefill.)
2. **Coverage: fetch the full current set each run** (not just the missing 626). The set is **read live from SteamPrefill's records each run — never a hardcoded list** — so it **auto-grows** as Karl adds games (the "1077"/"~626" figures are today's snapshot only). Idempotent skip-if-exists makes re-fetching already-archived apps nearly free, and a single uniform path is simpler and self-healing.
3. **Auth: the S2 spike first tries to reuse SteamPrefill's existing session** so Karl skips a second 2FA entirely. If reuse is not viable, fall back to DepotDownloader's own one-time login (`-username … -remember-password` + one 2FA), unattended thereafter.

## Authentication model

DepotDownloader `-remember-password` *"persists the login key for your Steam session, avoiding the need to enter a 2‑factor code every time"* (confirmed via Context7 `/steamre/depotdownloader`).

- **Preferred (S2 reuse):** point DepotDownloader at SteamPrefill's persisted session so **no new login** is needed. Resolved by the S2 spike.
- **Fallback (own session):** one-time interactive `docker exec -it` setup — `-username <user> -remember-password` + Steam Guard 2FA — persists DD's login key to a mounted config dir (`/depotdownloader-config`, chown 1000, survives container recreation). Every run thereafter is `-username <user> -remember-password`, unattended.
- **Re-auth** is needed only when Steam invalidates the token (password change, device deauth, natural expiry — months). The fetcher raises a typed `SteamAuthError` on a missing/expired session and surfaces "re-auth needed" via the same live signal `/health` + `/api/v1/platforms` already use (PR #208) — never a silent failure.
- **SECURITY (must hold):** the Steam password, Steam Guard 2FA, `shared_secret`, and any token/login-key are **never** echoed, logged, or written by our code to any file we control. One login per run (no per-app re-login — the whole point).

## Architecture

A manifest-only fetcher on the **agent** (it already has Steam egress, the durable archive mount, and the .NET runtime SteamPrefill needs), triggered from the control plane, plus an operator go-live runbook.

### Component A — `DepotDownloaderManifestFetcher` (agent)

New module `src/orchestrator/platform/steam/manifest_fetcher.py` (same package as `prefill_driver.py`, which the agent already consumes), STDLIB + subprocess only; **must not import `orchestrator.api.main` or `orchestrator.db.pool`** (import-isolation guard, `tests/agent/test_import_isolation.py`).

- `login_from_session()` — verify a usable session (reused SteamPrefill session per S2, else DD's remembered login); raise typed `SteamAuthError` on missing/expired.
- `fetch_all(app_ids) -> FetchResult` — **one login for the whole run**; for each app:
  - resolve the **cached** depot/gid set to fetch (from SteamPrefill's `successfullyDownloadedDepots.json`, so the fetched manifest is **gid-aligned with the on-disk chunks** — consistent with the #209 gid-match; S3 resolves the gid↔depot mechanics),
  - invoke DepotDownloader `-manifest-only` for those depots/manifests,
  - parse the chunk SHA‑1s and write `{app}_{app}_{depot}_{gid}.shas` (newline SHA‑1 per chunk) into `settings.steam_manifest_archive_dir/v1`,
  - **idempotent skip-if-exists** (already-archived `{app}_{app}_{depot}_{gid}.shas`/`.bin` ⇒ skip),
  - **per-app `except Exception`** isolation (one bad app counts + continues),
  - small inter-request delay (rate-limit safety).
- A **hard `except BaseException` boundary** around the whole run so a `gevent.Timeout`-style escape (or any subprocess oddity) can never kill the agent — the ③ lesson.
- Returns counts: fetched / skipped / failed / auth-state.

### Component B — output format (`.shas` sidecar, zero validator change)

DepotDownloader emits a human-readable text manifest, **not** SteamPrefill's protobuf `.bin`. The fetcher parses it to a `.shas` sidecar (`{app}_{app}_{depot}_{gid}.shas`, one SHA‑1 per line) — which the validator **already consumes**: `agent/routers/steam.py:steam_validate` has the `if binpath.suffix == ".shas": parse_shas(...)` branch, and `manifest_locator` globs `_MANIFEST_EXTS` (`.bin` + `.shas`) with newest-per-depot-by-mtime + the #209 `prefilled_gids` preference. So **no `manifest_parser`/`manifest_locator`/validator change is required** — the fetcher just writes `.shas` into the archive the union-read already covers. (S1 confirms DD's output carries the chunk SHA‑1s and locks the parse.)

### Component C — trigger (control plane + CLI) + enumeration

- **Agent self-enumerates the set each run** from its local SteamPrefill records: the app→[gid] map in `successfullyDownloadedDepots.json` (the apps+gids whose chunks are actually cached, the precise validate-what's-cached set), optionally unioned with `selectedAppsToPrefill.json` (selected-but-not-yet-downloaded). **No app-id list crosses the wire and nothing is hardcoded** — so the covered set tracks Karl's library automatically (auto-grows as the cron downloads new games). This also keeps the fetch gid-aligned (S3) without a control-plane round-trip.
- **Agent** `POST /v1/steam/fetch-manifests` (bearer-gated; no body needed — the agent reads its own records; runs `fetch_all` as a background job returning `job_id`, mirroring the existing agent prefill/pull job pattern in `agent/routers` + `agent/jobs.py`).
- **Control plane** new job kind + CLI `orchestrator-cli cache fetch-manifests` — just *triggers* the agent run (it does not need to enumerate; the agent does).
- **Weekly cron** re-runs it to keep manifests current (unattended per the auth model).

### Component D — packaging

Pin the DepotDownloader binary (exact version) into the agent image — a lean, focused addition (DD is self-contained .NET 8; **not** a revival of the deleted full venv-steam-worker stage). pip-audit/licenses unaffected (it's a binary, not a Python dep).

## Data flow (steady state)

1. Karl's nightly cron prefills chunks (unchanged).
2. **Weekly fetch-manifests run:** one login → for every selected app, fetch the cached gids' manifests via `-manifest-only` → write `.shas` into the durable archive (idempotent).
3. `validate` reads `union(live cache, archive)`, pins to the prefilled gid (#209) → validates any app whose manifest is now archived.
4. F13 sweep re-checks drift (unchanged); `cache validate-all` after a fetch lights up the whole library.

## Phase 0 spikes (each produces a written finding that may adjust the build)

- **S1 — output/parse.** Run DD `-manifest-only` for one real app; confirm the emitted manifest contains the per-chunk SHA‑1s the validator's cache-key needs; lock the `.shas` parse. (If the format is unexpectedly incompatible, fall back to emitting `.bin` — but `.shas` is strongly preferred and already supported.)
- **S2 — auth reuse.** Determine whether DepotDownloader can **reuse SteamPrefill's persisted session** (no new login). If yes, that path is used (no second 2FA). If no, document DD's own `-remember-password` one-time-login flow.
- **S3 — gid alignment.** Confirm the fetcher fetches the **cached** gid's manifest (resolve gid↔depot from `successfullyDownloadedDepots.json` / product-info), and cross-check one app's fetched manifest against on-disk chunks + a live `validate` so the computed lancache cache-key paths match real files.

## Testing (TDD)

- Unit tests use a **mocked DepotDownloader client** — **no live Steam in unit tests**.
- `manifest_fetcher`: one login per run; per-app isolation (one failure counts + continues); idempotent skip-if-exists; `.shas` written with correct `{app}_{app}_{depot}_{gid}` name; typed `SteamAuthError` on missing session; the `except BaseException` boundary; no secret in any written file or log.
- Trigger: agent endpoint 401 without bearer, enqueues a fetch job; CLI posts correctly; control job kind enqueues.
- Settings: new fields default + env override.
- Agent import-isolation guard stays green.

## Operator go-live (post-merge; Claude runs the boxes, only the 2FA — if needed — is Karl's)

1. Deploy the new agent image; mount `/depotdownloader-config` (chown 1000).
2. **Auth:** if S2 reuse works, none needed; else Karl runs the one-time interactive `-remember-password` + 2FA login once.
3. Run `orchestrator-cli cache fetch-manifests` over all 1077 (one login, minutes).
4. Run `orchestrator-cli cache validate-all`; report the before/after status histogram and reconcile against the ~1077 expectation.
5. Add the weekly cron.

## Security constraints (must hold)

Never echo/log/store the Steam password, Steam Guard 2FA, `shared_secret`, or any token/login-key. One login per run. NAS sudo is password-gated (privileged docker / Karl runs sudo). Agent is uid 1000.

## Scope

**Phase 0 spikes** (resolve S1–S3, record findings) gate **Phase 1 build** (TDD, mocked DD), shipped as **1 PR** (`feat/steam-manifest-fetcher-dd`): the agent fetcher (A + B), the trigger (C), and packaging (D). The operator go-live is executed post-merge and monitored — not code. The shipped force-prefill "seed" path remains as a fallback.
