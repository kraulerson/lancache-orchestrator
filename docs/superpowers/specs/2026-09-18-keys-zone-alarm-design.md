# keys_zone Alarm — Design

**Date:** 2026-09-18
**Status:** Approved (Orchestrator: Karl Raulerson)
**Incident behind it:** the 2026-07-31 mass deletion — nginx's cache-manager
evicting live game data from a ~94 %-full `keys_zone`, silently, for nine days
**Supersedes assumption:** that a keys_zone alarm is a percentage threshold on a
capacity number read from config

## Problem

The cache index is invisible. `df` cannot see it, nginx OSS exposes no gauge for
it, and when it fills, nginx deletes cached games to make room — with terabytes
of disk free and nothing written to any log.

This is not hypothetical. On 2026-07-31 it destroyed ~85 000 cache files across
the whole library. `bpftrace` caught nginx at ~31 unlinks/s. The zone was
`keys_zone=generic:4000m` (~32.8 M keys) against a ~30.7 M-object library — about
94 % full. Karl's own record has carried **"key-budget alarm REQUIRED"** as an
open action ever since.

### Why nginx will never tell us

Confirmed against the live deployment on 2026-09-18:

- `nginx/1.24.0` OSS. `configure` has `--with-http_stub_status_module` but no
  `http_api_module` (nginx Plus only). `stub_status` does not report cache zones.
- The error log at `/data/logs/error.log` contains **zero** zone or allocation
  messages across its entire history — including straight through the July
  incident.

nginx logs only when a forced expire *fails outright*. The normal path — evict
someone's game to fit a new key — is silent by design. There is no gauge to read,
so the metric has to be derived.

### Three ceilings, and the configured one is not the binding one

Measured 2026-09-18 by sampling leaf directories as root inside the lancache
container (100 leaves, 54 317 files; cross-checked against an independent
200-leaf sample at 35.9 M — ~1 % spread):

```
objects              35.60 M          zone used            44.5 %
mean object size     718 KiB          cache size           23.80 TiB
break-even size      708 KiB     ->   current mean is 101 % of break-even
keys to fill disk    78.9 M      vs   zone capacity 80.0 M   = 1.4 % margin
```

| Ceiling | Binds at | Behaviour when reached |
|---|---|---|
| `max_size=54000g` disk | ~78.9 M keys | **Healthy.** LRU evicts cold objects; the cache works as designed. |
| `keys_zone=10000m` | 80.0 M keys | **Pathological.** Silent eviction of live data with disk free. This is July. |
| **Host RAM (15.4 GiB)** | **~50–66 M keys** (budget-dependent) | **OOM kill**, container chosen by the kernel. |

Two findings follow, and both shape the design.

**The disk and key ceilings are effectively tied — 1.4 % apart.** `10000m`
landing that close to the disk ceiling is coincidence, not design. Break-even
mean object size is `54000 GiB ÷ 80 M = 708 KiB`; the measured mean is 718 KiB.
Any drift toward smaller objects (Epic ChunksV4 are tiny and object-dense) tips
the binding constraint out of the healthy mode and into the silent-data-loss one.

**Neither is what this host hits first.** The zone is shared memory that grows as
keys are inserted:

```
lancache-monolithic   4.855 GiB / 15.4 GiB   (31.5 %)   <- no mem_limit set
orchestrator-agent    839.7 MiB /  8 GiB     (mem_limit 8g; has OOMed before)
free -g:  total 15   used 6   shared 4   available 8
```

4.855 GiB at 44.5 % fill ≈ **146 bytes/key**, consistent with nginx's documented
~8 000 keys/MB. A *full* 10000m zone needs ~10 GiB on a 15.4 GiB host that also
carries an 8 GiB agent limit. **The zone cannot physically reach its configured
capacity — the host OOMs first.** The real headroom is whatever RAM budget
lancache is allowed, which is a policy choice (see Thresholds), not the "55 %
free" the configured number implies.

