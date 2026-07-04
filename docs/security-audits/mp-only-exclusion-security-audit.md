# Security Audit — MP-only prefill exclusion (#366)

**Feature:** mp-only-exclusion — flag multiplayer-only Steam games (a `game` with a
multiplayer category and NO single-player category, e.g. Dota 2) as prefill-exclusion
candidates. Migration 0013 adds `has_single_player`/`has_multiplayer` to `steam_app_info`;
`store.fetch_app_info` widens the appdetails fetch to `filters=basic,categories` and derives
the flags; `library_sync` backfills them (budget-bound); `classify()` gains an MP-only rule;
`auto_classify_block` + the selection-candidates endpoint pass the flags through.
**Modules:** `platform/steam/store.py`, `platform/steam/selection_classifier.py`,
`jobs/handlers/library_sync.py`, `scheduler/jobs.py`, `api/routers/selection.py`,
`db/migrations/0013_steam_app_info_categories.sql`
**Audit date:** 2026-07-04 · **Auditor:** self-review (Senior Security Engineer persona) + ruff (S) + mypy + full suite (1495) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
ruff `--select S` clean, mypy clean (98 files), all affected suites + full suite green
(1495 passed; only the pre-existing `test_licenses` tooling gap fails locally). Threat
cross-check: SSRF/outbound request, SQL injection, availability/DoS, data integrity,
migration data-loss, false-exclusion.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **No new outbound surface / no SSRF.** `fetch_app_info` still targets the fixed
  `store.steampowered.com/api/appdetails` host; only the `filters` query value changes
  (`basic` → `basic,categories`) and `appids` is an `int`-cast app_id. No URL, host, or
  redirect is caller-controlled. Same 15s timeout and same `steam_store_fetch_budget`
  rate cap.
- **No SQL injection.** Migration 0013 is static `ADD COLUMN` DDL. The library_sync upsert
  and the auto_classify_block / selection SELECTs bind the flags as `?` params; column
  names are static. `classify()` is pure (no DB, no I/O).
- **Malformed-data safe.** `_category_flags` guards every access with `isinstance`
  (list, dict, int) and returns `(None, None)` for absent/garbage categories — a hostile or
  malformed store response cannot crash the fetch or mis-set a flag.
- **No false exclusion.** `classify()` flags `multiplayer-only` ONLY when BOTH flags are
  known and mp=1, sp=0. A game whose categories haven't been fetched (NULL) or that carries
  no gameplay categories is never guessed as MP-only — proven by
  `test_unknown_flags_never_flag_mp_only` / `test_keeps_game_with_unfetched_flags`. And an
  auto-flagged game is still only a **candidate**: it rides the existing
  `prefill_exclusions` path (ON CONFLICT DO NOTHING never overrides an operator `allow`),
  and the operator can `selection allow` it back.
- **Availability.** The backfill re-fetch of NULL-flag rows is bounded by the existing
  per-run `steam_store_fetch_budget`; it self-heals over successive syncs and never blocks.
  A genuinely category-less game re-fetches each run (bounded by budget) — negligible.
- **Migration is loss-less + tamper-pinned.** Plain nullable `ADD COLUMN` (no rebuild):
  existing rows get NULL, no data touched; the new checksum is pinned in `CHECKSUMS`.
  Verified by `test_row_without_flags_defaults_null` + the full suite migrating every fixture.

## Decision
No findings. Ship. (Control-plane-only; deploy is a `git pull` + rebuild + recreate on
LXC 1105 — migration 0013 applies at boot; library_sync backfills category flags over
subsequent syncs, then MP-only games are auto-flagged and pruned via Piece 1.)
