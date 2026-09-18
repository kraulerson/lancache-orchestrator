# Next session — handoff

**Written:** 2026-09-18, end of the 2026-09-15 → 18 session.
**Not** the Phase 4 `HANDOFF.md` artifact (that is a release deliverable). This is
a working handoff: what is live, what is blocked, and what to pick up.

---

## Read this first — three things that will confuse you

### 1. The feature gate is BLOCKED and that is correct

```
scripts/test-gate.sh --check-batch
[FAIL] Testing session required (2 features since last test, interval is 2)
```

Do **not** start a new feature until this clears. UAT session 16 ran but did not
close: 9 of 9 scenarios executed, **7 pass, 1 partial, 1 FAIL**. The gate should
stay shut until the failure's disposition is settled.

### 2. `process-checklist.sh --status` will tell you "Session: 15, 9/9"

That is wrong, and it is a branch artifact. **UAT 16's state and artifacts live on
the unmerged branch `uat/session-16`** (8 commits ahead of main). Karl asked for
that PR to be held until the session's work finished. It now has: three agent
reports, the tester template, the results with all nine scenarios resolved, and
the triage with his sign-off.

**Decide early:** merge `uat/session-16`, or keep holding it. Leaving it unmerged
means the checklist keeps lying about which session is current.

### 3. Four "monitors" share one defect, and it is the theme of this project

A monitor that observes an event correctly and **cannot convey what it means**:

| issue | monitor | what it cannot distinguish |
|---|---|---|
| #326 | Kuma 180 sweep | "schedule running" vs "library covered" |
| #330 | Kuma 181 Epic prefill | "nothing to do" vs "scheduler dead" |
| #337 (fixed) | cache-catcher | commanded purge vs uncommanded eviction |
| — | `fetch_manifests` | reports UP while 716 of 1191 apps failed |

If you fix one, consider fixing them as a set. They are the same bug wearing
different hats.

---

## Live state, verified 2026-09-18

| thing | state |
|---|---|
| Schema | migration **17** |
| Sweep pass | **5**, started 2026-09-18 11:20:25 |
| Sweeps since 09-16 | **10 succeeded, 0 failed** (baseline before #311: 15 of 16 FAILED) |
| Cache truth | 1837 `up_to_date`, 1355 `not_downloaded`, 19 `failed`, 5 `unknown`, 1 `validation_failed` |
| Breaker | quiet — 0 observed downward transitions in 24 h |
| LXC disk | **44% used, 11 GB free** (was 94% / 1.3 GB) |
| NAS `/volume1` | 53% of 55 TB, 26 TB free |
| Agent | uid **0**, 256/256 buckets, healthy |

Deployed and proven this session: #311 sweep pass marker, #313 breaker heartbeat,
#332 purge count parsing, #333 breaker race, #337 cache-catcher actor, #339 purge
reversibility, plus Kuma monitor **217** (LXC disk).

---

## Priority 1 — the keys_zone alarm (no issue filed yet)

**The only outstanding item with a data-loss incident behind it.**

The 2026-07-31 mass deletion was nginx's cache-manager evicting because the
in-memory key index filled — `CACHE_INDEX_SIZE=10000m`, entirely independent of
disk space. `df` cannot see it. Karl's own memory records "key-budget alarm
REQUIRED" as an open action from that incident, and it is still open.

**Do not copy the LXC disk alarm for this.** `/volume1` is `CACHE_DISK_SIZE=54000g`
on a 55 TB volume — it is *designed* to fill and evict by LRU, so a percentage
alert fires during healthy operation. That is exactly the #326/#330 mistake.

Needs design: the metric is not in `df`. Likely sources are nginx's own stats or
inference from cache-manager behaviour. **Brainstorm it properly** — this is
architectural, not a cron one-liner.

## Priority 2 — close out UAT 16

- Decide the disposition of **scenario 9's failure** (fixed as #339, but the
  session never formally closed).
- `remediation_complete` and `gate_passed` are the two remaining steps.
- Then `scripts/test-gate.sh --reset-counter` unblocks feature work.

## Priority 3 — the open issues (8)

| # | sev | one-line |
|---|---|---|
| #312 | 3 | nine writer-guard evasions; 4, 5 and 6 are ordinary SQL a future author would write innocently |
| #315 | 3 | `VALIDATE_TIMEOUT_CEILING_SEC` is a bare constant while the rate it clamps is configurable |
| #316 | 4 | 19 games stuck at `failed`; operator passed the display — needs a decision, not a fix |
| #317 | 4 | `record_job_outcome()` still never fired in production; needs a deliberate UAT exercise |
| #326 | 4 | sweep monitor sees liveness, not coverage — **now has a real threshold: a pass is ~16.4 h** |
| #330 | 3 | Epic prefill monitor DOWN because no job is ever enqueued |
| #331 | 3 | operator purge queues for hours behind a sweep with no feedback |
| #334 | 3 | `get_settings()` raising would downgrade a breaker trip to a per-game error (latent only) |

## Priority 4 — unexplained, unfiled

**SteamPrefill silently skips three apps.** 34440 (Civ IV), 34470 (Civ IV
Colonization), 317850 (COH2 Ardennes Assault) are all in
`selectedAppsToPrefill.json`, none of their depots has ever been downloaded, and
the cron log says only `Skipping app...` without naming them. Needs a verbose
targeted run. Low value — three old, small games — but genuinely unexplained.

## Priority 5 — housekeeping

- `/volume2` on the NAS has no disk alarm (26% used, 337 GB free). 20-minute copy
  of the LXC pattern; do it alongside the keys_zone work.
- Semgrep 1.175.0 → 1.177.0, Snyk 1.1307.0 → 1.1307.2. Neither below minimum.
- **Framework discovery review is 149 days overdue** (`init.sh --reconfigure`).
- Six stale dependabot branches on origin; `docs/lancache-nas-migration` unmerged.

---

## Operational rules learned the hard way — do not relearn these

1. **Recreate containers ONLY in the gap after a sweep ends.** 12 of 15 historical
   sweep failures were recreate kills. Check
   `jobs WHERE state IN ('running','queued')` is EMPTY first.
2. **Gaps between sweeps are ~30 min**, because sweeps run to the 6 h cap. Stage
   every slow step (build, `docker save | docker load`, manifest sync) while the
   OLD container serves, so the risky step is a ~30-second recreate.
3. **After recreating the agent, verify uid 0 and 256/256 buckets.** uid 1000 sees
   65 buckets and reports ~8% cached on everything. Has happened twice.
4. **Every `%` in a crontab line must be escaped `\%`.** An unescaped one truncates
   the command silently — it installs cleanly and never runs.
5. **Clean up after deploys.** DB backups are ~1.1 GB each. Three of them plus
   stale images and build cache took the LXC to 94% full.
6. **A manual test of a scheduled job proves nothing about the schedule.** Wait for
   a real slot.

## A caution about this document's author

Four conclusions I reported in this session dissolved on one further query:
"42% of the library is deadlocked" (#329, closed invalid), "the partial badge is
untested", a 15-game list that was mostly entries with no depots, and "add these
three to the prefill list" when they were already on it. Each time the missing
step was one more verification before reporting.

**Verify the numbers in this handoff before acting on them.** They were true on
2026-09-18.
