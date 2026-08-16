# Design — fix the manifest locator's per-depot .bin discovery glob

**Date:** 2026-08-16
**Status:** Approved (design) — 2026-08-16
**Repo:** lancache_orchestrator. **Branch:** `fix/steam-manifest-locator-depot-glob` (not yet created)
**Found during:** live investigation of why Grim Dawn, STAR WARS Jedi: Survivor, and Battlefield 2042 remained stuck at `validation_failed` (96–99.9% cached) despite the host Steam prefill cron running every 6h. Unrelated to the Steam-prefill-ownership work in progress (`docs/superpowers/specs/2026-08-14-steam-prefill-ownership-design.md`) — this is a pre-existing defect in the read-only validate path, filed as its own change.

---

## 1. Problem

Three Steam games sat at `validation_failed` for over a week with no improvement, despite `run-steam-prefill.sh` completing successfully on every 6-hourly tick. `app.log` on the NAS shows Grim Dawn's full 9-depot, 12.44 GiB download completing cleanly on every recent run (`Finished downloading... Prefill complete!`). The games are not actually under-cached — **the validator is checking the wrong data and can never see the successful downloads.**

## 2. Root cause (verified live, not inferred)

`src/orchestrator/agent/manifest_locator.py::locate_manifest_bins` globs SteamPrefill's `.bin` manifest files as:

```python
for path in v1.glob(f"{app_id}_{app_id}_*.{ext}"):
```

This assumes every depot's manifest filename repeats the app ID twice: `{app_id}_{app_id}_{depot}_{gid}.bin`. That holds only for a game's *primary* depot. SteamPrefill names secondary depots `{app_id}_{depotGroupId}_{depotId}_{gid}.bin`, where `depotGroupId` is frequently a **different** number (a Steam-internal grouping, not the app ID). The glob silently excludes every one of those files from consideration — they are never even seen as candidates, let alone compared by mtime.

**Confirmed by running the real locator code against the live filesystem** (not a synthetic repro):

```
=== Grim Dawn (219990) ===
  depot=219991  parts[1]={'219990'}  MATCHES glob
  depot=229003  parts[1]={'228980'}  EXCLUDED by glob
  depot=2699230 parts[1]={'2699230'} EXCLUDED by glob
  depot=2699231 parts[1]={'2699230'} EXCLUDED by glob
  depot=483840  parts[1]={'483840'}  EXCLUDED by glob
  depot=642280  parts[1]={'642280'}  EXCLUDED by glob
  depot=642281  parts[1]={'642280'}  EXCLUDED by glob
  depot=897670  parts[1]={'897670'}  EXCLUDED by glob
  depot=897671  parts[1]={'897670'}  EXCLUDED by glob
```

Of 9 depots, only 1 (219991) matches. For the other 8, the locator falls back to whatever candidate *does* match the glob — for 5 of them that's a `.shas` sidecar **51 days old** (from the one-time DepotDownloader gap-filler, issue #213); for the remaining 3 (229003, 2699230, 2699231) there is no matching candidate at all, so those depots are silently absent from validation entirely.

The 6 files the (buggy) locator actually selects total **exactly 10784 chunks** — the precise `chunks_total` recorded in the orchestrator's database for Grim Dawn. That is not a coincidence; it is the direct mechanism. The same pattern was independently confirmed for Jedi: Survivor and Battlefield 2042.

This bug predates every later fix in this file's history — it was present in the very first commit that introduced this validation path (`157968e feat(steam): agent /v1/steam/validate via SteamPrefill manifests`), before the prefilled-gid pinning fix (`43e77db`) or the manifest archive (`f48ff2a`). It is not a regression from recent work.

### Why the prefilled-gid pin doesn't save it

`locate_manifest_bins` accepts a `prefilled_gids` set — when a depot's manifest gid is in that set, it's preferred over pure newest-by-mtime, pinning validation to the version SteamPrefill actually recorded downloading. This pin comes from `Config/successfullyDownloadedDepots.json`. All three games are **completely absent** from that file, despite `app.log` recording repeated successful completions — so the pin never engages, and the broken glob's newest-by-mtime fallback runs unopposed. This absence is not explained by the glob bug and is addressed separately in §5 below; the fix in this document does not depend on resolving it.

## 3. Fix

Broaden **only** the `.bin` glob to match on the app ID once, not twice:

```python
for path in v1.glob(f"{app_id}_*.{ext}"):
```

