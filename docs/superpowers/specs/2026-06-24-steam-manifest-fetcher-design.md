# Independent Steam Manifest Fetcher — Design

**Date:** 2026-06-24
**Status:** Approved (design)
**Follows:** durable manifest store (PR #200, [[project_validation_manifest_gap]]); re-arch ③c worker deletion (commit 49e12d4)

## Problem

The durable manifest store (PR #200) is live, but ~673 of Karl's stable Steam apps still have no manifest, so the validator can't check their (already-cached) chunks. Seeding manifests via SteamPrefill failed two ways, proven live 2026-06-24:

- **Per-app harvest** (`prefill --force` narrowed to one app, killed after the `.bin` lands) hits Steam **`RateLimitExceeded`** after ~118 rapid logins — each invocation re-authenticates — and risks the account.
- **One-login `prefill --force`** forces the full chunk re-download: ~5 TB of *already-cached* data re-read over the LAN to `/dev/null` at ~50 MB/s ≈ 30–40 h. SteamPrefill has no manifest-only mode.

Both are dead ends because SteamPrefill couples manifest acquisition to chunk download, and re-logins are rate-limited.

## Goal

A fetcher that does **one** Steam login and pulls **manifests only** (the chunk lists) for the owned Steam library, writing them into the durable archive so the F7 validator covers everything — including never-prefilled apps. One login (no rate-limit), manifests only (no chunk re-read), runs in minutes.

## Non-goals

- Epic (no Epic disk-stat validator yet — separate deferred feature).
- Replacing SteamPrefill prefill (the agent still prefills/warms the cache via SteamPrefill; the fetcher only sources *manifests*).
- Removing the PR #200 SteamPrefill→archive sync (the fetcher **complements** it).

## Decisions (settled)

| # | Decision | Choice |
|---|---|---|
| 1 | Library | **ValvePython/steam, asyncio interface** (no gevent — avoids the ③ `gevent.Timeout`-is-`BaseException` failure). Python-native, gives chunk-level manifests via `CDNClient`. |
| 2 | Auth | **Dedicated one-time interactive login by Karl** (username + password + Steam Guard 2FA) → ValvePython persists a session token; unattended thereafter. **Password/2FA never stored or logged** — used in-memory for that one login only. |
| 3 | Output | Verify whether the existing `parse_chunk_shas` reads ValvePython's raw manifest bytes written as `{app}_{app}_{depot}_{gid}.bin` (Steam's manifest is the same protobuf SteamPrefill caches). If yes → **zero validator change**. If not → write a `.shas` sidecar (`{app}_{app}_{depot}_{gid}.shas`, newline `<sha1>` per chunk) and add one parse branch to `manifest_locator`/`manifest_parser`. (SPIKE) |
| 4 | Enumerate | **All owned apps** (cheap — manifests only): validates the full library and distinguishes *cached* (`up_to_date`) from *owned-but-not-cached* (`validation_failed`). |
| 5 | Relationship | **Complement** the SteamPrefill→archive sync (PR #200), not replace it. Both write `/manifest-archive`; newest-`.bin`-per-depot wins. The fetcher becomes the authoritative way to keep full-library manifests current. |

## Architecture

### Component A — the fetcher (agent)
`src/orchestrator/agent/steam_manifest_fetcher.py` (new). A class wrapping ValvePython:

- **`login_from_session()`** — log in using the persisted session token (no password, no 2FA). Raises a typed `SteamAuthError` if the session is missing/expired (operator must re-run the one-time login).
- **`fetch_all(app_ids: list[int]) -> FetchResult`** — for each app: `get_product_info([app])` → current depots + manifest GIDs (filter to depots the account has a license for + the right OS) → `CDNClient.get_manifest(app, depot, gid)` (no content download) → serialize the manifest → write to `/manifest-archive/v1/{app}_{app}_{depot}_{gid}.<ext>`. Sequential or low-concurrency, with a small inter-request delay (polite to Steam; **no re-login** between apps). Per-app `try/except (Exception)` isolates failures (skip + count); a **hard `except BaseException` boundary** around the whole run guarantees a `gevent.Timeout`-style escape can never kill the agent (the ③ lesson).
- Writes are idempotent (skip a depot whose `{...}_{gid}.bin` already exists). Append-only to the archive.

### Component B — one-time auth setup (operator)
`src/orchestrator/agent/steam_manifest_fetcher.py::interactive_login(username)` invoked via a one-shot container command (`docker exec -it … python -m orchestrator.agent.steam_login`). Prompts for password + Steam Guard, calls ValvePython `login(user, pass, <2fa>)`, persists the session to a mounted dir (e.g. `/steam-fetcher-session`, chown 1000, **not** the SteamPrefill Config). The password string is never written to disk or logs.

### Component C — trigger (control plane)
- Agent endpoint `POST /v1/steam/fetch-manifests` (bearer-gated, allowlist as usual) → runs `fetch_all` over the supplied/owned app_ids as an agent background job (returns a job_id, poll for progress — mirrors the existing agent prefill/pull job pattern).
- Control-plane job kind + `orchestrator-cli cache fetch-manifests` (and/or `POST /api/v1/steam/fetch-manifests`) that enumerates owned steam app_ids (from the `games` table) and calls the agent. Throttled, one agent run.
- After the fetch, the existing PR #200 sync + `cache validate-all` light everything up — no new validate code.

### Component D — packaging
Re-add a lean ValvePython dependency to the agent image (a focused `requirements-steam-fetcher` or a constrained add to the agent deps — NOT the full deleted `venv-steam-worker` stage). Pin exact versions; verify the asyncio interface; keep the image lean.

## Data flow
1. (once) Karl runs the interactive login → session persisted.
2. `cache fetch-manifests` → agent: one login → for each owned app, fetch its depot manifests (no chunks) → write to `/manifest-archive/v1/`.
3. `cache validate-all` → the validator reads live∪archive, now covering the full owned library → statuses reflect true cache state.
4. (ongoing) re-run `fetch-manifests` (e.g. weekly) to keep manifests current as games update; the SteamPrefill sync continues to capture what it covers.

## Error handling & security
- **Auth secrets:** password + Steam Guard code used in-memory for the one login only; never written to disk/logs. Only the ValvePython session token is persisted (in a dedicated mounted dir, chown 1000). Session-missing/expired → typed error instructing the operator to re-run the one-time login.
- **gevent containment:** asyncio interface preferred; a `BaseException` boundary around the run regardless, so a timeout can never kill the agent (③ lesson). The fetcher runs as an isolated agent background task.
- **Idempotent + append-only:** never deletes from the archive; skips already-present depot manifests.
- **Politeness:** small inter-request delay; one login for the whole run; per-app failures isolated and counted, never abort the run.
- **Agent import isolation:** the fetcher module may import ValvePython but must not import `orchestrator.api.main`/`orchestrator.db.pool` (the agent import-isolation guard stays green).

## Spikes (front-load in the plan, before committing the integration)
- **S1 — ValvePython asyncio + CDNClient:** confirm login-from-session + `get_product_info` + `get_manifest` work via the asyncio interface without gevent, in the agent's Python 3.12 image. (Context7 `/valvepython/steam`.)
- **S2 — manifest format:** confirm whether the existing `parse_chunk_shas` reads ValvePython's serialized manifest written as `.bin`. Decide §-decision-3 (reuse `.bin` vs `.shas` sidecar) from the result.
- **S3 — owned-depot/license + manifest GID:** confirm enumerating owned app_ids + current depot/manifest-gid per app (and OS filtering) yields the same depots SteamPrefill caches (so cache-key paths match what's on disk).

## Testing (TDD)
- Fetcher core with a **mocked ValvePython client** (no live Steam in unit tests): login-from-session success/expired→typed error; `fetch_all` writes the right filenames; idempotent skip of existing; per-app exception isolated + counted; the `BaseException` boundary; OS/license filtering.
- Output: `.bin` (or `.shas`) is parseable by the (possibly extended) `manifest_parser`; `locate_manifest_bins` finds fetcher-written manifests in the archive (union read).
- Trigger: agent endpoint enqueues/runs a fetch job; CLI `cache fetch-manifests` calls it; bearer-gated (401 without token).
- Agent import-isolation guard still green.
- **Live validation (operator, post-merge):** one-time login, run `fetch-manifests` over all owned apps, run `cache validate-all`, confirm the ~673 stable apps flip to true cache state (cached/partial) and the histogram matches Karl's "over 1000 cached" expectation.

## Scope
One PR (`feat/steam-manifest-fetcher`): spikes → fetcher + auth-setup + trigger + packaging + tests. Live go-live (Karl's one-time 2FA login + the fetch + validate-all) is the post-merge operator step.