Right-sizing `CACHE_INDEX_SIZE` and putting a `mem_limit` on
`lancache-monolithic` is **out of scope here and filed separately**. This alarm
is what buys the time to do that properly.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Two instruments — a leading gauge and a lagging tripwire | They answer different questions and need different availability. A gauge predicts; a tripwire catches what outruns the prediction. |
| 2 | Both live in `tools/cache_catcher/` | The tripwire is already there, already version-controlled, already tested. One home, one deploy, one liveness story. |
| 3 | Object count by **statistical sampling** | MD5 distributes uniformly, so 256 leaves give ~±0.3 % at the cost of 256 directory reads. A full walk of 35.6 M files is hours of metadata I/O competing with the sweep. |
| 4 | Alarm on **floor OR projection**, never a bare percentage | 45 %-flat and 45 %-climbing-fast demand different responses. The floor is a stateless backstop; the projection buys lead time. |
| 5 | Effective ceiling is `min(zone, RAM)`, measured live | The configured number is unreachable on this hardware. Alarming against it would promise headroom that does not exist. |
| 6 | Threads inside the existing guard, **no cron** | Per-instrument Kuma monitors make partial failure visible, and it avoids the crontab `%` trap that silently killed the first disk monitor. |
| 7 | Promote the tripwire to Kuma, keep the email | Today it is email-only and cannot prove it is alive. Its silence is indistinguishable from a healthy cache. |

## Design

### Components

`fanotify_guard.py` runs `ctypes.CDLL("libc.so.6")` at import and cannot be
imported off the NAS. That constraint already forced `delete_actor.py` to exist;
it forces the same split here. All decision logic lives in pure modules CI can
reach.

| file | status | role |
|---|---|---|
| `tools/cache_catcher/key_budget.py` | new | pure stdlib, no I/O: extrapolation, standard error, projection, verdict |
| `tools/cache_catcher/kuma.py` | new | stdlib push helper; never raises; returns whether it was delivered |
| `tools/cache_catcher/key_budget_probe.py` | new | impure runner: sample, read RSS, append history, push. NAS-only |
| `tools/cache_catcher/fanotify_guard.py` | modified | `alert()` also pushes Kuma DOWN; new liveness thread pushes UP |

`kuma.py` mirrors the semantics already established twice in this system —
`src/orchestrator/clients/heartbeat.py` and `push_kuma()` in
`run-steam-prefill.sh`: the URL is the whole credential, an unset URL disables
the push, and **no failure to report ever changes the outcome of the thing being
reported on**.

### The gauge — daily, cheap path

`stat` costs ~180 files/sec on this NAS under sweep load; a 54 k-file size walk
timed out at over five minutes during design. The daily path therefore uses
`listdir` only and never stats a file.

1. Sample **256** distinct random leaf dirs of 65 536 (without replacement) →
   object estimate **and standard error**. The error is carried into the message
   so a borderline reading is not over-trusted. A leaf that cannot be read is
   counted as a read failure, never as an empty leaf — treating denied as empty
   is what made the first measurement during design under-report by 13×.
2. Read nginx's RSS from host `/proc` (the container is `privileged` with
   `pid=host`) → live `bytes_per_key = rss ÷ objects`.
3. Compute the ceilings:

```
zone_keys  = CACHE_INDEX_SIZE_MB × 8000        # nginx's documented density
ram_keys   = RAM_BUDGET_BYTES ÷ bytes_per_key  # measured, self-correcting
disk_keys  = 54000 GiB ÷ mean_object_size      # context only
effective  = min(zone_keys, ram_keys)
```

4. Append a row to `/log/key_budget.csv`:
   `ts, objects, stderr, rss_bytes, bytes_per_key, zone_mb`.
5. Verdict — **DOWN** if either holds:
   - `objects ≥ FLOOR × effective`
   - projected days until `effective` `< HORIZON`

