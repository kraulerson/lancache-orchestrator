# Design — Manifest fetcher: stop writing `.shas` for shared redistributable depots

<!-- Last Updated: 2026-07-05 -->

**Status:** Proposed (the "fetcher fix later" follow-up to PR #245).
**Related:** PR #245 (validator/purge exclude `steam_shared_redist_depots`), [[project_shared_redist_false_partial]], the steam manifest fetcher (PR #213, `platform/steam/manifest_fetcher.py`).

## Problem

The `DepotDownloaderManifestFetcher` runs `DepotDownloader -app {app} -manifest-only`, which
downloads a `.manifest` for **every** depot DepotDownloader resolves for the app — including the
shared **Steamworks Common Redistributables** depots (app 228980: VC++/DirectX runtime, depots
228981–228990) that games reference via `depotfromapp`. `_run_manifest_only` then walks every
`.manifest` file and `_write_shas` writes a `{app}_{app}_{depot}_{gid}.shas` sidecar for each,
**including the shared redist depots.**

Those sidecars are what dragged ~50 fully-cached games to a false "partial" on 2026-07-04: the
shared depots are only partially cached and, being `present > 0`, escaped the validator's
"drop zero-present depots" rule.

**PR #245 already fixes the *symptom*** — steam validate + purge now skip
`settings.steam_shared_redist_depots` (default 228981–228990) at read time, so the redundant
sidecars are ignored and the games read as `cached`. This design addresses the *source*: stop
producing the redundant sidecars, and clean up the ones already on disk — so correctness no longer
depends on a read-time exclusion, and the archive isn't accumulating manifests for content that is
never a game's own data.

## Goals / non-goals

- **Goal:** the fetcher never writes `.shas` for a shared-redist depot; a one-time cleanup removes
  existing redist sidecars from the archive.
- **Goal:** single source of truth — reuse the same `steam_shared_redist_depots` setting the
  validator uses, so the two can never drift.
- **Non-goal (this pass):** fully general `depotfromapp` detection (see "Alternatives"). The fixed
  set covers the dominant case (depot 228990 alone was in 40 of the 50 games); the long tail is
  already harmless post-#245.

## Design

### Part A — source prevention (skip at write time)

In `manifest_fetcher.py`, thread the shared-redist set into the fetcher and skip those depots
before `_write_shas`:

- `DepotDownloaderManifestFetcher.__init__` gains `shared_redist_depots: frozenset[int]`, passed
  from `settings.steam_shared_redist_depots` at construction (the agent app wiring that builds the
  fetcher already has `settings`).
- In the `_run_manifest_only` result loop (where each `(depot_id, gid, shas)` is collected) **or**
  in `fetch_all` before `_write_shas`, skip any `depot_id in self._shared_redist_depots`:
  ```python
  for depot_id, gid, shas in self._run_with_retry(app_id):
      if depot_id in self._shared_redist_depots:
          _log.info("fetch_manifests.redist_depot_skipped", app_id=app_id, depot=depot_id)
          continue
      if self._write_shas(app_id, depot_id, gid, shas):
          written += 1
  ```
- `FetchResult` counters unchanged; a skipped redist depot simply isn't counted as written.

**Test:** a fake `_run_manifest_only` returning one own depot + one redist depot (228990) → only the
own depot's `.shas` is written; the redist file is absent. (Mirror the existing
`tests/…/test_manifest_fetcher.py` pattern that stubs the DD subprocess.)

### Part B — one-time cleanup of existing redist sidecars

Existing redist `.shas` files already sit in the archive (`<archive>/v1/*_*_{228981..228990}_*.shas`)
and in any live SteamPrefill cache the locator reads. A small idempotent cleanup removes them:

- A `--cleanup-redist` mode on the fetcher (or a standalone `scripts/` one-shot) that globs
  `v1/` for `*_*_{depot}_*.shas` where `depot ∈ shared_redist_depots` and unlinks them, logging a
  count. Idempotent (re-running finds nothing). Run once on the agent after deploy.
- Safe: these sidecars are pure derived data (re-fetchable), and after Part A they won't be
  re-created. The validator already ignores them, so removal only reclaims storage + tidies the
  archive; it changes no validation outcome.

**Test:** seed a dir with own-depot + redist-depot `.shas`; run cleanup; assert only redist files
removed, own files intact.

### Rollout

1. Ship Part A + Part B behind the existing `steam_shared_redist_depots` setting.
2. Deploy the agent; run the cleanup once; the next fetch produces no new redist sidecars.
3. **Optional, later:** once confident, the validator's read-time exclusion (#245) can stay as a
   cheap defense-in-depth (recommended — keep both; they share the setting so there's no drift).

## Alternatives considered

- **General `depotfromapp` detection (the "proper" fix).** Query each app's depot config from Steam
  PICS product info (SteamKit — the codebase already uses it in `steamkit_manifest_parser.py`) and
  write `.shas` only for depots the app **owns** (no `depotfromapp` / not `sharedinstall`). This
  catches *every* shared depot, not just the 228980 set (the observed long tail — 228758, 229006,
  229020, 229032, 229619 — would be handled automatically). **Deferred** because it adds a PICS
  product-info round-trip per app to a fetcher that currently only shells out to DepotDownloader,
  and the fixed-set + #245 already make the long tail harmless (those games validate `cached`
  today). Revisit if a shared depot outside the 228980 set is ever found to cause a real partial.
- **Do nothing (rely on #245 alone).** Viable — #245 makes the sidecars harmless. Rejected as the
  standing state because the archive keeps accumulating manifests for non-game content and
  correctness stays coupled to a hardcoded read-time list. Part A/B is cheap hygiene that decouples
  it.
- **Tell DepotDownloader to skip shared depots.** No clean `-manifest-only`-compatible flag to
  exclude `depotfromapp` depots; DD downloads the full resolved depot set. Filtering at our
  write step is simpler and in our control.

## Estimated size

Small: ~1 settings thread-through + a 3-line skip in the fetch loop + a ~15-line cleanup routine,
plus two unit tests. No migration, no API/CLI surface change. Agent-only deploy + one cleanup run.
