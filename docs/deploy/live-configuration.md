<!-- Last Updated: 2026-09-10 -->

# Live configuration — both production hosts (operator)

The state of the two production hosts as they actually run today, and **which
failure each setting prevents**. Verified against the live boxes on 2026-09-10.

## Why this document exists

Between 2026-09-08 and 2026-09-10 a run of production fixes was applied *directly
to the machines*. They are correct and they work — but several of them live only
on the hosts, in files that are **not in this repository and not version
controlled anywhere**:

| Not version controlled | Host | Contains |
|---|---|---|
| `/home/karl/lancache-host/docker-compose.yml` (and `agent.env`, `.env`) | NAS | the agent's uid, memory cap, DNS, healthcheck |
| `/home/karl/lancache-host/run-steam-prefill.sh` | NAS | the Steam prefill wrapper + its Kuma heartbeat |
| `karl`'s crontab | NAS | prefill schedule + the push-URL environment line |
| `/root/orch-lxc.env`, `/root/deploy-orchestrator-lxc.sh`, `/etc/cron.d/orch-breaker-heartbeat` | LXC 1105 | tokens, feature flags, sweep tuning, breaker heartbeat |
| `/opt/uptime-kuma/data/kuma.db` | CT 1057 | every monitor and notification binding |

Rebuild any host from this repo alone and every one of those is lost. **Two have
already been lost once and each caused a production outage** — the agent's
`user: "0:0"` (every game measured ~8% cached) and its `dns:` (Steam prefill
failed silently for 13 days). Both were re-applied by hand and are now persisted
in compose; this document is the record of *why* they are there, so the next
person does not delete them as noise.

Most entries below are annotated in the live files themselves. This is the
cross-host summary; the files remain authoritative.

> `docs/deploy/lxc-cutover-runbook.md` is the **one-time** migration procedure
> that produced this topology. It is history. This document is the living state.

**Topology recap.** Control plane (API, jobs, scheduler, SQLite) on Proxmox LXC
1105 = `10.100.23.105`. Data-plane agent (byte-puller, disk-stat, prefill
drivers) on the UGREEN NAS beside lancache: SSH `karl@192.168.1.30`, but the
agent container itself sits on the `lancache_mv` macvlan at `192.168.1.44:8780`
and lancache at `192.168.1.40`. Monitoring on CT 1057 = `10.100.23.57:3001`.

---

## 1 — LXC 1105 (`10.100.23.105`): control plane

Container `orchestrator`, image `orchestrator:dpa`, `--network host`, DB on the
`orchestrator-data` docker volume. Health is **`/api/v1/health` on port 8765** —
not `/health`, not 8080. It requires the bearer token:

```sh
T=$(grep '^ORCH_TOKEN=' /root/orch-lxc.env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $T" http://localhost:8765/api/v1/health
# {"status":"ok", ..., "scheduler_running":true, "validator_healthy":true, "agent_reachable":true}
```

### 1.1 `/root/orch-lxc.env`

Variable **names** and the non-secret values that matter operationally. Never
print or copy the secret values anywhere — `ORCH_TOKEN` and the five
`ORCH_KUMA_PUSH_*` push URLs are credentials.