`CACHE_INDEX_SIZE` is read from the running nginx's `/proc/<pid>/environ`, not
hardcoded. This is deliberate: #315 is an open bug about exactly this shape — a
bare constant governing something that is configurable elsewhere. If the environ
read fails, the fallback value is logged loudly and named in the Kuma message,
never applied silently.

**Disk is context, not an alarm.** Disk-binding is the healthy mode. The gauge
reports which ceiling is nearest; it goes DOWN only on the two that are not.

### The gauge — weekly, expensive path

Mean object size from a 20-leaf sample (~11 k stats, ~60 s). Reported in the
message; **never alarmed on**. Its only job is to answer whether disk or zone
will bind first — the 708 KiB break-even above. A weekly cadence keeps the cost
off the daily path.

`disk_keys` is therefore derived from a figure up to seven days old. That is
acceptable because it is context rather than an alarm input, but the message
states the measurement's age so a stale value is never mistaken for a fresh one.
If no measurement has ever succeeded, `disk_keys` reports `unknown` — it does not
fall back to a guess.

### Thresholds

`RAM_BUDGET_BYTES` is the one judgement call in this design. Set naively —
`MemTotal − agent_limit − OS` = 15.4 − 8 − 1.5 = 5.9 GiB — the monitor is **born
red**, because lancache already occupies 4.855 GiB. `fetch_manifests_max_failure_ratio`
carries a comment warning about precisely this, and a monitor that is red on day
one gets muted and then ignored.

The agent's 8 GiB is a *limit*, not consumption; it is using 840 MiB. The budget
is therefore set against realistic peak, not the limit:

```
RAM_BUDGET_BYTES = 9 GiB
  9 GiB ÷ 146 B/key   = ~66 M keys effective ceiling   (vs 80 M configured)
  current 35.6 M      = 54 % of effective
FLOOR   = 0.75        -> 49.6 M keys
HORIZON = 90 days
  runway to floor     = ~14 M keys
```

All four are settings in `/log/keybudget.env`, not constants. The defaults above
are the starting point; they are meant to be tuned once the numbers have been
visible for a while, which is the entire point of putting them on the monitor.

**How long is 14 M keys?** Honestly: not yet knowable, and this is why the
history file exists.

```
30.7 M (Jul 31)  ->  34.4 M (Aug 12)  ->  35.6 M (Sep 18)
Jul 31 - Aug 12   +3.7 M over 12 days  = ~308 k/day   (the forced-refill period)
Aug 12 - Sep 18   +1.2 M over 37 days  = ~32 k/day
```

At 32 k/day the runway is ~14 months; at the 106 k/day implied by the longer
baseline, ~4 months; during a refill or Epic-storm burst, ~45 days. Those
estimates are not equally trustworthy: the sampling error on a single reading is
about ±1 % (±0.36 M), which is a third of the entire Aug 12 → Sep 18 delta. The
recent growth rate is therefore within noise of its own measurement.

**A projection built on two points this noisy would be false precision.** The
gauge must accumulate its own consistently-sampled history before its projection
means anything — and until it does, the projection reports `unknown`, which the
floor covers. This is the honest reading, not a caveat bolted on.

### Tripwire promotion

