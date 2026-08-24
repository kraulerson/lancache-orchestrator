# UAT Session 14 — Live-System Verification (production)

**Date:** 2026-08-24 (UTC times throughout unless noted)
**Scope:** READ-ONLY verification against the live deployment. No state was mutated: no purge, no prefill,
no restart, no redeploy, no file written on any host, no write to the production DB. Every DB read used
`sqlite3.connect("file:/var/lib/orchestrator/orchestrator.db?mode=ro", uri=True)`.
**Commits since last UAT (2026-07-04):** 158.

---

## Topology as probed (corrections to the brief)

| Component | Documented in brief | Actually found |
|---|---|---|
| Control plane | LXC 1105 `10.100.23.105`, container `orchestrator` | Confirmed. API on **:8765** (host network), not :8080. Health route is **`/api/v1/health`**, not `/health`. |
| Data-plane agent | UGREEN NAS, curl `192.168.1.44:8780` from the LXC | Confirmed. `/v1/health` (not `/health`) returns 200. |
| UGREEN NAS | `10.100.23.30`, user `karl` in docker group | **Wrong IP.** `10.100.23.30` has no route from the Mac or the LXC. The NAS is reachable at **`192.168.1.30`** (`DXP4800-8473`). |
| lancache | `ssh karl@192.168.1.40` | **No longer an SSH host.** `192.168.1.40` pings but refuses :22. Post NAS-host-migration (#266) lancache runs as `lancache-monolithic` **on the NAS itself** (`192.168.1.30`). `192.168.1.40` remains the cache service VIP (heartbeat + `ORCH_LANCACHE_BASE_URL` still point there and work). |

```
$ ssh -o BatchMode=yes root@10.100.23.105 'hostname; docker ps'
lancache-orchestrator
CONTAINER ID   IMAGE          COMMAND                  CREATED      STATUS                PORTS     NAMES
cbce5613b865   234b9440cc37   "sh -c 'exec python …"   6 days ago   Up 6 days (healthy)             orchestrator

$ ssh -o BatchMode=yes karl@192.168.1.30 'docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"'
NAMES                 IMAGE                      STATUS
orchestrator-agent    orchestrator:dpa           Up 6 days (healthy)
lancache-dns          lancachenet/lancache-dns   Up 12 days
cache-catcher         cache-catcher:guard        Up 12 days
lancache-monolithic   lancachenet/monolithic     Up 2 weeks
```

---

## Summary

The system is **healthy and doing its job**. All four scheduled jobs fire on time and succeed; the cache is
at 1815/1817 candidates `cached` every 6 hours; **zero** games are currently in `partial`. The two
highest-risk regressions this session targeted — the shared-redist false-partial and the Steam
new-purchase blind spot — are both **verified fixed with runtime evidence**.

Six of the eight areas verify clean. Two do not:

- **Game 15035 (ARK ModKit) has not been validated since 2026-07-03 — 52 days.** ~244 GiB of cache has zero
  validation coverage, and every 6-hourly sweep burns a full disk-stat that is thrown away. Confirmed still
  happening, 27/27 sweeps in the current container lifetime. **SEV-2.**
- **External monitoring has been blind for at least 6.8 days.** An Uptime Kuma instance at
  `10.100.23.57:3001` polls `/api/v1/health` every 60 s and has received **854 consecutive 403s** because
  it is not in `ORCH_ALLOWED_SOURCE_IPS`. This bears directly on the monitoring change planned next.
  **SEV-2.**

One further **SEV-2** emerged that was not on the list: the weekly `fetch_manifests` job **records
`succeeded` while 669 of 1170 apps fail** (`manifest_fetch.done: fetched=84, skipped=1492, failed=669`).
Job state is therefore useless as a monitoring signal for that job.

**Important correction to the documented 15035 theory.** The recorded belief is that the agent *completes*
the disk-stat in ~14 min and returns 200 while the orchestrator has already given up. The evidence
**contradicts the second half**: the agent never returns 200 for that request at all. Detail in §7.

### Defect ranking

| Sev | Defect |
|---|---|
| SEV-2 | Game 15035 unvalidated for 52 days; 300 s timeout vs a ~7 min disk-stat; work discarded 4×/day (§7) |
| SEV-2 | Uptime Kuma (`10.100.23.57`) blocked by source-IP allowlist — 854 × HTTP 403, monitoring blind (§8) |
| SEV-2 | `fetch_manifests` reports `succeeded` with a 57 % per-app failure rate; failures invisible to job state (§4, §6) |
| SEV-3 | `platforms` table auth/sync columns stale and actively misleading (steam `expired`, epic expiry in the past, epic `last_sync_at` NULL) (§8) |
| SEV-3 | 1358 Steam `not_downloaded` + 16 Epic `failed` games are permanently outside the sweep candidate set (§2) |
| SEV-3 | A single transient chunk `ReadTimeout` permanently drops an Epic game from scheduled prefill (§5) |
| SEV-3 | LXC root filesystem 75 % full (4.8 G free); 12.9 GB reclaimable in Docker (§8) |
| SEV-3 | `GIT_SHA=unknown` in the deployed image — `/health` cannot identify the running commit (§8) |
| SEV-3 | `GET /api/v1/jobs?kind=bogus` returns `200` + empty list rather than `422` (§1) |

---

# Feature-by-feature verdicts

## 1. F18 cache purge — **VERIFIED WORKING**

**Kind registered in the live schema:**
```
$ docker exec -i orchestrator python -   # SELECT sql FROM sqlite_master WHERE name='jobs'
CREATE TABLE jobs (
    kind          TEXT NOT NULL CHECK (kind IN (
                      'prefill','validate','library_sync','auth_refresh',
                      'sweep','manifest_fetch','fetch_manifests','purge')),
...
```
Migration `0014_jobs_kind_purge` applied `2026-07-05 18:46:43`.

**Jobs-list endpoint surfaces it:**
```
$ curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/jobs?kind=purge&limit=10"
{"jobs":[{"id":36079,"kind":"purge","game_id":1778,"platform":"steam","state":"succeeded",
"progress":null,"source":"api","started_at":"2026-07-05 18:49:23","finished_at":"2026-07-05 18:49:34",
"error":null,"payload":null}],
"meta":{"total":1,"limit":10,"offset":0,"has_more":false,
"applied_filters":{"kind":{"eq":"purge"}},"applied_sort":[{"field":"id","direction":"desc"}]}}
HTTP 200
```

**Route registered (probed without invoking it — GET on a POST-only route):**
```
$ curl -s -X GET -H "Authorization: Bearer $T" -w "\nHTTP %{http_code}\n" \
    "http://127.0.0.1:8765/api/v1/games/1778/purge"
{"detail":"Method Not Allowed"}
HTTP 405
```
405 (not 404) confirms the purge route exists and rejects the wrong verb.

**Past purge jobs — 1 ever, and it completed:**
```
id    | kind  | game_id | platform | state     | source | started_at          | finished_at         | error
36079 | purge | 1778    | steam    | succeeded | api    | 2026-07-05 18:49:23 | 2026-07-05 18:49:34 | (null)
```
11 s. The full reversible round trip is provable from the game's own record — purged 2026-07-05 18:49,
re-prefilled 2026-07-06 01:23, cached ever since:
```
id   | platform | app_id  | title        | status     | last_validated_at   | last_prefilled_at
1778 | steam    | 1018130 | Castle Break | up_to_date | 2026-08-24 09:13:17 | 2026-07-06 01:23:55

validation_history (latest 5): 2026-08-24 09:13:16  60/60 cached
                               2026-08-24 03:13:22  60/60 cached
                               2026-08-23 21:12:34  60/60 cached  ...
```

**Caveat (not a defect):** the only runtime exercise of purge is the 2026-07-05 deploy validation. Nothing
has purged in the 50 days since, so this is *verified-once*, not *verified-recently*. No purge was run
during this session, per the read-only constraint.

**SEV-3 nit:** an unknown `kind` value is not rejected —
`GET /api/v1/jobs?kind=bogus` → `HTTP 200` with `{"jobs":[],"meta":{...,"applied_filters":{"kind":{"eq":"bogus"}}}}`.
An invalid enum should 422 rather than silently return nothing.

---

## 2. Steam new-purchase auto-cover — **VERIFIED WORKING**

**Claim under test:** the gated sweep now validates `unknown` owned games, so new purchases escape `unknown`.

**Live sweep selection (source, deployed image):**
```
src/orchestrator/jobs/handlers/sweep.py:32
    "WHERE status IN ('unknown','up_to_date','validation_failed') AND owned = 1 "
```

**Runtime proof the `unknown` path executes.** Exactly one game in the entire DB is `unknown` — Epic
`Steelrising`, inserted 2026-08-22 — and the sweep *did* pick it up on today's 09:00 run:
```
=== games w/ status unknown ===
id     | platform | app_id                           | title      | owned | status  | last_validated_at
360757 | epic     | 800ab4cd7b3b4b1cae4b5bff81cdfa1a | Steelrising| 1     | unknown | (null)

=== its validation_history row from the 09:00 sweep ===
game 360757 | started 2026-08-24 09:39:20 | chunks 0/0 | outcome=error | error='no_manifest'
```
`last_validated_at` stays NULL and the status stays `unknown` — which is exactly the designed behaviour
(`sweep.py:29`: "an uncovered 'unknown' game returns outcome='error' which validate leaves untouched"), so
it will be retried on every sweep until it becomes coverable. The gate is working, not stuck.

**Counts requested:**
```
=== games by platform+status ===
platform | status          | n
epic     | failed          | 16
epic     | unknown         | 1
epic     | up_to_date      | 656
steam    | failed          | 4
steam    | not_downloaded  | 1358
steam    | up_to_date      | 1160
```
**Steam games stuck in `unknown`: 0.** (Total `unknown` across both platforms: 1.)

**Newly-added Steam games are being covered.** The 15 highest Steam `game.id` values — i.e. the most
recently inserted — are all `up_to_date` and were all validated on today's 09:00 sweep:
```
348632 | steam | 699130  | World War Z                               | up_to_date | 2026-08-24 09:39:27
345518 | steam | 3947040 | Pegfinity                                 | up_to_date | 2026-08-24 09:39:17
345499 | steam | 3371770 | Funguys Swarm                             | up_to_date | 2026-08-24 09:39:21
329884 | steam | 3498390 | Astroloot                                 | up_to_date | 2026-08-24 09:39:16
329773 | steam | 2282790 | RIFTSTORM                                 | up_to_date | 2026-08-24 09:39:15
329768 | steam | 2231380 | Tom Clancy's Ghost Recon® Breakpoint      | up_to_date | 2026-08-24 09:39:26
329687 | steam | 1817030 | Swordcery                                 | up_to_date | 2026-08-24 09:39:13
329668 | steam | 1721110 | Abyssus                                   | up_to_date | 2026-08-24 09:39:07
329445 | steam | 992300  | 嗜血印 Bloody Spell                        | up_to_date | 2026-08-24 09:39:16
329295 | steam | 460930  | Tom Clancy's Ghost Recon® Wildlands       | up_to_date | 2026-08-24 09:39:25
...
```
(None carry a `last_prefilled_at` — they were filled by the host SteamPrefill cron, which is the intended
topology, and the orchestrator picked them up and validated them.)

### Related SEV-3: `not_downloaded` is still a one-way door

`not_downloaded` and `failed` are **not** in the sweep's `WHERE status IN (...)` list. 1358 Steam +
16 Epic games are therefore never re-examined. Latest-validation age confirms it — the errored cohort has
not been touched since 2026-07-31:
```
=== latest-validation age buckets ===
platform | bucket   | n
epic     | a: <2d   | 656
epic     | b: 2-7d  | 1
epic     | d: >30d  | 8
steam    | a: <2d   | 1160
steam    | c: 7-30d | 660
steam    | d: >30d  | 702
```
Most of the 1358 are not real games, which limits the blast radius:
```
=== steam not_downloaded: app_type breakdown ===
dlc                  684
(no steam_app_info)  616
game                  36
advertising           11
demo                   9
music                  2
```
But **36 are `app_type='game'`** and are permanently invisible — e.g. `1840 Source Filmmaker`,
`34440/34450/34460/34470 Civilization IV + expansions`, `115300 Call of Duty: MW3 (2011)`,
`201271 A Total War Saga: FALL OF THE SAMURAI`, `348250 Google Earth VR`, `366842 CoD: Black Ops III - Zombies`.
None of the 36 has any manifest on disk (see §4), which is *why* they are stuck — and 5 of them
(34276, 34440, 34450, 34460, 34470) appear verbatim in the `fetch_manifests` failure list, tying this
directly to the §4/§6 defect.

---

## 3. Steam shared-redist exclusion (depots 228981–228990) — **VERIFIED WORKING**

**Runtime proof the skip executes on every sweep** (agent structured log, today's 09:00 sweep):
```
$ ssh karl@192.168.1.30 'docker logs orchestrator-agent'   # excerpt
{"app_id": 956680,  "depots": [228986, 228990], "event": "steam_validate.shared_redist_skipped", ...}
{"app_id": 4704690, "depots": [228989, 228990], "event": "steam_validate.shared_redist_skipped", ...}
{"app_id": 228380,  "depots": [228988, 228990], "event": "steam_validate.shared_redist_skipped", ...}
{"app_id": 272860,  "depots": [228990],         "event": "steam_validate.shared_redist_skipped", ...}
{"app_id": 290300,  "depots": [228984, 228990], "event": "steam_validate.shared_redist_skipped", ...}
```
Aggregated over the whole agent log (6.8 days):
```
distinct apps w/ shared_redist_skipped: 543
distinct apps w/ depots_excluded:       216
depot histogram: {228981: 494, 228982: 520, 228983: 2806, 228984: 1635, 228985: 1506,
                  228986: 1871, 228987: 494, 228988: 3302, 228989: 3950, 228990: 10680}
events per day:  {'2026-08-18': 2166, '2026-08-19': 2172, '2026-08-20': 2172, '2026-08-21': 2172,
                  '2026-08-22': 2172, '2026-08-23': 2172, '2026-08-24': 1086}
```
All ten depots in the 228981–228990 range are exercised. 2172/day = 543 apps × 4 sweeps — every sweep,
every app.

**Outcome: the fix beat its own prediction.** The prediction was that ~50 games would return to `cached`.
Every game that was `partial` during the 2026-07-03→07-05 incident window is `cached` today — 77 of them:
```
=== games PARTIAL on 2026-07-03..05 -> current latest outcome ===
platform | cur_outcome | n
epic     | cached      | 1
steam    | cached      | 76
```

**Count by validation status (latest row per game):**
```
platform | outcome | n
epic     | cached  | 656
epic     | error   | 9        <- 'no_manifest' (dead Epic catalogue entries, HTTP 404 on manifest API)
steam    | cached  | 1160
steam    | error   | 1362     <- 'no_manifest_in_cache' (see §4)
```
**Zero `partial` on either platform.** The daily trend shows partials decaying to nothing and staying there:
```
=== outcome by day ===
2026-08-08 | cached 6238 | partial 45
2026-08-09 | cached 7120 | partial 32 | error 1
2026-08-10 | cached 7160 | partial 16
2026-08-11 | cached 7163 | partial 13
2026-08-12 | cached 7164 | partial 12
2026-08-13 | cached 7164 | partial 12
2026-08-14 | cached 7164 | partial 13
2026-08-15 | cached 7164 | partial 12
2026-08-16 | cached 7167 | partial  9
2026-08-17 | cached 7197 |
2026-08-18 | cached 7238 | error 1
2026-08-19 | cached 7240 |
2026-08-20 | cached 7250 |
2026-08-21 | cached 7252 |
2026-08-22 | cached 7260 | error 6
2026-08-23 | cached 7260 | error 4
2026-08-24 | cached 3630 | error 2   (partial day — 09:00 sweep only)
```
**No `partial` outcome has been recorded anywhere since 2026-08-16.** All 28 Steam games that showed
`partial` at any point in the last 16 days now read `cached`.

---

## 4. Steam manifest fetcher coverage (.shas vs .bin) — **VERIFIED WORKING (gap closed)**

Counted directly out of the agent's `/manifest-archive` volume, keyed on the `appid_depotid_manifestid`
filename convention:
```
$ ssh karl@192.168.1.30 'docker exec -i orchestrator-agent python -' < cov.py
files: .bin=4919 .shas=8763
distinct app_ids with .bin : 1163
distinct app_ids with .shas: 1148
both                        : 1145
.bin but NO .shas           : 18
.shas but NO .bin           : 3
union                       : 1166
```

| | documented gap | now |
|---|---|---|
| apps with `.shas` / apps with `.bin` | **330 / 1077 (30.6 %)** | **1148 / 1163 (98.7 %)** |

The 18 apps that have a `.bin` but no `.shas`:
`212630, 338250, 339550, 460930, 617480, 699130, 1646850, 1663850, 2230760, 2313550, 2690330, 2887680,
3210350, 3371770, 3451100, 3498390, 3947040, 4594150` — consistent with recent additions arriving after
the last weekly fetcher run (newest `.bin` is dated 2026-08-24, newest `.shas` 2026-08-18).

`.shas` write history shows the backfill and then steady-state top-ups:
```
shas files by mtime month: {'2026-06': 7034, '2026-07': 1357, '2026-08': 372}
bin  files by mtime month: {'2026-01': 202, '2026-02': 354, '2026-04': 676, '2026-05': 514,
                            '2026-06': 783, '2026-07': 599, '2026-08': 1791}
```

**Note on the 1362 `no_manifest_in_cache` errors.** These are *not* a fetcher regression. There are 2522
Steam rows but only ~1163 apps with manifests, because the Steam library rows are licences, not installable
games — 684 `dlc`, 616 with no `steam_app_info`, 11 `advertising`, 9 `demo`, 2 `music`. Only 36 are
`app_type='game'`, and those trace to the fetcher failures below.

### SEV-2: `fetch_manifests` hides a 57 % per-app failure rate

```
$ ssh karl@192.168.1.30 'docker logs orchestrator-agent | grep manifest_fetch.done'
{"apps": 1170, "fetched": 84, "skipped": 1492, "failed": 669,
 "event": "manifest_fetch.done", "level": "info", "timestamp": "2026-08-18T08:57:47.779613Z"}
```
669 failures, all the same shape, all in one weekly run:
```
{"app_id": 8870,  "reason": "RuntimeError: DepotDownloader produced no manifest for app 8870 (rc=1)",
 "event": "manifest_fetch.app_failed", "level": "warning", "timestamp": "2026-08-18T05:13:46.596436Z"}
{"app_id": 34276, "reason": "RuntimeError: DepotDownloader produced no manifest for app 34276 (rc=1)", ...}
{"app_id": 34440, "reason": "RuntimeError: DepotDownloader produced no manifest for app 34440 (rc=1)", ...}
first 2026-08-18T05:13:46Z  last 2026-08-18T08:57:47Z
```
Despite that, the orchestrator recorded the job clean:
```
id    | platform | state     | started_at          | finished_at         | dur_s | error
45036 |          | succeeded | 2026-08-18 05:00:00 | 2026-08-18 08:57:50 | 14269 | (null)
```
The failures live only in the agent's warning stream; job state says `succeeded`. Any monitor built on job
state will never see this. Many of these app IDs are genuinely uncacheable (delisted, region-locked,
tool-only), so the count is not necessarily 669 real problems — but the job cannot currently tell the
difference, and neither can an operator.

---

## 5. Epic prefill wall-clock cron (:45 past 03/09/15/21 UTC) — **VERIFIED WORKING**

Every one of the 27 expected slots in the container's 6.8-day lifetime fired, on the second, with **zero
misses and zero drift**:
```
$ docker logs orchestrator | grep scheduler.scheduled_prefill.enqueued
2026-08-17T21:45:00.001949Z {'count': 0}
2026-08-18T03:45:00.002360Z {'count': 0}
2026-08-18T09:45:00.004891Z {'count': 0}
2026-08-18T15:45:00.001725Z {'count': 0}
2026-08-18T21:45:00.009867Z {'count': 1}
2026-08-19T03:45:00.001794Z {'count': 0}
2026-08-19T09:45:00.002417Z {'count': 0}
2026-08-19T15:45:00.000961Z {'count': 0}
2026-08-19T21:45:00.001131Z {'count': 0}
2026-08-20T03:45:00.001202Z {'count': 0}
2026-08-20T09:45:00.001748Z {'count': 0}
2026-08-20T15:45:00.000908Z {'count': 0}
2026-08-20T21:45:00.001756Z {'count': 0}
2026-08-21T03:45:00.001854Z {'count': 0}
2026-08-21T09:45:00.002116Z {'count': 0}
2026-08-21T15:45:00.001212Z {'count': 0}
2026-08-21T21:45:00.002036Z {'count': 0}
2026-08-22T03:45:00.001626Z {'count': 2}
2026-08-22T09:45:00.000978Z {'count': 0}
2026-08-22T15:45:00.000971Z {'count': 0}
2026-08-22T21:45:00.001790Z {'count': 0}
2026-08-23T03:45:00.000974Z {'count': 0}
2026-08-23T09:45:00.001790Z {'count': 0}
2026-08-23T15:45:00.002264Z {'count': 0}
2026-08-23T21:45:00.009288Z {'count': 0}
2026-08-24T03:45:00.000951Z {'count': 0}
2026-08-24T09:45:00.001423Z {'count': 0}
```
`count: 0` on a quiet slot is correct — the driver enqueues only games that need filling. `ORCH_SCHEDULED_PREFILL_ENABLED=true`
on the LXC and `false` on the agent, matching the documented split.

**The 3 non-zero slots produced real jobs**, and 2 of the 3 succeeded:
```
id    | game_id | platform | state     | source    | started_at          | finished_at         | dur_s | error
45087 | 360759  | epic     | succeeded | scheduler | 2026-08-22 03:45:04 | 2026-08-22 03:46:38 | 93    |
45086 | 360756  | epic     | succeeded | scheduler | 2026-08-22 03:45:00 | 2026-08-22 03:45:04 | 4     |
45046 | 337151  | epic     | failed    | scheduler | 2026-08-18 21:45:00 | 2026-08-18 21:45:51 | 51    | RuntimeError: epic prefill failed: 1/3115 chunks
```
Both successes then validated as `cached` on today's sweep (`360756` and `360759` are `up_to_date`,
last validated `2026-08-24 09:39:20` / `09:39:23`).

### SEV-3: one transient chunk error permanently drops a game from scheduled prefill

Job 45046 failed on **1 chunk out of 3115**, from a single `ReadTimeout`:
```
{"job_id": 45046, "game_id": 337151, "failed": 1, "total": 3115,
 "failure_reasons": {"ReadTimeout": 1}, "event": "prefill.epic.chunks_failed", "level": "warning",
 "timestamp": "2026-08-18T21:45:51.754962Z"}
{"kind": "prefill", "job_id": 45046, "kind_error": "RuntimeError", "elapsed_ms": 51612,
 "event": "jobs.handler.failed", "level": "warning", "timestamp": "2026-08-18T21:45:51.763919Z"}
```
`Caravan SandWitch` (game 337151) went to `status='failed'` and has not been retried in the 24 slots since
(all `count: 0`). `failed` is outside the sweep's candidate set too, so nothing will ever revisit it. It is
also now carrying a Game_shelf exclusion:
```
excl | epic | e1f0b4ad4b16426e991276aceb81bbe2 | exclude / gameshelf: covered on higher-priority launcher
```
A 1-in-3115 network blip should be retried, not treated as terminal.

**Also noted (informational):** both successful prefills logged
`prefill.epic.low_hit_ratio` with `hit_ratio: 0.0`. For a first-ever prefill of a brand-new game a 0 % cache
hit ratio is the expected and desired outcome (everything came from WAN, which is the point). The warning
fires on a condition that is normal for cold games and will be noise for any monitor built on it.

---

## 6. Scheduled jobs — last run, outcome, duration

All four are running. Times UTC. "Now" at time of collection: `2026-08-24 13:49:54`.

| Job | Cadence (observed) | Last run | State | Duration | Verdict |
|---|---|---|---|---|---|
| `library_sync` (steam) | every 6 h at :44 | 2026-08-24 12:44:32 → 12:45:21 | succeeded | **49 s** | Working |
| `library_sync` (epic) | every 6 h at :45 | 2026-08-24 12:45:21 → 12:45:25 | succeeded | **3 s** | Working |
| `sweep` (validation) | every 6 h at :00 | 2026-08-24 09:00:00 → 09:39:31 | succeeded | **2371 s (39.5 min)** | Working, 1 game always errors (§7) |
| scheduled Epic `prefill` | every 6 h at :45 | 2026-08-22 03:45:04 → 03:46:38 | succeeded | **93 s** | Working (slots fire 4×/day; job only when work exists) |
| `fetch_manifests` | **weekly**, Tue 05:00 | 2026-08-18 05:00:00 → 08:57:50 | succeeded | **14269 s (3 h 58 m)** | Fires reliably — **but see SEV-2 in §4** |

Raw:
```
=== last 10 sweep jobs ===
45116 | succeeded | 2026-08-24 09:00:00 | 2026-08-24 09:39:31 | 2371
45113 | succeeded | 2026-08-24 03:00:00 | 2026-08-24 03:39:37 | 2376
45110 | succeeded | 2026-08-23 21:00:00 | 2026-08-23 21:38:44 | 2323
45107 | succeeded | 2026-08-23 15:00:00 | 2026-08-23 15:38:51 | 2330
45104 | succeeded | 2026-08-23 09:00:00 | 2026-08-23 09:38:59 | 2338
45101 | succeeded | 2026-08-23 03:00:00 | 2026-08-23 03:39:29 | 2368
45098 | succeeded | 2026-08-22 21:00:00 | 2026-08-22 21:38:53 | 2333
45095 | succeeded | 2026-08-22 15:00:00 | 2026-08-22 15:39:23 | 2363
45092 | succeeded | 2026-08-22 09:00:00 | 2026-08-22 09:39:31 | 2371
45085 | succeeded | 2026-08-22 03:00:00 | 2026-08-22 03:39:17 | 2357

=== last 10 fetch_manifests ===
45036 | succeeded | 2026-08-18 05:00:00 | 2026-08-18 08:57:50 | 14269
44953 | succeeded | 2026-08-11 05:00:00 | 2026-08-11 08:46:57 | 13616
43180 | succeeded | 2026-08-04 09:00:52 | 2026-08-04 13:06:43 | 14751
36588 | succeeded | 2026-07-28 05:00:00 | 2026-07-28 08:50:54 | 13853
36473 | succeeded | 2026-07-21 05:00:00 | 2026-07-21 08:48:30 | 13709
36384 | succeeded | 2026-07-14 05:00:00 | 2026-07-14 08:46:45 | 13604
36166 | succeeded | 2026-07-07 05:00:00 | 2026-07-07 08:44:00 | 13440
36054 | failed    | 2026-07-04 03:32:45 | 2026-07-04 04:05:50 | 1985   AgentError: agent unreachable: ConnectError
```
The 2026-08-04 run at 09:00 rather than 05:00 is the only cadence anomaly; every run since 2026-07-07 has
succeeded. Next expected: 2026-08-25 05:00.

**Sweep is highly stable and its own summary is a good monitoring signal** (unlike `fetch_manifests`):
```
{"job_id": 45116, "candidates": 1817, "full": false, "event": "sweep.started",  "...": "2026-08-24T09:00:00.879703Z"}
{"job_id": 45116, "total": 1817, "cached": 1815, "validation_failed": 0, "validation_error": 1,
 "evicted": 0, "recovered": 0, "errors": 1, "event": "sweep.completed",         "...": "2026-08-24T09:39:31.017897Z"}
```
Identical `1817 / 1815 / 0 / 1 / 0 / 0 / 1` on the last four sweeps. The two non-cached are Steelrising
(`validation_error`, no manifest yet) and 15035 (`errors`, timeout).

**No stuck jobs:**
```
=== jobs stuck running/queued ===
(no rows)
```

**Scheduler config at boot:**
```
{"library_sync_interval_sec": 21600, "jobs": 6, "event": "scheduler.started", "...": "2026-08-17T18:44:31.826219Z"}
{"enabled": true, "running": true, "library_sync_interval_sec": 21600, "event": "api.boot.scheduler_started", ...}
```

---

## 7. Game 15035 "ARK ModKit (UE4)" — **VERIFIED BROKEN — SEV-2**
### (with a correction to the documented root-cause theory)

**Current state — unvalidated for 52 days:**
```
id    | platform | app_id    | title            | size_bytes   | status     | last_validated_at   | last_prefilled_at
15035 | epic     | ARKDevKit | ARK ModKit (UE4) | 261877742712 | up_to_date | 2026-07-03 13:50:42 | 2026-07-02 11:47:19

=== 15035 validation_history — ONE row, ever ===
id    | started_at          | finished_at         | dur_s | method    | chunks_total | chunks_cached | outcome
23610 | 2026-07-03 13:46:32 | 2026-07-03 13:50:42 | 249   | disk_stat | 359671       | 359671        | cached
```
261,877,742,712 B = **243.9 GiB**, 359,671 chunks. Confirms the brief's figures.

**It fails on every single sweep.** 27 of 27 `sweep.game_error` events in the container's lifetime, and
every one of them is this game and nothing else:
```
$ docker logs orchestrator | grep -c "sweep.game_error"
27
$ docker logs orchestrator | grep "sweep.game_error" | grep -oE '"game_id": [0-9]+' | sort | uniq -c
     27 "game_id": 15035
```
```
{"job_id": 45116, "game_id": 15035, "error": "AgentError", "reason": "agent unreachable: ReadTimeout",
 "event": "sweep.game_error", "job_kind": "sweep", "level": "warning", "timestamp": "2026-08-24T09:30:00.441293Z"}
{"job_id": 45113, ... "timestamp": "2026-08-24T03:30:07.772726Z"}
{"job_id": 45110, ... "timestamp": "2026-08-23T21:29:16.225481Z"}
{"job_id": 45107, ... "timestamp": "2026-08-23T15:29:22.377399Z"}
{"job_id": 45104, ... "timestamp": "2026-08-23T09:29:29.555784Z"}
   ... unbroken back to 2026-08-17T21:30:16.661349Z (container start)
```

**The timeout, in the deployed source:**
```
src/orchestrator/clients/agent_client.py:228   (inside epic_validate)
            timeout=httpx.Timeout(300.0, connect=10.0),
```
Matches the recorded citation `agent_client.py:228`. The same 300 s value is at lines 211
(`steam_validate`), 241 (`steam_purge`) and 258.

**The disk-stat genuinely needs more than 300 s.** Measured from the 119 Epic validations over 20 k chunks
in today's 09:00 sweep:
```
epic validations >20k chunks in 08-24 09:00 sweep: n=119 chunks=6030810 wall=7255s -> 831 chunks/s
extrapolated 359,671 chunks (game 15035) => 433 s  (7.2 min)
fastest 1499 chunks/s ; slowest 411 chunks/s
=> 15035 range: 4.0 min (fast) .. 14.6 min (slow)
```
Corroborated by the nearest-size peer that *does* complete — `MechWarrior 5 Editor`, 203,631 chunks in 271 s:
```
id    | title                | started_at          | finished_at         | dur_s  | chunks_total | outcome
14942 | MechWarrior 5 Editor | 2026-08-24 09:22:04 | 2026-08-24 09:26:35 | 271    | 203631       | cached
15206 | Destiny 2            | 2026-08-24 09:30:11 | 2026-08-24 09:33:44 | 213    | 172062       | cached
14962 | Fortnite             | 2026-08-24 09:23:01 | 2026-08-24 09:25:57 | 176    | 150282       | cached
```
271 s at 203 k chunks is already 90 % of the budget. 15035 is 1.77× larger. **So the 300 s ceiling is
between 1.4× and 3× too small**, and MechWarrior 5 Editor is one bad NFS day away from joining it.

### Correction: the agent does **not** return 200 — the work is lost, not merely discarded

The recorded theory is "the agent completes the disk-stat successfully (~14 min) and returns 200" while the
orchestrator has already timed out. The first half is plausible; **the second half is not supported by the
evidence.** No 200 is ever emitted for this request.

Exact accounting for today's 09:00 sweep:
```
DB, epic rows written in the sweep window:
  total rows                       656   (min started 09:21:02, max finished 09:39:23)
  rows with chunks_total > 0       655   (i.e. actually served by the agent; the 656th is
                                          Steelrising's chunkless 'no_manifest' error)

Agent access log, POST /v1/epic/validate 200 OK in the same window:
  09:21 43 | 09:22 27 | 09:23 33 | 09:24 43 | 09:25 22 | 09:26 19 | 09:27 33
  09:28 31 | 09:29 53 | 09:30 30 | 09:31 32 | 09:32 43 | 09:33 54 | 09:34 38
  09:35 48 | 09:36 37 | 09:37 49 | 09:38 18 | 09:39  2
  TOTAL = 655
```
**655 agent 200s ⇄ 655 agent-backed DB rows.** The boundaries line up to the millisecond — first agent 200
at `09:21:02.207`, DB min `started_at` `09:21:02`; last agent 200 at `09:39:23.296`, DB max `finished_at`
`09:39:23` — which proves the access-log lines are exactly those calls, with no spare. **There is no
unaccounted 200 that could belong to 15035.**

And nothing arrives late either:
```
$ docker logs -t orchestrator-agent | grep "epic/validate" \
    | awk '$1 > "2026-08-24T09:39:24" && $1 < "2026-08-24T14:00"'
(end)     # zero lines — nothing between the sweep and the time of collection
```
Same shape on the two preceding sweeps (first/last epic 200 per sweep, nothing outside):
```
2026-08-23T21:20:17.434Z ... 2026-08-23T21:38:37.098Z
2026-08-24T03:21:09.984Z ... 2026-08-24T03:39:30.021Z
2026-08-24T09:21:02.207Z ... 2026-08-24T09:39:23.296Z
```

**So the actual behaviour is:** the orchestrator opens `POST /v1/epic/validate` for 15035 at ~09:25:00,
the agent begins a ~7-minute disk-stat, the orchestrator's 300 s read timeout fires at 09:30:00.44 and
drops the connection, and the agent's handler is torn down on client disconnect — it never completes and
never logs a 200. The NAS still pays for the ~5 minutes of I/O it did before being cut off. Net effect is
the same as the recorded theory (result thrown away, 4×/day) but the mechanism differs, and it matters for
the fix: a longer client timeout alone is sufficient, and there is no orphaned agent-side work to reap.

**Impact.** 243.9 GiB — the largest Epic title in the library — has had **no validation coverage since
2026-07-03**. The game still reads `up_to_date` in the UI and in the API, so eviction of this title would be
completely silent. That is the orchestrator's core value proposition failing, on its biggest object.

**Recommended fix (NOT applied — read-only session):** scale the `epic_validate` / `steam_validate` timeout
with `chunks_total` (e.g. `max(300, chunks_total / 300)` → ~1200 s for 15035), or simply raise the constant
at `agent_client.py:228` to ≥ 1800 s. A flat raise is enough today; scaling is what stops the next
300 k-chunk title from re-opening this.

---

## 8. Overall health — **HEALTHY, with two SEV-2 monitoring/config defects**

### /health

```
$ curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/health
{"status":"ok","version":"0.1.0","uptime_sec":587102,"scheduler_running":true,
 "lancache_reachable":true,"cache_volume_mounted":true,"validator_healthy":true,
 "steam_auth_ok":true,"agent_reachable":true,"git_sha":"unknown"}
HTTP 200
```
Every flag green. Uptime 587,102 s = 6.79 days.

```
$ curl -s http://192.168.1.44:8780/v1/health          # agent, from the LXC
{"ok":true,"validator_healthy":true}
HTTP 200
```

### Container health

```
LXC 1105:  orchestrator          Up 6 days (healthy)
NAS:       orchestrator-agent    status=running  restarts=0  health=healthy   started 2026-08-18T02:26:42Z
           lancache-monolithic   status=running  restarts=0  health=none      started 2026-08-06T02:33:34Z
           lancache-dns          status=running  restarts=0  health=none      started 2026-08-12T14:00:51Z
           cache-catcher         status=running  restarts=0  health=none      started 2026-08-12T14:14:21Z
```
Zero restarts anywhere.

### Disk

```
LXC 1105:
/dev/mapper/VM-vm--1105--disk--0   20G   14G  4.8G  75% /
  /var/lib 13G  ·  orchestrator.db 1.1G  ·  docker json.log 14 MB
  $ docker system df
  Images         24  ACTIVE 1   4.686GB   RECLAIMABLE 4.313GB (92%)
  Local Volumes   1  ACTIVE 1   1.114GB   RECLAIMABLE 0B
  Build Cache   274  ACTIVE 0   8.653GB   RECLAIMABLE 3.98GB

NAS:
/dev/bcache0    55T   26T   29T  48% /volume1      (cache volume — ample)
/dev/mapper/...volume1  454G  107G  347G  24% /volume2
overlay          19G  1.4G   17G   9% /
```
**SEV-3:** the LXC root is at 75 % with 4.8 G free, and 12.9 GB of that is reclaimable Docker images +
build cache. Not urgent, but it will bite during the next image build. (No cleanup performed — read-only.)

### Cache permissions — the mode-000 failure mode is NOT present

```
$ ls -ld /volume1/cache /volume1/cache/cache
drwxr-xr-x   8 root     root 4096 Aug  9 13:55 /volume1/cache
drwxr-xr-x 258 www-data root 4096 Aug 12 08:14 /volume1/cache/cache
```

### Eviction watch — clean

`cache-catcher` shows writes only; the single `DEL` in the recent window is a GOG helper touching a
non-cache path and is explicitly classified as benign:
```
2026-08-24T10:00:25+0000 DEL DELETE pid=1290835 comm=python3 ... name=fallout_london cmd=[python3 .../gogrepoc.py download ...]
2026-08-24T10:00:25+0000 DEL-ignored (not a cache object) name=fallout_london comm=python3
2026-08-24T12:00:14+0000 WRITES 346 in 21603s (nginx cache writes, MOVED_FROM)
2026-08-24T13:14:04+0000 WRITES 445 in 4430s (nginx cache writes, MOVED_FROM)
```
No nginx eviction bursts. Consistent with the sweep's `"evicted": 0` on every run.

### lancache errors — none

```
$ docker logs --since 72h lancache-monolithic 2>&1 | grep -icE "error|crit|alert|emerg"
0
```

---

# Recurring errors with counts

### Orchestrator (`docker logs orchestrator`, 2026-08-17 18:44 → 2026-08-24 14:01, 6.8 days)

```
=== level counts ===
  41774 "level": "info"
    878 "level": "warning"
      1 "level": "error"
```

| Count | Event | Assessment |
|---|---|---|
| **845** | `api.source.rejected` | **SEV-2** — see below |
| **27** | `sweep.game_error` | **SEV-2** — all 27 are game 15035, §7 |
| 2 | `prefill.epic.low_hit_ratio` | Informational — 0 % hit ratio is correct for a first prefill; noisy warning level |
| 1 | `prefill.epic.chunks_failed` | SEV-3 — 1/3115 chunks, killed job 45046 permanently (§5) |
| 1 | `lancache.probe.network_error` | One-off `ReadTimeout` on the heartbeat, 2026-08-19T00:00:17Z; self-recovered |
| 1 | `jobs.handler.failed` | Same event as `chunks_failed` above (job 45046) |
| 1 | `api.auth.rejected` | My own unauthenticated probe during this session — not a production event |
| **1 (only ERROR)** | `api.manual_downloads.agent_error` | `{"launcher": "Amazon Games", "reason": "agent unreachable: ReadTimeout", ..., "timestamp": "2026-08-21T17:14:52.640758Z"}` — single occurrence, same 300 s-timeout family as §7 |

### SEV-2 — `api.source.rejected` × 845: external monitoring has been blind for ≥ 6.8 days

```
$ docker logs orchestrator | grep "api.source.rejected" | grep -oE '"client_host": "[^"]*"' | sort | uniq -c
    854 "client_host": "10.100.23.57"
      1 "client_host": "192.168.1.192"
$ docker logs orchestrator | grep "api.source.rejected" | grep -oE '"path": "[^"]*"' | sort | uniq -c
    855 "path": "/api/v1/health"
```
```
{"reason": "source_not_allowed", "path": "/api/v1/health", "client_host": "10.100.23.57",
 "event": "api.source.rejected", "level": "warning", "timestamp": "2026-08-24T13:50:05.181454Z"}
INFO:     10.100.23.57:37188 - "GET /api/v1/health HTTP/1.1" 403 Forbidden
```
Every 60 seconds, without a single success. Identifying the caller:
```
$ for p in 22 80 443 3001 8080 9090 3000; do (echo >/dev/tcp/10.100.23.57/$p) 2>/dev/null && echo "open: $p"; done
open: 22
open: 3001
$ curl -s http://10.100.23.57:3001/
Found. Redirecting to /dashboard
```
Port 3001 + that redirect is Uptime Kuma. The allowlist on the LXC is:
```
ORCH_ALLOWED_SOURCE_IPS=10.100.23.102        # Game_shelf only
```
The LAN-bind allowlist is doing exactly what it was built to do — it is the *configuration* that is
incomplete. Someone stood up health monitoring and it has never once returned a green check. This is
directly load-bearing for the monitoring change planned next: **add `10.100.23.57` to
`ORCH_ALLOWED_SOURCE_IPS` before building anything on top of `/health`.** (Not changed — read-only.)
Note the agent's own allowlist already carries two entries (`10.100.23.102,10.100.23.105`), so the pattern
is established; the LXC's just was not updated.

### Agent (`docker logs orchestrator-agent`, 2026-08-18 02:26 → now, 6.4 days)

```
=== level counts ===
  37947 "level": "info"
   1339 "level": "warning"
       0 error / critical
```

| Count | Event | Assessment |
|---|---|---|
| **669** | `manifest_fetch.app_failed` | **SEV-2** — all from the single 2026-08-18 weekly run; job still reported `succeeded` (§4) |
| **669** | `manifest_fetch.dd_nonzero` | Same 669 apps, the DepotDownloader-level pair of the above (`returncode: 1`, empty stderr) |
| 1 | `api.auth.rejected` | My own unauthenticated probe this session |

Informational (info level, high volume — healthy, not errors):
```
  18171  validator.self_test.ok
  14112  steam_validate.shared_redist_skipped      <- §3, the fix working
   5616  steam_validate.depots_excluded
     26  agent.prune_selection.done
     22  manifest_archive.synced
```

### SEV-3 — `platforms` table is stale and actively misleading

```
name  | auth_status | auth_method | auth_expires_at          | last_sync_at        | last_error
steam | expired     | steam_cm    | (null)                   | 2026-06-18 23:13:56 | NotAuthenticated: no logged-in steam session
epic  | ok          | epic_oauth  | 2026-07-03T03:16:46.973Z | (null)              | (null)
```
Three separate lies, against a live system where everything works:
- steam reads `expired` with a 2026-06-18 error, while `/health` reports `"steam_auth_ok": true` and
  `library_sync` (steam) has succeeded every 6 h including 2026-08-24 12:44:32 (49 s).
- epic reads `auth_status: ok` with `auth_expires_at` **52 days in the past**.
- epic `last_sync_at` is NULL despite 6-hourly successful syncs.

This is the known orphaned-column issue (PR #208 moved auth reporting to live `/health`), but the columns
are still there and still wrong. Anything that reads this table — a future monitor especially — gets a
false answer in both directions. Either backfill the writers or drop the columns.

### SEV-3 — `GIT_SHA=unknown`

```
$ docker inspect orchestrator --format '{{json .Config.Env}}'
... "GIT_SHA=unknown" ...
$ curl .../api/v1/health   ->   "git_sha":"unknown"
```
The image (`orchestrator:dpa`, built 2026-08-17T18:44:29Z) does not stamp its commit, so there is no way to
confirm from the runtime which of the 158 commits since the last UAT are actually deployed. Everything
verified in this report was verified behaviourally rather than by version. Worth fixing before the next
UAT.

---

# Could not determine

1. **Whether the agent's 15035 handler is cancelled or silently abandoned.** Established with certainty that
   no `200 OK` is ever emitted for it (§7). Distinguishing "uvicorn cancelled the task on client
   disconnect" from "the handler ran to completion and failed to write to a dead socket" would require
   either instrumenting the agent or issuing a live `/v1/epic/validate` — both out of scope here. It does
   not change the fix.
2. **The exact wall-clock duration of a 15035 disk-stat today.** The 433 s central estimate (range
   240–876 s) is extrapolated from 119 same-sweep Epic validations, not measured. A direct measurement
   needs an RPC to production.
3. **Whether the 669 `fetch_manifests` failures are legitimate.** Many app IDs look like delisted or
   tool-only entries where DepotDownloader correctly produces nothing, but the run does not distinguish
   "no manifest exists" from "fetch failed". Determining the real number requires per-app triage.
4. **Purge behaviour under current conditions.** Verified structurally (schema, route, filter) and
   historically (the 2026-07-05 job and its cached aftermath), but not exercised — running one was
   explicitly out of scope.
5. **The 616 Steam `not_downloaded` rows with no `steam_app_info`.** Whether these are DLC, delisted, or
   real games needs a Steam API lookup that was not performed.
6. **lancache request-level hit/miss statistics.** `docker logs lancache-monolithic` yielded no
   access-log lines in the last 72 h for the status-code histogram attempted, and `cache_observations` has
   **0 rows** in the DB, so no cache hit-ratio evidence was available from either source.
7. **Total cache directory size.** `du -sh /volume1/cache/cache` exceeded the command timeout on a 26 TB
   tree and was abandoned; `df` figures are used instead.

---

## Commands used (reproduction)

```bash
# control plane
ssh -o BatchMode=yes root@10.100.23.105 'hostname; docker ps'
ssh root@10.100.23.105 'T=$(docker inspect orchestrator --format "{{range .Config.Env}}{{println .}}{{end}}" \
  | grep ^ORCH_TOKEN= | cut -d= -f2); curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/health'
ssh root@10.100.23.105 'docker logs orchestrator 2>&1 | grep "sweep.game_error"'

# read-only DB access (script piped over stdin; nothing written to any host)
ssh root@10.100.23.105 'docker exec -i orchestrator python -' < query.py
#   query.py opens: sqlite3.connect("file:/var/lib/orchestrator/orchestrator.db?mode=ro", uri=True)

# data plane  (NAS is 192.168.1.30, NOT 10.100.23.30)
ssh -o BatchMode=yes karl@192.168.1.30 'docker ps'
ssh karl@192.168.1.30 'docker logs -t orchestrator-agent 2>&1 | grep "epic/validate"'
ssh karl@192.168.1.30 'docker exec -i orchestrator-agent python -' < cov.py
# agent is reachable only from the LXC (macvlan):
ssh root@10.100.23.105 'curl -s http://192.168.1.44:8780/v1/health'
```
