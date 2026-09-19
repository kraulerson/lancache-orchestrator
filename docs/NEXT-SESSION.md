# Next session — handoff

**Written:** 2026-09-18, end of the keys_zone-alarm implementation session.
**Replaces** the 2026-09-18 design-session handoff, which is consumed — its one
instruction ("make the failing tests pass, then deploy") is done. That version
survives at commit `c5046c5`.
**Not** the Phase 4 `HANDOFF.md` artifact (that is a release deliverable).

---

## Start here

The keys_zone alarm is **merged, deployed and running**. PR #347 and PR #349 both
landed on 2026-09-19. There is no half-built feature to resume and nothing is
blocked.

**One thing is still unverified.** Confirm the **24 h gauge fired on a real
slot**. The liveness thread is now thoroughly proven — after 18 h 15 m, monitors
219 and 220 had **74 heartbeats each, 0 down**, exactly on the 15-minute cadence
with no drift and no gaps. But the daily gauge on monitor **218 has run only
once, at startup**. A manual invocation proves nothing about a schedule; the LXC
disk monitor's `%` bug passed its manual test and then never ran once.

It is next due around **22:37 UTC** each day (the probe sleeps 86400 s from
process start, so the slot moves if the container is restarted).

```sh
ssh karl@192.168.1.30 'docker exec cache-catcher tail -3 /log/key_budget.csv'
# expect a SECOND row roughly 24h after 1789771023
```

```sh
ssh root@10.100.23.57 'python3 -c "
import sqlite3
c = sqlite3.connect(\"file:/opt/uptime-kuma/data/kuma.db?mode=ro\", uri=True)
for r in c.execute(\"SELECT monitor_id,count(*) FROM heartbeat WHERE monitor_id IN (218,219,220) GROUP BY monitor_id\"):
    print(r)"'
# 218 should be >1 by then; 219 and 220 climb every 15 min
```

---

## What was built

PR **#347**, merged 2026-09-19 (branch `design/keys-zone-alarm`, now deleted).
Build Loop closed **6/6**; the feature gate is clear at 1 of 2 until the next UAT
session. All four deployed files are byte-identical to `main` — `kuma.py` was
re-synced after the merge **without a container restart**, since the only
difference was a comment and a restart would have reset the 24 h probe timer.

| file | role |
|---|---|
| `tools/cache_catcher/kuma.py` | stdlib Kuma push, never raises |
| `tools/cache_catcher/key_budget.py` | pure decision logic — sampling, capacities, projection, verdict |
| `tools/cache_catcher/key_budget_probe.py` | I/O shell — dirs, `/proc`, CSV history, push |
| `tools/cache_catcher/fanotify_guard.py` | two daemon threads beside the fanotify loop |

Tests: **41** (9 + 32). Full suite **1985 passed, 3 deselected**. Phase 2.4
audit: **0 findings** — `docs/security-audits/keys-zone-alarm-security-audit.md`.

**Three Kuma monitors**, group 119, notification 3. DB backed up first to
`kuma.db.bak-prekeybudget-20260918-163525`; the live guard to
`/log/fanotify_guard.py.bak-prekeybudget-20260918-163652`.

| id | monitor | answers | silence means |
|---|---|---|---|
| 218 | `lancache:key-budget` | is the index filling | the probe thread died |
| 219 | `lancache:cache-guard` | is the guard process running | the guard died |
| 220 | `lancache:cache-eviction` | is cache loss happening now | the guard died |