The eviction detector already works and already distinguishes a commanded purge
from an eviction (#337). What it cannot do is prove it is alive.

- `alert()` gains a Kuma **DOWN** push alongside the existing email. The email
  stays — it is the channel that actually reaches Karl.
- A liveness thread pushes **UP** every 15 min with rolling counts, so silence
  means the guard is dead.
- Commanded purges stay email-only NOTICEs and change no monitor state. The
  whole point of #337 was that a purge is not an alarm.

### Kuma monitors

Two new push monitors in group **119** ("host: lancache-orchestrator (1105)"),
each **bound to notification 3 ("Telegram - Storage")**. Per §3.1 of
`docs/deploy/live-configuration.md`, binding is not optional — monitors 176–182
spent months turning red on a dashboard and telling nobody.

| monitor | fed by | silence means |
|---|---|---|
| `lancache:key-budget` | probe thread, daily | the probe thread died |
| `lancache:cache-guard` | liveness thread, 15 min | the guard died |

Created with the §3.2 procedure: stop `uptime-kuma`, back up
`/opt/uptime-kuma/data/kuma.db` to `kuma.db.bak-prekeybudget-<YYYYMMDD-HHMMSS>`,
edit, restart. Push URLs are credentials; they live in `/log/keybudget.env`,
uncommitted, exactly as `alert.env` does.

### Failure modes

Monitoring must never break the thing it monitors. Every push is swallowed;
every sample is wrapped. Three rules are non-obvious and each encodes a failure
this system has already had:

1. **A failed sample pushes DOWN** with the reason — never silence. Silence is
   how the July incident lasted nine days.
2. **A missing or corrupt history file makes the projection report `unknown`,
   not infinite runway.** Absence of a trend must never read as safe. The floor
   is stateless and keeps working regardless.
3. **Every run pushes, up or down.** Kuma treats silence as DOWN, so a dead
   thread, a dead container and a wrong URL all surface as red — the property
   that makes monitors 216 and 217 meaningful.

## Testing (test-first)

`tests/tools/test_key_budget.py`, written and verified failing before any
implementation, following the Phase 2 Build Loop.

Pure-logic coverage:

- extrapolation from leaf samples, including uneven and empty leaves
- standard error on a known distribution
- projection with a normal history; with a **single** sample; with an **empty**
  history — the last must yield `unknown`, and a test asserts `unknown` does not
  read as safe
- a **shrinking** cache (eviction already in progress) must not project a
  comfortable runway
- floor-vs-projection precedence: either alone trips DOWN
- `effective = min(zone, ram)` selection, including the case where RAM binds
- degenerate inputs: zero objects, zero `bytes_per_key`, absent `CACHE_INDEX_SIZE`

`fanotify_guard.py`'s `ctypes` path remains untestable off the NAS, as today.
That is the reason the split exists and it is documented in the README.

## Rollout

1. Tests first, verified failing.
2. Implement; update `README.md`, `CHANGELOG.md`, `FEATURES.md`.
3. Deploy via the README's existing recipe — `scp` → `docker cp` → `docker
   restart cache-catcher`. **This touches nothing that serves traffic or runs
   jobs**: not lancache, not the agent, not the orchestrator. The recreate rule
   and the ~30 min inter-sweep gap do not constrain it.
4. Create and bind the two Kuma monitors (§3.2 procedure, DB backed up first).
5. **Verify by waiting for a real scheduled run**, not a manual invocation. A
   manual test of a scheduled job proves nothing about the schedule — the disk
   monitor's `%` bug passed a manual test and never ran.

## Out of scope

- **Right-sizing `CACHE_INDEX_SIZE` and adding a `mem_limit` to
  `lancache-monolithic`.** Filed separately. It requires a lancache restart,
  which is a far larger operational event than a watcher deploy, and it is the
  fix this alarm exists to buy time for.
- Alarming on disk. `/volume1` is `CACHE_DISK_SIZE=54000g` on 55 TB and is
  *designed* to fill and evict by LRU; a percentage alarm there fires during
  healthy operation, which is the #326/#330 defect.
- `/volume2` disk alarm — unrelated housekeeping, tracked in the handoff.

## Notes on framing

The original framing of this task warned against a percentage threshold as a
repeat of #326/#330. That is correct for `/volume1` and does not transfer to the
keys_zone: the disk is meant to fill, the index is not, so index fill is
unambiguously pathological and a gauge on it *can* tell healthy from broken. The
substantive reason not to use a bare percentage is different — a percentage of
the **configured** capacity is a number this host cannot reach, and 45 %-flat and
45 %-climbing need different responses. Hence `min(zone, RAM)` plus a trend.
