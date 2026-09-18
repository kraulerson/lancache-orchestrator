# cache-catcher

The fanotify guard that watches the lancache filesystem on the NAS and emails
when cached game files disappear or become unreadable. It is the tripwire for
the **2026-07-31 mass-deletion incident** class: nginx's cache-manager evicting
from a nearly-full `keys_zone`, silently, for days.

## Why these files are here

Until 2026-09-16 this code lived **only** inside the running container, in the
persistent `/log` volume. There was no version control, no review, and no way to
test a change before it was the thing guarding the cache. Issue #337 was the
prompt: the guard had the deleting process on every log line and ignored it, so
an operator purge raised the same emergency alert as a real eviction.

| file | role |
|---|---|
| `delete_actor.py` | pure, stdlib-only: who deleted, and does it count as eviction |
| `key_budget.py` | pure, stdlib-only: sampling maths, capacities, projection, verdict |
| `kuma.py` | pure, stdlib-only: push one Uptime Kuma heartbeat, never raise |
| `key_budget_probe.py` | I/O shell for `key_budget.py` — reads dirs and `/proc`, pushes |
| `fanotify_guard.py` | the deployed guard — loads `libc.so.6`, NAS-only |

`fanotify_guard.py` cannot be imported off the NAS (`ctypes.CDLL("libc.so.6")`
runs at import), which is exactly why the decision logic is in `delete_actor.py`
instead. That module is covered by `tests/tools/test_delete_actor.py` and runs in
CI like any other code. `key_budget.py` and `kuma.py` follow the same split for
the same reason, tested by `tests/tools/test_key_budget.py` and
`tests/tools/test_kuma.py`.

**Imports are flat on purpose.** In the container every file sits directly in
`/log/`, so `fanotify_guard.py` does `from key_budget_probe import ...`. In the
repo the same files are a package, so tests do
`from tools.cache_catcher.key_budget import ...`. Both shapes must keep working;
do not turn the container-side imports into relative ones.

## The two instruments

The guard runs the fanotify loop in its main thread and two daemon threads
beside it. Each thread feeds **its own** Kuma monitor, so one thread dying leaves
its monitor silent and red while the others stay green — a partial failure stays
visible instead of being masked.

| thread | monitor | period | silence means |
|---|---|---|---|
| liveness | `lancache:cache-guard` | 15 min | the guard process is dead |
| liveness | `lancache:cache-eviction` | 15 min | the guard process is dead |
| probe | `lancache:key-budget` | 24 h | the probe thread is dead |

**`cache-guard` and `cache-eviction` are deliberately separate monitors.** One
monitor carrying both signals cannot distinguish "the guard is dead" from "the
cache is being evicted" — the same defect #326, #330 and #337 each describe.
`cache-guard` answers only *is this process running*. `cache-eviction` answers
only *is cache loss happening now*: it goes DOWN the moment an `eviction` or
`mode000` alert fires and stays DOWN for `EVICT_LATCH_SEC` (1 h) after the last
one, because Kuma treats silence as DOWN and so the monitor needs a heartbeat of
its own — without the latch the next heartbeat would flip it green 15 minutes
into an incident that was still running. A commanded `purge` moves no monitor.

### The keys_zone gauge

`df` cannot see cache-index occupancy, and nginx OSS publishes no gauge for it:
no `http_api_module`, `stub_status` omits cache zones, and the error log stays
silent through the normal evict-to-fit path — it logged nothing at all through
the nine days of the 2026-07-31 incident. So the probe derives it: sample
`SAMPLE_LEAVES` random leaf directories, scale the mean to all 65536, and
compare against the nearest real ceiling — `min(zone_keys, ram_keys)`, named in
the message so the operator knows which one is binding.

It pushes DOWN on crossing `FLOOR` **or** on projecting less than
`HORIZON_DAYS` to the floor, and DOWN rather than silent when the sample or the
ceiling is unknown. Until the history file has accumulated, the projection
reports `trend unknown` — it never reports a comfortable runway it cannot
justify.

**Counting must be done as root, inside a container.** Some leaf directories are
mode `0700` (e.g. `/volume1/cache/cache/00`), and **a denied read is
indistinguishable from an empty directory**. Counting as `karl` from the NAS host
produced a figure **13× low** during design, and it looked entirely plausible.
`summarise_sample()` therefore counts an unreadable leaf as a read failure, never
as a leaf with no files in it, and `tests/tools/test_key_budget.py` pins that.

