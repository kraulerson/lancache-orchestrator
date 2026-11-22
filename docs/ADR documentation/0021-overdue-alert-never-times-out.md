# ADR-0021: The New-Game Overdue Alert Never Times Out

<!-- Last Updated: 2026-11-21 -->

**Status:** Accepted — 2026-11-21 (Orchestrator: Karl Raulerson).
Design: `docs/superpowers/specs/2026-11-20-new-not-cached-design.md`.
Related: [ADR-0020].

---

## 1. Context

A new game still not cached after 7 days almost always means its automatic
download failed. A game still `unknown` 48 h after it was first seen means
measuring is failing. Both need a person to look. The risk of any alert is that
it stays red until people learn to ignore it.

## 2. Decision

`orch:new-games-overdue` is DOWN while any game is overdue and UP otherwise, with
**no time-out**. A game leaves the overdue set only when it is cached
(`pending_update` counts), blocked, or excluded (`mode='exclude'`).

- Overdue means: first seen more than the window ago and status not `up_to_date`
  or `pending_update`; or `unknown` and first seen more than 48 h ago. Rows with
  NULL `first_seen_at` are never overdue.
- Blocking or excluding clears it, whatever set the block: an operator, #388's
  auto-block after repeated manifest 4xx, or Game_shelf's automatic exclusions.
  Neither is silent, so the SQL need not tell sources apart.
- Two monitors, not one: `job:orch-new-games-check` is liveness only (scheduler
  runs), `orch:new-games-overdue` is state only (scheduler and trigger runs). One
  monitor cannot tell "the check stopped" from "a game is overdue" (the
  #326/#330/#337 defect class; the keys_zone 219/220 split).
- The message lists the titles with a reason (`not cached` or `unmeasured`),
  capped at the heartbeat message limit (200 characters) with a `+N more` tail.

## 3. Alternatives considered

- **Time out the alert after N days.** Rejected: a failed download needs a
  decision, and a time-out is a silent fallback.
- **Let the game quietly become "Not cached".** Rejected for the same reason.
- **One monitor for liveness and overdue.** Rejected as above.

## 4. Consequences

- The bounded fix for "a red monitor people ignore" is the operator's act:
  block or exclude the game, or fix the download.
- Fixing the download clears the alert without further action: scheduler runs
  also re-measure every game overdue as `not cached`, so the next scheduled
  check (at most 6 h away) measures the now-cached game and the monitor goes UP.
  `orchestrator-cli game validate <id>` measures it immediately; the monitor then
  updates at the next scheduled or trigger check. Trigger, API and CLI runs do
  not re-measure overdue games.
- Kuma notifies only on a status change, so a second game going overdue while the
  monitor is DOWN sends no new notification. The message still lists it, and a
  `new_games.overdue` log line is written per game per run.
- An API- or CLI-sourced run never pushes the overdue monitor.

## References

- `docs/superpowers/specs/2026-11-20-new-not-cached-design.md`
- `docs/deploy/live-configuration.md` section 3.1

[ADR-0020]: 0020-first-seen-defines-new.md
