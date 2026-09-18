# Next session — handoff

**Written:** 2026-09-18, end of the keys_zone-alarm design session.
**Replaces** the 2026-09-15 → 18 handoff, which is consumed — its Priority 1 and
2 are both done. That version survives at commit `a26a82d` if you want it.
**Not** the Phase 4 `HANDOFF.md` artifact (that is a release deliverable).

---

## Start here — one instruction

You are resuming a half-built feature. The design is approved, the plan is
written, the tests are committed and failing on purpose. **Your job is to make
them pass, then deploy.**

```
Read docs/superpowers/plans/2026-09-18-keys-zone-alarm.md and continue from
Task 1 Step 3. Tasks 1 and 2 already have their tests written and committed
(RED verified); what is missing is the implementation.
```

Branch: **`design/keys-zone-alarm`** (3 commits, not yet pushed, no PR).
Build Loop: **`keys-zone-alarm`, 2 of 6 steps** — `tests_written` and
`tests_verified_failing` are marked; `implemented`, `security_audit`,
`documentation_updated`, `feature_recorded` remain.

### Why the last session stopped where it did

Not a problem with the work. `enforce-plan-tracking` blocks `Write|Edit` on
source files (`.py` outside `tests/`) once `writing-plans` has been invoked in a
session, unless a plan task is marked in_progress via a `TaskUpdate` tool. That
session had no such tool. **The marker clears on a genuine session startup**, so
a fresh session — this one — can write source normally. Tests and `.md` files
were always exempt, which is why the RED phase landed and the implementation did
not.

**Do not invoke `writing-plans` in this session.** There is nothing left to plan,
and invoking it would re-arm the same gate.

---

## Exactly what is done

| | state |
|---|---|
| Design spec | `docs/superpowers/specs/2026-09-18-keys-zone-alarm-design.md` — **approved by Karl**, commit `b9485fd` |
| Implementation plan | `docs/superpowers/plans/2026-09-18-keys-zone-alarm.md` — 5 tasks, real code in every step, commit `0fd8961` |
| Tests | `tests/tools/test_kuma.py` (7) + `tests/tools/test_key_budget.py` (22), commit `160ad59` — **deliberately failing** with `ModuleNotFoundError` |
| Feature gate | **CLEAR** (`exit 0`). UAT 16 closed at 9/9 by Karl on 2026-09-18 |
| Issue #346 | filed — the keys_zone is provisioned beyond host RAM. Out of scope for the alarm |

**Remaining work, in order:** write `tools/cache_catcher/kuma.py` (Task 1 Step 3)
→ `tools/cache_catcher/key_budget.py` (Task 2 Step 3) → `key_budget_probe.py`
(Task 3) → modify `fanotify_guard.py` (Task 4) → docs, Kuma monitors, deploy,
live verification (Task 5).

The plan's Task 1 Step 5 and Task 2 Step 6 say to commit tests and implementation
together. **The tests are already committed**, so those commits carry the
implementation only. Everything else in the plan is accurate.

---

## What this feature is, in one paragraph

The 2026-07-31 mass deletion was nginx's cache-manager evicting live game data
from a ~94 %-full `keys_zone`. `df` cannot see that metric, nginx OSS publishes
no gauge for it, and the error log logged **nothing** through the entire nine-day
incident. So the alarm derives the metric: sample cache leaf directories, count
objects, compare against the nearest real ceiling, and alarm on a floor **or** a
projected fill date. Alongside it, the existing eviction tripwire in
`cache-catcher` — which today alerts by email only and **cannot prove it is
alive** — gains a Kuma push and a liveness heartbeat.

---

## Measured live 2026-09-18 — use as a baseline, re-verify before trusting

```
objects            35.6 M        zone used          44.5 %
mean object size   718 KiB       cache size         23.8 TiB
break-even size    708 KiB  ->   mean is 101 % of break-even
zone capacity      80.0 M keys   (10000m x 8000 keys/MB, nginx OSS)
keys to fill disk  78.9 M        -> disk and key ceilings are 1.4 % apart
lancache RSS       4.855 GiB     -> ~146 bytes/key
host RAM           15.4 GiB      agent mem_limit 8 GiB, has OOMed before
```

**The growth rate is not yet knowable.** 30.7 M (Jul 31) → 34.4 M (Aug 12) →
35.6 M (Sep 18). The Aug → Sep delta is only ~3× the ±1 % sampling error, so any
runway figure quoted today is within noise of its own measurement. This is why
the gauge reports `unknown` until its history file accumulates, and why the
static floor has to carry the load meanwhile. **Do not quote a runway number as
fact.**

---

## Four things that will waste your time if you do not know them

### 1. Count the cache as root INSIDE the container

Some leaf dirs are `drwx------` (e.g. `/volume1/cache/cache/00`). Counting as
`karl` from the NAS host gets EPERM, and **a denied read looks identical to an
empty directory**. The first measurement taken during design came out **13× low**
(2.6 M vs 35.6 M) and looked internally plausible. The tell was that 72 % of
sampled leaves reported "missing", which is impossible for uniform MD5.

```sh
ssh karl@192.168.1.30 'docker exec lancache-monolithic sh -c "..."'
```

`tests/tools/test_key_budget.py` encodes this as a test. Do not weaken it.

### 2. `stat` is ~180 files/sec on this NAS; `listdir` is instant

