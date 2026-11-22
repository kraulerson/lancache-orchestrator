# ADR-0020: First-Seen Time, Not Purchase Date, Defines a "New" Game

<!-- Last Updated: 2026-11-21 -->

**Status:** Accepted — 2026-11-21 (Orchestrator: Karl Raulerson).
Design: `docs/superpowers/specs/2026-11-20-new-not-cached-design.md`.
Plan: `docs/superpowers/plans/2026-11-21-new-not-cached-feature-a.md`.

---

## 1. Context

A game bought on 2026-11-19 showed Unknown in Game_shelf until the weekly sweep
reached it, and a new Epic purchase waited up to a week to be downloaded, because
scheduled Epic prefill downloads only measured games. To measure new games soon,
and to alert when one never caches, the orchestrator needs a definition of "new".

The orchestrator has no store purchase date. A Steam row appears only once
SteamPrefill has written a manifest, and library sync is the only insert path.

## 2. Decision

A game is **new** for `new_game_window_days` (7) after the orchestrator first
sees it. Migration 0019 adds `games.first_seen_at`:

- Library sync stamps it on INSERT only. It is absent from every
  `DO UPDATE SET`, so a re-sync never rewrites it.
- Rows that exist at migration time keep NULL and are never new. No backfill.
- A game un-owned and then owned again is not new: its row already exists.
- Other insert paths (manual coverage, tests) leave it NULL.
- `is_new` in the API also requires the game to be neither blocked nor excluded.

## 3. Alternatives considered

- **Store purchase dates.** Rejected (a non-goal of the design): the orchestrator
  does not hold them, and a purchase date says nothing about whether the
  orchestrator can measure the game yet.
- **Backfill `first_seen_at` for existing rows.** Rejected: about 3,270 existing
  rows (2026-11-20) would show "New - Not Cached" at deploy, and each would be measured. The
  anti-goal is the whole library turning new.
- **Re-check every `unknown` game.** Rejected: a game measured before its download
  would drop out of the set and keep a wrong label that sticks.

## 4. Consequences

- The label is honest about what the system knows: "first seen here", not "bought
  on".
- A Steam purchase SteamPrefill never starts has no row, so it is never new and
  never alerts (accepted gap S1). Closing it needs Game_shelf as an ownership
  source (ADR-0016, ADR-0018, both Proposed); #392 tracks it.
- Pre-0019 games that are uncached are covered only by the weekly sweep, as
  before.
- A new Epic purchase becomes eligible for scheduled prefill within hours (an
  intended Changed entry).

## References

- `docs/superpowers/specs/2026-11-20-new-not-cached-design.md`
- [ADR-0016], [ADR-0018] — ownership as an explicit input (Proposed)
- [ADR-0021] — the overdue alert never times out

[ADR-0016]: 0016-ownership-as-an-explicit-input.md
[ADR-0018]: 0018-standalone-ownership-service.md
[ADR-0021]: 0021-overdue-alert-never-times-out.md