| Variable | Value | Note |
|---|---|---|
| `ORCH_TOKEN` | *(secret)* | shared with the NAS agent's `agent.env` — the same token both ways |
| `ORCH_POOL_READERS` | `4` | |
| `ORCH_DB_CACHE_SIZE_KIB` | `8192` | |
| `ORCH_DB_MMAP_SIZE_BYTES` | `134217728` | |
| `ORCH_LANCACHE_HEARTBEAT_URL` | `http://192.168.1.40/lancache-heartbeat` | lancache liveness, straight to the monolithic container |
| `ORCH_API_HOST` | `0.0.0.0` | bound wide; the allowlist below is what actually restricts it |
| `ORCH_ALLOWED_SOURCE_IPS` | `10.100.23.102,10.100.23.57` | Game_shelf + Uptime Kuma. **Fail-closed at boot** — an empty/absent value refuses to start rather than serving everyone (PR #173) |
| `ORCH_AGENT_BASE_URL` | `http://192.168.1.44:8780` | the agent's **macvlan** address, not the NAS host address |
| `ORCH_AGENT_ENABLED` | `true` | |
| `ORCH_SCHEDULED_PREFILL_ENABLED` | `true` | Epic scheduled prefill. Was `false` through the post-#305 convergence period; re-enabled once the recovery sweeps completed |
| `ORCH_SWEEP_BATCH_SIZE` | `2` | see below |
| `ORCH_KUMA_PUSH_LIBRARY_SYNC` | *(secret URL)* | job heartbeat → Kuma 179 |
| `ORCH_KUMA_PUSH_SWEEP` | *(secret URL)* | → Kuma 180 |
| `ORCH_KUMA_PUSH_SCHEDULED_PREFILL` | *(secret URL)* | → Kuma 181 |
| `ORCH_KUMA_PUSH_FETCH_MANIFESTS` | *(secret URL)* | → Kuma 182 |
| `ORCH_KUMA_PUSH_MEASUREMENT_BREAKER` | *(secret URL)* | → Kuma 216, and read by the cron heartbeat in §1.2 |

**Deliberately absent:** `ORCH_VALIDATION_SWEEP_ENABLED`. It defaults to `true`,
so the scheduled validation sweep is on. Do not "helpfully" add it set to
`false`; if you ever set it to pause sweeps, delete the line again afterwards
rather than flipping it back to `true`, so the file keeps saying only what
differs from the defaults. `ORCH_MEASUREMENT_BREAKER_THRESHOLD` is likewise
absent and therefore at its default (25 downward transitions).

**`ORCH_SWEEP_BATCH_SIZE=2` — why 2 and not more (PR #303).** The agent's
disk-stat worker pool has **2** workers. Any control-plane concurrency above 2
does not add throughput: the extra calls simply queue behind the pool, and the
per-call timeout budget starts running while the request is still waiting for a
worker, so a wider batch makes calls *time out* that would otherwise have
succeeded. It distorts every timing signal the sweep has. If the agent's pool
size ever changes, change this to match it — they are one number in two files.

### 1.2 `/etc/cron.d/orch-breaker-heartbeat`

```
17 6 * * * root U=$(grep '^ORCH_KUMA_PUSH_MEASUREMENT_BREAKER=' /root/orch-lxc.env | cut -d= -f2-); \
  [ -n "$U" ] && curl -fsS -m 15 "$U?status=up&msg=breaker+armed+daily+heartbeat" >/dev/null 2>&1
```

**Why this exists.** Kuma 216 is a *push* monitor, and a push monitor treats
silence as DOWN. The orchestrator only ever pushes to it on a **trip** (`down`)
— a healthy breaker is, by design, silent. Without a daily `up`, the monitor
would sit permanently red and be worthless. With it, the semantics are clean:

- **red because the orchestrator pushed `down`** → the cache-loss circuit
  breaker actually tripped; investigate the sweep.
- **red because nothing arrived for ~48 h** → the LXC, the container, or cron is
  dead. Also a real alarm, and one nothing else was catching.

Do not remove this cron thinking it is cosmetic; removing it converts monitor 216
from a working alarm into a permanent false alarm.

### 1.3 Deploy procedure

The clone at `/root/lancache-orchestrator` is **not kept current between
deploys** — it is normal for it to be dozens of commits stale. Always pull first.

```sh
# 0. Tag the current image for rollback FIRST — on both hosts.
docker tag orchestrator:dpa orchestrator:dpa-pre-<PR-or-issue>

# 1. Build on the LXC (it has the clone and the Dockerfile).
cd /root/lancache-orchestrator && git pull --ff-only
docker build -t orchestrator:dpa .

# 2. Restart the control plane.
sh /root/deploy-orchestrator-lxc.sh
```

Rollback tags present today include `orchestrator:dpa-pre-305`,
`dpa-pre-303`, `dpa-pre-deps`, plus a long tail back to `dpa-pre-epic`. The
convention is **tag before every build**, named for the PR or issue being
deployed. Roll back by re-tagging and re-running the deploy script:

```sh
docker tag orchestrator:dpa-pre-<ref> orchestrator:dpa && sh /root/deploy-orchestrator-lxc.sh
```

If the change also touches `agent/`, the identical image must reach the NAS.
The LXC **cannot** SSH to the NAS (host key verification fails, and that is not
to be "fixed" on a production box without asking) — stream it through a machine
that reaches both:

```sh
# From the Mac:
ssh root@10.100.23.105 'docker save orchestrator:dpa' | ssh karl@192.168.1.30 'docker load'
```

Tag on the NAS too, before the load: `docker load` silently steals the `:dpa`
tag from the old image and leaves it dangling as `<none>`, recoverable only by
digest.

### 1.4 DB backup convention

Before any deploy carrying a migration, take a backup **from inside the running
container** using SQLite's backup API. That is WAL-safe and needs no downtime —
`cp` of a live WAL database is not:

```
/var/lib/orchestrator/backup-pre-<ref>-<UTC timestamp>.db
```

Live example from the migration-0015 deploy:
`/var/lib/orchestrator/backup-pre-305-20260908T111950Z.db` (~1.1 GB, alongside
`orchestrator.db` in the `orchestrator-data` volume). These are full copies of a
gigabyte-scale DB; prune old ones deliberately, and keep at least the one
matching the currently-deployed image tag.

---

## 2 — NAS `192.168.1.30`: data-plane agent

Everything lives in `/home/karl/lancache-host/` (compose project: lancache
monolithic + lancache-dns + `orchestrator-agent` + cache-catcher). **This
directory is not version controlled** — which is exactly why the settings below
need writing down. Deploy is `docker compose up -d --force-recreate
orchestrator-agent`; compose never builds the image (bare local tag
`orchestrator:dpa`, no `build:` key).

### 2.1 The four settings on `orchestrator-agent` that are load-bearing

**`user: "0:0"` — required, and already lost once.**
lancache's cache directories are really `0700 www-data`. Running as uid 1000 the
agent can enter only **65 of the 256** hash buckets, so every game measures at
roughly 8% cached and the whole cache-truth pipeline reports a fabricated
catastrophe. The fix was originally applied only to the *running* container; a
recreate on 2026-09-08 silently dropped it and reproduced the incident. It is
now in compose. Do not "tidy" it away as a security smell — the alternative is a
measurement layer that lies.

**`mem_limit: 8g` — the difference between a restart and a zombie.**
A sweep at concurrency 10 grew the agent to 9.9 GB (Epic manifests decode into
300k-chunk lists) and the kernel's **global** OOM killer eventually took it after
hours of thrashing — leaving a container Docker still displayed as `Up` with no
process inside it, i.e. an outage that monitoring could not see. With a cgroup
limit the kill is immediate and container-scoped, and `restart: unless-stopped`
brings the agent straight back.

**`dns: [192.168.1.40]` — required, and already lost once.**
Without it, `lancache.steamcontent.com` resolves to the real Steam CDN and
SteamPrefill aborts in seconds with `LancacheNotFoundException`. This was lost
when the container was started by a hand-typed `docker run` instead of compose,
and Steam prefill then failed **98 consecutive runs over 13 days** while
monitoring stayed green (see §2.4 for the other half of that bug).

**The explicit `healthcheck` on `127.0.0.1:8780/v1/health`.**
The inherited image healthcheck probes the *control plane* on `:8765`, a port the
agent never binds — so the container reports `unhealthy` forever and can never
signal a real outage. The override probes the agent's own port:

```yaml
healthcheck:
  test: ["CMD", "python", "-c",
         'import httpx; httpx.get("http://127.0.0.1:8780/v1/health").raise_for_status()']
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 10s
```

### 2.2 `agent.env` (not version controlled)

Names and non-secret values: `ORCH_TOKEN` *(secret, matches the LXC)*,
`ORCH_POOL_READERS=4`, `ORCH_DB_CACHE_SIZE_KIB=8192`,
`ORCH_DB_MMAP_SIZE_BYTES=134217728`,
`ORCH_LANCACHE_HEARTBEAT_URL=http://192.168.1.40/lancache-heartbeat`,
`ORCH_API_HOST=0.0.0.0`, `ORCH_ALLOWED_SOURCE_IPS=10.100.23.102,10.100.23.105`,
`ORCH_AGENT_BASE_URL=http://127.0.0.1:8780`,
`ORCH_SCHEDULED_PREFILL_ENABLED=false`, `ORCH_AGENT_ENABLED=true`,
`ORCH_STEAM_USERNAME`, `ORCH_AGENT_BIND_HOST=0.0.0.0`,
`ORCH_AGENT_BIND_PORT=8780`, `ORCH_LANCACHE_BASE_URL=http://192.168.1.40`.

`ORCH_LANCACHE_BASE_URL` is what Epic validation resolves against; omitting it
breaks Epic specifically, not Steam, so the failure looks platform-shaped rather
than config-shaped. Env changes require a container **recreate**, not a restart.

### 2.3 Before any recreate: sync manifests to the archive

**Do this first, every time.** SteamPrefill's live manifest cache is
`/tmp/.cache/SteamPrefill` *inside the container* and a recreate destroys it —
confirmed live once at 62 live `.bin` → 0. Anything not yet copied to
`/manifest-archive` is gone permanently, and the affected games silently revert
to invisible.

```sh
docker exec orchestrator-agent python -c \
  'from pathlib import Path; from orchestrator.agent.manifest_archive import sync_manifests_to_archive; \
   print(sync_manifests_to_archive(Path("/tmp/.cache/SteamPrefill"), Path("/manifest-archive")))'
```

It prints the number of manifests copied, is append-only, and is safe to run at
any time.

### 2.4 `run-steam-prefill.sh` + the crontab

Steam prefill is a **host cron** (every 6 h), not an orchestrator job; Epic is the
orchestrator's. They share no lock, deliberately.

- **The wrapper now pushes its own Kuma heartbeat** — `status=up` only when
  **both** passes exit 0, otherwise `status=down` with the failure reason — and
  it **exits non-zero on failure**. Previously it always exited 0 and the
  crontab appended `&& curl …status=up`, so Kuma was told "success" after every
  run of the 13-day total outage. A wrapper that reports its own outcome is the
  only version of this that can ever go red.
- The push URL lives in a crontab **environment line**
  `KUMA_PUSH_STEAM_PREFILL=` (name only — never print or paste the URL). Unset =
  no push, which the script tolerates.
- **Two timeouts.** `RECENT_MAX=4h` bounds the `--recently-purchased`
  new-purchase discovery pass; `RUN_MAX=10h` bounds the selection pass. They are
  separate so a stall in the first cannot eat the budget of the second and
  starve the actual prefill. Either timeout sends an email alert.
- **Skip counting.** Overlapping runs are still skipped (concurrent SteamPrefill
  runs corrupt the shared cache and `Config/` state), but consecutive skips are
  counted and an email goes out after **3** (~18 h). This exists because a
  SteamPrefill was once found alive for 5 d 18 h *after* printing "Prefill
  complete!" — a bare `flock -n` would have skipped every tick from then on, in
  silence.
- Backups of the pre-change versions: `run-steam-prefill.sh.bak-20260909` and
  `.locks/crontab.bak-20260909`.

### 2.5 Post-recreate verification — all three have failed in production

Run all three after **every** agent recreate. Each corresponds to a real outage.

```sh
# 1. Running as root (0700 cache dirs) — expect 0
docker exec orchestrator-agent python -c "import os; print(os.getuid())"

# 2. All cache buckets visible — expect 256
docker exec orchestrator-agent sh -c 'ls /data/cache/cache | wc -l'

# 3. lancache DNS override in effect — expect 192.168.1.40
docker exec orchestrator-agent getent hosts lancache.steamcontent.com
```

Verified 2026-09-10: `0`, `256`, `192.168.1.40`. If #1 returns 1000 or #2
returns 65, stop — do not let a sweep run; it will overwrite cache truth with
garbage.

**Health-check gotcha:** the agent is on a macvlan and is **not** reachable from
the NAS host's own shell (`127.0.0.1`, `.30`, `.31` all fail) — a curl from
inside an SSH session to the NAS looks exactly like a dead agent even when it is
perfectly healthy. Check `docker ps` / `docker logs` on the NAS, or curl
`http://192.168.1.44:8780/v1/health` **from the LXC**.

---

## 3 — CT 1057 (`10.100.23.57:3001`): Uptime Kuma

Uptime Kuma **v2.4.0**, systemd unit `uptime-kuma`, database
`/opt/uptime-kuma/data/kuma.db` (**not version controlled — back it up**).

### 3.1 Monitors

- **216 `job:orch-measurement-breaker`** — push type, in group **119
  "host: lancache-orchestrator (1105)"**, bound to notification **3 "Telegram -
  Storage"**. Fed by the orchestrator on a breaker trip and by the daily cron
  heartbeat in §1.2.
- **176–182** — `job:steam-prefill`, `job:steamprefill-update`,
  `job:gog-backup`, `job:orch-library-sync`, `job:orch-validation-sweep`,
  `job:orch-epic-prefill`, `job:orch-fetch-manifests`. All push type. These were
  bound to notification 3 on **2026-09-09**; before that they had **no
  notification binding at all** — they turned red on the dashboard and told
  nobody. If you add a monitor, binding it to a notification is not optional.

Verified live 2026-09-10: 176–182 and 216 all carry `notification_id = 3`.

### 3.2 How monitors were created and edited

The UI is fine for one-off edits; bulk changes were made in the DB:

1. `systemctl stop uptime-kuma`
2. Back up the DB first: `/opt/uptime-kuma/data/kuma.db.bak-<reason>-<YYYYMMDD-HHMMSS>`
   — e.g. `kuma.db.bak-prebreaker-20260908-150606` (creating monitor 216) and
   `kuma.db.bak-prebindings-20260909-115410` (binding 176–182).
3. Insert/update rows with `sqlite3` (note: `sqlite3` is **not** on `PATH` on
   this container; Python's `sqlite3` module works, and a read-only inspection
   should open the file with `mode=ro` so a look never becomes a write).
4. `systemctl start uptime-kuma`

Kuma reads **notification bindings** from the DB at send time, so changing only
`monitor_notification` rows needs no restart. Changing a monitor's own
definition does.

---

## 4 — Verify after any change

| # | Check | Command | Expect |
|---|---|---|---|
| 1 | Control plane healthy | `curl -s -H "Authorization: Bearer $T" http://localhost:8765/api/v1/health` on 1105 | `status:ok`, `scheduler_running`, `validator_healthy`, `agent_reachable` all true |
| 2 | Agent container | `docker ps` on the NAS | `orchestrator-agent … Up … (healthy)` |
| 3 | Agent uid | `docker exec orchestrator-agent python -c "import os; print(os.getuid())"` | `0` |
| 4 | Cache buckets | `docker exec orchestrator-agent sh -c 'ls /data/cache/cache \| wc -l'` | `256` |
| 5 | Agent DNS | `docker exec orchestrator-agent getent hosts lancache.steamcontent.com` | `192.168.1.40` |
| 6 | Agent reachable cross-host | `curl -s http://192.168.1.44:8780/v1/health` **from the LXC** | 200 |
| 7 | Manifests preserved | archive `.bin` count did not drop across the recreate | non-decreasing |
| 8 | Breaker heartbeat | Kuma 216 | green within 24 h of the 06:17 cron |
| 9 | Job heartbeats | Kuma 176–182 | green, each bound to notification 3 |
| 10 | Rollback available | `docker images \| grep dpa-pre-` on both hosts | a tag for the image you just replaced |

---

## 5 — Quick reference: setting → failure it prevents

| Host | Setting | Failure it prevents |
|---|---|---|
| NAS | `user: "0:0"` | uid 1000 sees 65/256 buckets → every game reports ~8% cached; cache truth is fabricated (**lost once, 2026-09-08**) |
| NAS | `dns: [192.168.1.40]` | `lancache.steamcontent.com` → real CDN → `LancacheNotFoundException`; Steam prefill dead (**lost once: 98 failed runs / 13 days**) |
| NAS | `mem_limit: 8g` | global OOM kill leaves a zombie container Docker still shows as `Up`; no restart, no signal |
| NAS | explicit `healthcheck :8780/v1/health` | inherited check probes `:8765` → permanently `unhealthy` → a real outage cannot be reported |
| NAS | manifest sync before recreate | the in-container live manifest cache is destroyed; affected games silently go invisible |
| NAS | wrapper pushes its own Kuma heartbeat, exits non-zero | `&& curl …status=up` in cron reported success for 13 days of total failure |
| NAS | `RECENT_MAX=4h` / `RUN_MAX=10h` | a hung pass runs forever, or starves the pass after it |
| NAS | skip counter + email after 3 | a finished-but-hung SteamPrefill holds the lock and skips every tick, silently |
| NAS | `ORCH_LANCACHE_BASE_URL` | Epic validation resolves nothing; looks like an Epic bug, is a config bug |
| LXC | `ORCH_SWEEP_BATCH_SIZE=2` | concurrency above the agent's 2-worker pool only queues, and burns the per-call timeout budget while queued |
| LXC | `/etc/cron.d/orch-breaker-heartbeat` | a push monitor with no `up` is permanently red; with it, red means a real trip **or** ~48 h of a dead LXC/cron |
| LXC | `ORCH_VALIDATION_SWEEP_ENABLED` absent | the sweep stays on at its default; an explicit `false` left behind silently stops all measurement |
| LXC | `ORCH_ALLOWED_SOURCE_IPS` | the API is bound `0.0.0.0`; this is the only thing restricting it (fail-closed at boot) |
| LXC | `dpa-pre-<ref>` tag before every build | no named rollback; `docker load` leaves the old image dangling, recoverable by digest only |
| LXC | `backup-pre-<ref>-<ts>.db` before a migration | no point-in-time DB to return to if a migration is wrong |
| Kuma | monitors bound to notification 3 | a monitor that goes red on the dashboard and tells nobody (**true for 176–182 until 2026-09-09**) |
| Kuma | `kuma.db.bak-<reason>-<ts>` before DB edits | a hand-written SQL mistake takes the whole monitoring config with it |

---

## 6 — Back this up

Nothing in §1–§3 is recoverable from this repository. A host rebuild needs, at
minimum:

- `/root/orch-lxc.env`, `/root/deploy-orchestrator-lxc.sh`,
  `/etc/cron.d/orch-breaker-heartbeat`, and the latest
  `/var/lib/orchestrator/backup-pre-*.db` (LXC 1105)
- `/home/karl/lancache-host/` in full — compose, `agent.env`, `.env`,
  `run-steam-prefill.sh`, `cachedomains/`, `.locks/` — plus `crontab -l` for
  `karl` (NAS)
- `/opt/uptime-kuma/data/kuma.db` (CT 1057)

The env files and the crontab contain credentials. Back them up somewhere
encrypted; never into this repository.