A 54 k-file size walk took **over five minutes** during design and timed out the
tool call. The daily probe path must use `listdir` only. Mean object size is a
separate, rarer measurement for exactly this reason.

### 3. The commit gate needs a fresh marker every single time

`enforce-evaluate` blocks every `git commit`. Its marker is **single-use** —
consumed by each successful commit. Recreate it as a **lone, unchained** command
from the repo root, with a reason containing no shell metacharacters:

```sh
bash .claude/framework/hooks/mark-evaluated.sh "reason here"
```

It fails if chained with `&&`, or if the script path is written as a
space-containing absolute path. `cd` to the repo root as its own separate call
first. Related traps: `config-guard` blocks Bash **reads** of framework files
(use the Read tool); `marker-guard` blocks any command naming a marker path; and
`enforce-evaluate` reads a bare `-n` anywhere in a commit command as
`--no-verify`.

### 4. Run tests with the PATH prefix

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest
```

Without it `tests/test_licenses.py` false-fails on a missing `pip-licenses`
binary. Bare `python` is not on PATH. Baseline before this work: **1867 passing,
3 deselected**; add 29 once the new modules exist.

---

## Deployment notes for Task 5

The deploy touches **only the `cache-catcher` container** — not lancache, not the
agent, not the orchestrator. It therefore does **not** need to wait for an
inter-sweep gap, and the usual container-recreate rule does not bind. That was a
deliberate design choice, not a happy accident.

The two standing operational rules still apply to anything else you touch:

1. **Recreate containers only when no job is running or queued.** 12 of 15
   historical sweep failures were recreate kills. Check `jobs WHERE state IN
   ('running','queued')` is empty first.
2. **Inter-sweep gaps are ~30 min.** Stage slow steps — build, `docker save |
   docker load`, manifest sync — while the old container still serves.

Two more, learned the hard way and still true:

3. **After recreating the agent, verify uid 0 and 256/256 buckets.** uid 1000
   sees 65 buckets and reports ~8 % cached on everything. Has happened twice.
4. **Every `%` in a crontab line must be escaped `\%`.** An unescaped one
   truncates the command silently — it installs cleanly and never runs.

**Verify by waiting for a real scheduled run**, never a manual invocation. The
LXC disk monitor's `%` bug passed its manual test and then never ran once.

---

## Still open, carried forward

### Open issues (8, unchanged)

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

Plus **#346** (new): `keys_zone=10000m` exceeds host RAM. Needs a decision on the
real RAM budget, a corrected `CACHE_INDEX_SIZE`, and a `mem_limit` on
`lancache-monolithic`. All three need a lancache restart, which is why they were
kept out of the alarm.

### Not filed

- **A follow-up issue for the weekly mean-object-size measurement.** The spec
  describes it as context-only and never alarmed on; the plan defers it
  explicitly rather than dropping it silently. File it when the alarm lands.
- **SteamPrefill silently skips three apps** — 34440 (Civ IV), 34470 (Civ IV
  Colonization), 317850 (COH2 Ardennes Assault). All in
  `selectedAppsToPrefill.json`, none of their depots ever downloaded, cron log
  says only `Skipping app...` without naming them. Low value, genuinely
  unexplained.

### Housekeeping

- `/volume2` on the NAS has no disk alarm (26 % used). A 20-minute copy of the
  LXC disk pattern in `live-configuration.md` §1.3 — **mind the `%` escaping**.
- Semgrep 1.175.0 → 1.177.0, Snyk 1.1307.0 → 1.1307.3. Neither below minimum.
- **Framework discovery review is 151 days overdue.** The banner says
  `init.sh --reconfigure`; no `init.sh` exists at that implied path. The real one
  is `~/.claude-dev-framework/scripts/init.sh`, and a decoy `init.sh` without
  `--reconfigure` sits in the solo-orchestrator repo. **Verify before running.**
- Six stale dependabot branches on origin; `docs/lancache-nas-migration` unmerged.

---

## Four monitors still share one defect

A monitor that observes an event correctly and **cannot convey what it means**.
This is the theme of the whole project, and the keys_zone work fixes the fourth
row by giving the tripwire a Kuma monitor and a liveness heartbeat.

| issue | monitor | what it cannot distinguish |
|---|---|---|
| #326 | Kuma 180 sweep | "schedule running" vs "library covered" |
| #330 | Kuma 181 Epic prefill | "nothing to do" vs "scheduler dead" |
| #337 (fixed) | cache-catcher | commanded purge vs uncommanded eviction |
| **in progress** | cache-catcher liveness | "cache healthy" vs "guard is dead" |

---

## A caution about this document's author

Two of my own conclusions dissolved on one further check **during this session**,
and both were caught before they reached the design only because I looked again:

1. The first cache count was **13× low** — I treated permission-denied leaf
   directories as empty ones. It looked plausible until the 72 %-missing figure
   turned out to be arithmetically impossible.
2. The first runway figure was **wrong arithmetic**, quoted twice in chat before
   the spec's self-review caught it. Corrected to ~4–14 months, then corrected
   again to "not yet knowable" once sampling error was accounted for.

The previous handoff's author gave the same warning and it was worth heeding. The
numbers above were true on 2026-09-18. **Re-verify before acting on them** — the
live state is authoritative, this document is a snapshot.

**Authoritative sources, in preference order:** the live systems · `FEATURES.md`
· `CHANGELOG.md` · `PROJECT_BIBLE.md` · `.claude/phase-state.json` ·
`APPROVAL_LOG.md` · this file.