**219 and 220 are two monitors because Karl changed the spec mid-execution.** As
designed, `alert()` pushed DOWN and the liveness thread pushed UP to the *same*
monitor on a 15-minute clock — so a real eviction would have turned it red and
then green again within 15 minutes while still running. `cache-eviction` now
latches DOWN for an hour after the last `eviction` or `mode000` alert. A
commanded `purge` still moves no monitor (#337). The change is recorded as an
amendment at the end of the design spec.

**The plan's alert `kind` strings were wrong** — it guessed `("evict", "attrib")`
where the call sites pass `"eviction"`, `"mode000"`, `"purge"`, `"external"`. Had
the guess shipped, no alert would ever have reached Kuma and the whole tripwire
promotion would have been silently inert. The plan's own instruction to verify
them against the code is what caught it.

## Live numbers, 2026-09-18 — re-verify before trusting

```
first run   KEY-BUDGET up: 35.8M objects, 48% of ram ceiling 75.2M, trend unknown
hand count  90 leaves, 0 read failures, 50366 files -> 36.7M  (agrees within 2.5%)
bytes/key   128.5 measured live  (design measured 146; re-measured every run)
history     /log/key_budget.csv -> 1789771023,35782656,4598390784,128.50892857142858
```

`trend unknown` is correct and will stay so until the history file accumulates.
**Do not quote a runway number as fact** — 30.7 M (Jul 31) → 34.4 M (Aug 12) →
35.6 M (Sep 18), and the Aug→Sep delta is only ~3× the sampling error, so any
runway figure sits inside the noise of its own measurement.

---

## Five things that will waste your time if you do not know them

### 1. Count the cache as root INSIDE the container

Some leaf dirs are `drwx------` (e.g. `/volume1/cache/cache/00`). Counting as
`karl` from the NAS host gets EPERM, and **a denied read looks identical to an
empty directory** — the first measurement during design came out **13× low** and
looked internally plausible. Confirmed again this session: 0 read failures as
root inside `lancache-monolithic`. `tests/tools/test_key_budget.py` encodes this.
Do not weaken it.

### 2. `stat` is ~180 files/sec on this NAS; `listdir` is instant

A 54 k-file size walk took over five minutes during design. The daily probe path
uses `listdir` only. Mean object size is deliberately a separate, rarer
measurement — now filed as **#348**.

### 3. The framework markers are single-use and consumed by each commit

`enforce-evaluate` blocks every commit. Recreate its marker as a **lone,
unchained** command from the repo root, with a reason containing **no shell
metacharacters** — a semicolon in the reason trips `config-guard`:

```sh
bash .claude/framework/hooks/mark-evaluated.sh "reason here"
```

`enforce-superpowers` is single-use the same way, so the *next* source edit after
a commit re-blocks until a Superpowers skill is invoked again. Related traps:
`config-guard` blocks Bash **reads** of framework files (use the Read tool);
`marker-guard` blocks any command naming a marker path; `enforce-evaluate` reads
a bare `-n` anywhere in the command as `--no-verify`, **and it pattern-matches
the word in prose too** — a heredoc that merely discusses committing is blocked,
so write such files with the Write tool rather than a shell heredoc.

`enforce-context7` flags **local sibling modules** as unresearched libraries when
they are imported flat (`import kuma`), and its stdlib whitelist is missing
`ctypes`, `smtplib` and `ssl`. The prescribed path clears it: call
`resolve-library-id` for each name, confirm there is no match, proceed.

### 4. Lint and SAST differ between local and CI — both bit this session

- **`tools/` is excluded from CI ruff but NOT from the local pre-commit hook**,
  which lints every staged `.py`. `UP031` rejects `%`-formatting, so new `tools/`
  files use f-strings; only `fanotify_guard.py` is exempt in `pyproject.toml`.
- **A bare `# nosemgrep: <id>` is ignored by CI's semgrep 1.36.0**, which matches
  only the fully-qualified `semgrep.`-prefixed form. List both spellings,
  comma-separated.
- **The pre-commit hook runs semgrep without `--error`**, so findings print but
  never block. A clean commit is not a clean scan. Run `semgrep scan --error`
  explicitly, including `p/security-audit`, which the hook omits but CI runs.

### 5. Run tests with the PATH prefix

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest
```

Without it `tests/test_licenses.py` false-fails on a missing `pip-licenses`
binary. Bare `python` is not on PATH. **The baseline is 1944**, not the 1867
quoted in older docs.

---

## Standing operational rules

The alarm's own deploy touched **only the `cache-catcher` container** — not
lancache, not the agent, not the control plane — so it needed no inter-sweep gap.
That was a design choice. For anything else:

1. **Recreate containers only when no job is running or queued.** 12 of 15
   historical sweep failures were recreate kills. Check `jobs WHERE state IN
   ('running','queued')` is empty first.
2. **Inter-sweep gaps are ~30 min.** Stage slow steps — build, `docker save |
   docker load`, manifest sync — while the old container still serves.
3. **After recreating the agent, verify uid 0 and 256/256 buckets.** uid 1000
   sees 65 buckets and reports ~8 % cached on everything. Has happened twice.
4. **Every `%` in a crontab line must be escaped `\%`.** An unescaped one
   truncates the command silently — it installs cleanly and never runs.
5. **Verify by waiting for a real scheduled run**, never a manual invocation.

---

## Still open, carried forward

### Open issues (10)

| # | sev | one-line |
|---|---|---|
| #312 | 3 | nine writer-guard evasions; 4, 5 and 6 are ordinary SQL a future author would write innocently |
| #315 | 3 | `VALIDATE_TIMEOUT_CEILING_SEC` is a bare constant while the rate it clamps is configurable |
| #316 | 4 | 19 games stuck at `failed`; operator passed the display — needs a decision, not a fix |
| #317 | 4 | `record_job_outcome()` still never fired in production; needs a deliberate UAT exercise |
| #326 | 4 | sweep monitor sees liveness, not coverage — a real pass is ~16.4 h |
| #330 | 3 | Epic prefill monitor DOWN because no job is ever enqueued |
| #331 | 3 | operator purge queues for hours behind a sweep with no feedback |
| #334 | 3 | `get_settings()` raising would downgrade a breaker trip to a per-game error (latent only) |
| #346 | — | `keys_zone=10000m` exceeds host RAM; needs a lancache restart |
| #348 | — | weekly mean-object-size measurement, deferred from the alarm |

**#346** still needs a decision on the real RAM budget, a corrected
`CACHE_INDEX_SIZE`, and a `mem_limit` on `lancache-monolithic`. All three need a
lancache restart, which is why they were kept out of the alarm. The alarm buys
time for it; it does not fix it.

### Not filed

- **SteamPrefill silently skips three apps** — 34440 (Civ IV), 34470 (Civ IV
  Colonization), 317850 (COH2 Ardennes Assault). All in
  `selectedAppsToPrefill.json`, none of their depots ever downloaded, cron log
  says only `Skipping app...` without naming them. Low value, genuinely
  unexplained.

### Housekeeping

- `/volume2` on the NAS has no disk alarm (26 % used). A 20-minute copy of the
  LXC disk pattern in `live-configuration.md` §1.3 — **mind the `%` escaping**.
- Semgrep 1.175.0 → 1.177.0, Snyk 1.1307.0 → 1.1307.3. Neither below minimum.
  **CI pins semgrep 1.36.0**, which is a separate and much older pin.
- **Framework discovery review is 151 days overdue.** The banner says
  `init.sh --reconfigure`; no `init.sh` exists at that implied path. The real one
  is `~/.claude-dev-framework/scripts/init.sh`, and a decoy `init.sh` without
  `--reconfigure` sits in the solo-orchestrator repo. **Verify before running.**
- Six stale dependabot branches on origin; `docs/lancache-nas-migration` unmerged.

---

## The monitor defect, now two of four fixed

A monitor that observes an event correctly and **cannot convey what it means**.
This is the theme of the whole project.

| issue | monitor | what it cannot distinguish |
|---|---|---|
| #326 | Kuma 180 sweep | "schedule running" vs "library covered" |
| #330 | Kuma 181 Epic prefill | "nothing to do" vs "scheduler dead" |
| #337 (fixed) | cache-catcher | commanded purge vs uncommanded eviction |
| **(fixed)** | cache-catcher liveness | "cache healthy" vs "guard is dead" — now Kuma 219 vs 220 |

#326 and #330 remain. If you pick one up, consider doing both: they are the same
bug wearing different hats, and the fix shape is now established — ask what *two*
facts a monitor could be conflating, and give each fact its own monitor.

---

## A caution about this document's author

Two conclusions dissolved on one further check during the **design** session, and
both were caught only by looking again: a cache count that was **13× low**
because permission-denied leaves were treated as empty, and a runway figure that
was wrong arithmetic, quoted twice before the spec's self-review caught it.

This session's near-miss was different in kind: **local verification passing is
not CI verification passing.** `ruff`, `pytest` and `semgrep` were all clean
locally and CI still failed SAST — because the semgrep pinned in CI is far older
than the local one and matches suppression comments differently. The lesson
generalises: when a check runs in two places, confirm which version runs where
before trusting either result.

The numbers above were true on 2026-09-18. **Re-verify before acting on them** —
the live systems are authoritative, this document is a snapshot.

**Authoritative sources, in preference order:** the live systems · `FEATURES.md`
· `CHANGELOG.md` · `PROJECT_BIBLE.md` · `.claude/phase-state.json` ·
`APPROVAL_LOG.md` · this file.