The `.shas` glob is unchanged — it stays `f"{app_id}_{app_id}_*.{ext}"`. That extension is written exclusively by the orchestrator's own DepotDownloader-based fetcher (`platform/steam/manifest_fetcher.py`), whose naming convention is fixed and already correct; broadening it would only risk matching something unintended for no benefit.

**Why the broader glob is safe (verified, not assumed):** a glob prefix match requires the literal next character after the matched digits to be `_`. `f"{219990}_*"` cannot match `2199905_...` or any other app's files — the character immediately following `219990` in that filename is `5`, not `_`. Checked against the live `/steamprefill-cache` and `/manifest-archive` directories (4700+ and equivalent files respectively) with no collision. `list_prefilled_app_ids` in the same module already uses this unscoped style (`v1.glob(f"*.{ext}")` + manual first-segment split) and does not have this bug — only the per-app depot lookup in `locate_manifest_bins` does.

`len(parts) != 4: continue` stays unchanged — every real filename observed, `.bin` or `.shas`, is exactly 4 underscore-separated segments (app, group-or-app, depot, gid).

## 4. Testing

**Unit (`tests/agent/test_manifest_locator.py`).** Every existing test in this file uses `{app}_{app}_{depot}_{gid}` naming — which is exactly why this bug shipped invisibly from the first commit: there has never been a test exercising the differing-group-id shape that real multi-depot SteamPrefill output actually produces. New cases:

- A depot whose `.bin` filename has a group-id segment different from the app id must now be found (previously silently excluded).
- A mix of one matching-pattern depot and one differing-pattern depot for the same app — both found, matching the real Grim Dawn shape (1 depot matches the old pattern, 8 don't).
- The `.shas` glob is unchanged: a `.shas` file with a differing group-id-style name (hypothetical, since the fetcher never writes one) is confirmed NOT matched, proving the two extensions are scoped independently.
- All existing tests must continue to pass unmodified — this is a widening, not a behavior change, for the naming shape they already cover.

**Integration (`tests/agent/test_steam_validate.py`).** Extend the existing fixture-based test pattern (`APP, DEPOT, GID` + `sample_manifest.bin`) with a second depot under a different group id, asserting `POST /v1/steam/validate`'s `chunks_total` now includes both depots' chunks — proving the fix end-to-end through the actual endpoint, not just the locator in isolation.

## 5. The `successfullyDownloadedDepots.json` gap (investigated, not fixed here)

Checked all five `selectedAppsToPrefill.json` backups on the NAS (Jun 24 → Aug 9) for the three games' presence:

| App | Jun 24 | Jun 26 | Jul 5 | Aug 3 (pre-surgical) | Aug 9 (current) |
|---|---|---|---|---|---|
| Grim Dawn (219990) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Jedi Survivor (1774580) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Battlefield 2042 (1517290) | ✗ | ✗ | ✗ | ✓ | ✗ |

**Grim Dawn and Jedi Survivor have been continuously selected for 2+ months** — this rules out selection-list churn (removal/re-add resetting SteamPrefill's own bookkeeping) as the explanation for their missing `successfullyDownloadedDepots.json` entries, despite `app.log` recording repeated successful full downloads. No other state file exists in `SteamPrefill/` or `SteamPrefill/Config/` on the NAS that could hold an alternate record. The remaining explanation lives inside SteamPrefill's own external .NET binary, which is out of this repo's control and not further diagnosable without live experimentation on the production prefill process (adding verbose flags, re-running) — out of scope for this investigation.

**Battlefield 2042 is a separate, simpler problem, unrelated to the glob bug's root cause:** present in the `pre-surgical-20260804` snapshot, absent from every backup since. It was deliberately removed from the selection around Aug 4–9 and never re-added — nothing has attempted to prefill it since, independent of anything in this document. Re-adding it to `selectedAppsToPrefill.json` is an operator action, not a code change, and is explicitly **not** part of this fix.

The glob fix in §3 makes the JSON gap moot for validation *correctness* regardless of whether it is ever explained: once every depot's manifest is visible to the locator, newest-by-mtime alone picks the currently-prefilled version correctly, with or without the gid pin.

## 6. Rollout

No migration, no schema change, no settings change. Once deployed, the next scheduled validation sweep (`0 3,9,15,21 * * *`, gated to include `validation_failed` games) re-validates all three games automatically. Grim Dawn and Jedi Survivor should flip to `up_to_date` within one sweep cycle post-deploy — no manual trigger needed. Battlefield 2042 will remain `validation_failed` until separately re-added to the selection (§5), since nothing is prefilling it regardless of this fix.

## 7. Open questions

None. Scope, fix, and test plan are fully settled.