**The daily path uses `listdir` and never `stat`.** `stat` runs at roughly 180
files/sec on this NAS under sweep load; a 54k-file walk took over five minutes
during design. Mean object size is a separate, rarer measurement for that reason.

## Deploying a change

Both files live in the container's `/log` volume, which survives `docker rm`:

```sh
scp tools/cache_catcher/delete_actor.py tools/cache_catcher/kuma.py \
    tools/cache_catcher/key_budget.py tools/cache_catcher/key_budget_probe.py \
    tools/cache_catcher/fanotify_guard.py karl@192.168.1.30:/tmp/
ssh karl@192.168.1.30 'for f in delete_actor.py kuma.py key_budget.py \
                                key_budget_probe.py fanotify_guard.py; do \
                         docker cp /tmp/$f cache-catcher:/log/$f; done && \
                       docker restart cache-catcher'
```

`/log/entrypoint.sh` runs the guard as the container's main process, so a restart
picks up the new code. Verify afterwards:

```sh
ssh karl@192.168.1.30 'docker logs --since 3m cache-catcher | head -20'
# expect: FANOTIFY guard started (delete+attrib; evict>=10/60s; ...)
#         MONITOR threads started (liveness 900s, key-budget 86400s)
# then, within a few seconds: KEY-BUDGET up: <N>M objects, <N>% of ram ceiling ...
```

This deploy touches **only the `cache-catcher` container** — not lancache, not
the orchestrator agent, not the control plane. It therefore does *not* have to be
scheduled into an inter-sweep gap, and the standing container-recreate rule does
not bind. That was a design choice, not a happy accident.

## Alert semantics (#337)

| deleter | classified | alert |
|---|---|---|
| `python -m orchestrator.agent` | `orchestrator` | **NOTICE**, commanded purge, own cooldown |
| `nginx` | `nginx` | **ALERT**, eviction — names the actor |
| anything else, or unattributable | `other` | **ALERT**, eviction — names the actor |

**Unknown alerts.** Only a positively identified orchestrator agent is treated as
commanded. A process whose `/proc` entry vanished before the event was read still
counts toward the eviction signature — a monitor that guesses "probably fine" is
worse than none, because the incident it exists for ran unnoticed for days.

Config (SMTP credentials) stays in `/log/alert.env` on the NAS and is **not**
version-controlled.

## `/log/keybudget.env` (not version controlled)

Read by `key_budget_probe.load_cfg()` and shared with the guard. Every key has a
default in `key_budget_probe.DEFAULTS`, so a missing file is a working
configuration with all three monitors disabled. **An unset push URL disables that
heartbeat** — which is why `kuma.push` reports a blank URL as delivered rather
than as a failure to retry.

| key | default | meaning |
|---|---|---|
| `KUMA_PUSH_KEY_BUDGET` | *(unset)* | push URL for `lancache:key-budget` |
| `KUMA_PUSH_CACHE_GUARD` | *(unset)* | push URL for `lancache:cache-guard` |
| `KUMA_PUSH_CACHE_EVICTION` | *(unset)* | push URL for `lancache:cache-eviction` |
| `SAMPLE_LEAVES` | `256` | leaf directories sampled per run (of 65536) |
| `RAM_BUDGET_BYTES` | `9663676416` (9 GiB) | memory the zone is allowed to occupy |
| `FLOOR` | `0.75` | fraction of the ceiling that trips DOWN |
| `HORIZON_DAYS` | `90` | projected days to floor that trips DOWN |
| `PROBE_INTERVAL_SEC` | `86400` | seconds between gauge runs |

**The push URLs are credentials.** Treat the file exactly as `alert.env` is
treated: written directly on the NAS, owned by root, mode `600`, never pasted
into a commit, a log or a chat. Nothing in this code logs a push URL.

`RAM_BUDGET_BYTES` is the honest ceiling on this host, not the configured one.
`keys_zone=10000m` would need ~10 GiB of shared memory on a 15.4 GiB NAS that
also carries an 8 GiB agent limit, so the host OOMs before the index fills — the
zone cannot reach its configured capacity. That is
[#346](https://github.com/kraulerson/lancache-orchestrator/issues/346), which
needs a lancache restart and is deliberately out of scope here; the gauge
measures its ceiling from live RAM instead of trusting the configured number.
