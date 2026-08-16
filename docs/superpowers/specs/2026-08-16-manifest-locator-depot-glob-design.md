# Design — fix the manifest locator's per-depot .bin discovery glob

**Date:** 2026-08-16
**Status:** Revised after adversarial review round 2 (2026-08-16) — see §9 and §10. Core fix (§3) unchanged since round 1.
**Repo:** lancache_orchestrator. **Branch:** `fix/steam-manifest-locator-depot-glob` (PR #276)
**Found during:** live investigation of why Grim Dawn, STAR WARS Jedi: Survivor, and Battlefield 2042 remained stuck at `validation_failed` (96–99.9% cached) despite the host Steam prefill cron running every 6h. Unrelated to the Steam-prefill-ownership work in progress (`docs/superpowers/specs/2026-08-14-steam-prefill-ownership-design.md`) — this is a pre-existing defect in the read-only validate path, filed as its own change.

**Round-1 verdict: REQUEST CHANGES** — the original §6 "Rollout" understated blast radius and never mentioned `/v1/steam/purge` shares the same widened lookup. Addressed in the round-1 revision; see §9.

**Round-2 verdict: REQUEST CHANGES** — the round-1 revision itself contained a factual error (§4/§9 claimed `tests/agent/test_steam_purge.py` didn't exist; it does, with 6 tests including near-duplicate shared-redist coverage), and its two new mitigations (§6, §7) described intent without defining a real decision rule or examining operational cost. Both reviewers independently verified every claim against the live source before accepting it; both flagged real issues. §10 records round 2's findings and how each was resolved in this version.

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

The 6 files the (buggy) locator actually selects total **exactly 10784 chunks** — the precise `chunks_total` recorded in the orchestrator's database for Grim Dawn. **What this proves and what it doesn't:** the locator is deterministic, so re-running the same unmodified code against a largely-unchanged file set was always going to reproduce the same number — the match confirms *determinism*, not by itself the *diagnosis*. The actual causal evidence is the depot-by-depot naming audit above: reading the real filenames on disk and showing, per depot, which ones the glob's literal string match does and doesn't accept. The chunk-count match is strong corroboration that this mechanism — not something else — is what produces the database's current value, but it is the naming audit that establishes *why*. The same naming-mismatch pattern and the same audit method were independently applied to Jedi: Survivor and Battlefield 2042 with the same result.

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
- **Realistic multi-depot scale (round-1 finding):** one test reproducing Grim Dawn's actual shape — 9 depots, 3 distinct on-disk states (1 fresh `.bin` matching the old pattern, several `.bin` files that only the widened glob can see, a `.shas` sidecar for comparison) — asserting the full per-depot candidate set and total chunk count, not just the 2-depot toy case. Catches anything a minimal pairwise test could miss at real scale.
- **Cross-app-ID collision regression test (round-1 finding):** since collision-safety is the load-bearing safety argument for the whole fix, it needs its own explicit test, not just informal verification in this document. Construct two apps whose IDs are numeric prefixes of one another in both directions (e.g. `44` and `440`; `4400` and `440`) sharing one cache directory, and assert `locate_manifest_bins(44, ...)` never returns any of app `440`'s files and vice versa.
- The `.shas` glob is unchanged: a `.shas` file with a differing group-id-style name (hypothetical, since the fetcher never writes one) is confirmed NOT matched, proving the two extensions are scoped independently.
- All existing tests must continue to pass unmodified — this is a widening, not a behavior change, for the naming shape they already cover.

**Integration (`tests/agent/test_steam_validate.py`).** Extend the existing fixture-based test pattern (`APP, DEPOT, GID` + `sample_manifest.bin`) with a second depot under a different group id, asserting `POST /v1/steam/validate`'s `chunks_total` now includes both depots' chunks — proving the fix end-to-end through the actual endpoint, not just the locator in isolation.

**Integration (`tests/agent/test_steam_purge.py`).** `_steam_chunk_paths` (`agent/routers/steam.py:329`) is shared verbatim by `/v1/steam/validate` and `/v1/steam/purge`; §7 below explains why purge's behavior changes too.

**Correction (round-2 finding):** an earlier revision of this document claimed this test file was "previously entirely absent." That was checked and found false — `tests/agent/test_steam_purge.py` already exists (6 tests, predating this branch), including `test_steam_purge_skips_shared_redist_depot`, which already covers almost exactly what was about to be proposed as new coverage. The actual gap, correctly scoped:

- **Genuinely new:** a purge test with a differing-group-id secondary depot, asserting the widened lookup causes purge to delete that depot's chunks too (previously invisible to purge, same as to validate) — this naming shape has no existing coverage in the file.
- **Extend, don't duplicate:** the existing `test_steam_purge_skips_shared_redist_depot` already proves the shared-redist exclusion works for purge in general; it does not need a second, near-identical test. If it's cheap to do while touching the file, extend its fixture to also include a differing-group-id-named redist depot, proving the exclusion holds under the new naming shape too — otherwise leave it as-is, since the exclusion's `depot_id`-keyed logic (§7) doesn't care which glob found the file.

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

## 6. Blast radius: this changes validation for the whole library, not just 3 games (round-1 finding)

The original draft of this document reasoned only about the three known-broken games. That was wrong. The scheduled sweep's candidate query is:

```python
# src/orchestrator/jobs/handlers/sweep.py:30-34
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE status IN ('unknown','up_to_date','validation_failed') AND owned = 1 "
    "ORDER BY id"
)
```

`up_to_date` is included, not just `validation_failed`. **Every currently-`up_to_date` owned Steam game is re-validated on the very next scheduled sweep tick after this deploys**, using the widened manifest set.

For the vast majority of the library this changes nothing: a single-depot game, or a multi-depot game whose secondary depots happen to already match the old `{app_id}_{app_id}_*` pattern, gets the identical candidate set before and after. The behavior only changes for games with at least one secondary depot under a differing group ID — the same shape as Grim Dawn.

**What could happen to those games, and why it's the intended outcome, not a regression:** if a game like that currently reports `up_to_date` only because the old glob was blind to a secondary depot that is *itself* incomplete (a language pack partially evicted, say), that game's `up_to_date` status today is **already wrong** — it's the same undercounting bug, just landing on the passing side by chance instead of the failing side, as it did for Grim Dawn. The fix makes validation for that game accurate for the first time. It is not new risk introduced by this change; it is the same latent bug (present since the file's first commit, §2) becoming visible in the other direction. The three known games are simply the ones where it happened to land visibly wrong; there is no way to know how many others might flip without running the corrected code.

**Given that, this needs visibility, not a silent change left to be noticed by accident. Round-2 finding: the round-1 revision's proposed mitigation — force-triggering `orchestrator-cli cache validate-all` (`{"full": true}`) immediately post-deploy — was itself examined and rejected. `_CANDIDATE_SQL_FULL` (`sweep.py:39`) is `SELECT id, status FROM games ORDER BY id` with no `WHERE` clause at all: it validates every row in `games`, every platform, every status, unconditionally, with no cap on candidate count (`sweep_batch_size` defaults to 10 concurrent, `settings.py:256`, but nothing bounds the total). `validate_game` (`disk_stat.py:340-346`) special-cases only `platform == "epic"`; every other row — including the manually-covered GOG/Amazon/Humble/Itch games this project tracks separately — falls through to a Steam-manifest lookup that can never match, which is harmless (returns `outcome="error"`, non-clobbering) but is pure wasted load on a NAS this project's own history already documents as CPU-steal-bound. Forcing that tool immediately post-deploy is a bigger, less-targeted hammer than the problem calls for.**

**Resolution: do not force anything. The existing gated sweep already covers exactly the population that needs re-checking**, on its existing schedule, with no new trigger or CLI surface needed:

1. **Before deploying**, snapshot every owned Steam game's `status` from the database.
2. Deploy the fix.
3. Let the next scheduled gated sweep run naturally (`0 3,9,15,21 * * *` — within 6h; `_CANDIDATE_SQL` already selects `status IN ('unknown','up_to_date','validation_failed') AND owned = 1` across all platforms, `sweep.py:30-34` — this is exactly the population affected by the fix, no broader). After it completes, snapshot again and diff.
4. **Decision rule (round-2 finding: the original wording had none).** Steam coverage was independently verified at ~3 total partials library-wide before this investigation began. If more than **20** owned Steam games flip `up_to_date → validation_failed` in this one sweep, treat that as suspicious rather than confirmatory — a change of that magnitude is more consistent with a bug in the widened glob itself (e.g. the unchanged `len(parts) != 4` filter silently mishandling some real filename shape not seen in this investigation's sample) than with genuinely-hidden incompleteness at that scale. In that case: **halt before letting any downstream automation (F8 scheduled prefill, etc.) act on the new statuses**, and manually inspect a sample of the flipped games' on-disk manifests against the locator's actual output before trusting the result. Below that threshold, the flips are the expected, intended outcome described above.
5. `validation_failed → up_to_date` flips (Grim Dawn, Jedi Survivor, plus any others the old glob was undercounting favorably) confirm the fix positively; no threshold applies to that direction.

## 7. `/v1/steam/purge` shares the exact same widened lookup (round-1 finding)

`_steam_chunk_paths` (`agent/routers/steam.py:329-404`) is the single shared source of manifest→cache-path enumeration for **both** `/v1/steam/validate` and `/v1/steam/purge` (stated in its own docstring, line 333-335) — it calls `locate_manifest_bins` directly, so §3's fix applies to both callers identically, with no code change needed in either router.

`steam_purge` (`agent/routers/steam.py:501-528`) applies no depot-level scoping of its own: "every enumerated chunk across all depots (no depot-scoping — purge_chunks no-ops on paths that aren't present)" (line 516-518 comment, verified verbatim in source). After this fix, purging any Steam game with a secondary depot under a differing group ID will delete that depot's chunk files too — files the old narrow glob never even enumerated, so purge silently left them behind before. This is the same correctness direction as the validate fix (purge should act on everything a game actually owns, not on whatever a buggy glob happened to see) and is not a new category of behavior, but it was entirely unexamined and untested in the original draft.

**Checked and confirmed safe against the one sharper risk this could imply — cross-game deletion via a shared depot:** the Steamworks Common Redistributables exclusion (`settings.steam_shared_redist_depots`, PR #245) is applied *inside* `_steam_chunk_paths` itself (`agent/routers/steam.py:380-387`), keyed by `depot_id`, before any path is added to what either validate or purge sees. It is unconditional on which glob found the file. Widening the `.bin` glob does not bypass or weaken this exclusion in any way — a depot that's supposed to be protected from purge stays protected. This was verified by reading the exclusion's exact position in the shared function, not assumed from the original PR's intent.

Test coverage for this is in §4 (corrected in round 2 — see §10).

## 8. Rollout

No migration, no schema change, no settings change, no new CLI surface. Order per §6: snapshot Steam game statuses, *then* deploy, then let the next scheduled gated sweep run within its normal 6h cadence — no forced trigger — then snapshot again and apply §6's decision rule. Grim Dawn and Jedi Survivor are expected to flip to `up_to_date` in that pass. Battlefield 2042 will remain `validation_failed` regardless of this fix until separately re-added to the selection (§5) — nothing is prefilling it.

## 9. Adversarial review round 1 (2026-08-16)

An independent reviewer (fresh context, instructed to verify every claim against the real code rather than trust this document) was dispatched against this spec before implementation began. Summary of findings and resolution:

| # | Finding | Resolution |
|---|---|---|
| 1 | Rollout (old §6) reasoned about 3 games; sweep candidate query includes `up_to_date`, so the whole library is affected on the next tick | §6 rewritten with blast-radius analysis. **Superseded in round 2** — the specific mitigation proposed here (force-trigger `cache validate-all`) was itself found to be a worse tool than the alternative; see §10 finding 3 |
| 2 | `/v1/steam/purge` shares the exact same widened lookup; never mentioned | New §7: documents the behavior change, confirms the shared-redist cross-game-deletion protection is orthogonal and unaffected. Held up under round-2 re-verification |
| 3 | Possible edge case: a depot Steam has since removed from an app, but with chunks still lingering in cache (not yet evicted) | Accepted as an existing risk category. **Round 2 found the magnitude claim needed quantifying, not just categorizing** — see §10 finding 4 |
| 4 | "10784 matches the DB, not a coincidence" overclaimed what was demonstrated | §2 rewritten to separate the naming audit (causal evidence) from the chunk-count match (corroboration, not proof). Held up under round-2 re-verification |
| 5 | Testing plan gaps: no cross-app collision test, no purge test, no realistic multi-depot-scale test | Cross-app and multi-depot-scale tests added to §4 correctly. **The purge-test claim was factually wrong** — see §10 finding 1 |

Round-1 verdict: REQUEST CHANGES.

## 10. Adversarial review round 2 (2026-08-16)

A second, independent reviewer (fresh context, no knowledge of round 1's report) was dispatched against the round-1 revision, with explicit instructions to verify every claim itself rather than trust that round 1's fixes were real. It re-verified every round-1 citation (all held up) and found four new problems in the revision itself:

| # | Finding | Resolution |
|---|---|---|
| 1 | **Factually false claim:** §4/§9 stated `tests/agent/test_steam_purge.py` was "previously entirely absent." It already existed (6 tests, predates this branch), including `test_steam_purge_skips_shared_redist_depot` — near-duplicate of proposed "new" coverage. Directly checkable (`ls`) and wasn't checked. | §4 corrected: only the differing-group-id purge test is genuinely new; the existing shared-redist test may be extended for the new naming shape but is not duplicated |
| 2 | §6's before/after diff was a report with no decision rule — no threshold distinguishing "expected magnitude" from "something is wrong," no defined action either way | §6 now states an explicit threshold (20 games) and a halt-and-manually-inspect action if exceeded, grounded in this investigation's own baseline (~3 total partials library-wide before the fix) |
| 3 | The proposed rollout mitigation (force-triggering `cache validate-all`, i.e. `{"full": true}`) is a bigger, costlier tool than examined: `_CANDIDATE_SQL_FULL` has no `WHERE` clause (all games, all platforms, all statuses), no cap on total candidate count, and every non-Steam/non-Epic game wastes a guaranteed-`error` Steam lookup — unexamined cost on a NAS this project's own history documents as CPU-steal-bound | §6 rewritten: no forced trigger at all. The existing gated sweep (already scheduled every 6h) already selects exactly the affected population (`status IN (...) AND owned=1`, all platforms) — observe its next natural run instead of manufacturing a bigger one |
| 4 | Finding #3's (round-1) "same category, not new" framing for the stale-removed-depot risk undercounted the actual change: exposure goes from 1 depot per game (old glob) to up to N depots (Grim Dawn: 9) — a real multiplier, not just an unchanged category | §9 finding 3 annotation above updated to point here. Quantified explicitly: this is a real, roughly N-fold increase in per-game exposure for an N-depot game, not merely a relabeled identical risk. Still no code mitigation added — this is presented to the Orchestrator as a quantified, explicit decision (§11), not resolved unilaterally |

Round-2 verdict: REQUEST CHANGES. This revision addresses every item above.

## 11. Open questions

None block sending this back for a third review round. One item is explicitly the Orchestrator's call rather than a default I've picked:

- **Finding #3/§10-4 (stale-removed-depot exposure multiplier):** no code mitigation is proposed. Options if the Orchestrator wants one: (a) accept as-is, matching this document's current default; (b) log when a selected manifest's mtime is old relative to the game's other depots, as a cheap detection signal without changing behavior; (c) something more involved (e.g. cross-checking depot IDs against a live Steam API) — not recommended, disproportionate to a rare edge case. No action needed to proceed with (a); flagging so the choice is deliberate, not silent.
