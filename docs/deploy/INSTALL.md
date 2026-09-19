# Install / rebuild — lancache_orchestrator

**Written:** 2026-09-19, from live inspection of the three running hosts.
**Audience:** you, or whoever is rebuilding this after a host loss.

> **Read this first.** This system was built incrementally in production over
> several months, not installed from a script. This guide is reconstructed from
> the running deployment, and **it is not sufficient to rebuild from zero** —
> §10 lists exactly what is missing and why. It *is* sufficient to rebuild any
> single component, to understand what depends on what, and to know what you
> must have backed up.
>
> Where a fact was read off a live host it is stated plainly. Where it was
> inferred, it says so. **Do not fill a gap in §10 with a plausible guess** — a
> wrong step here costs a 29 TB re-download.

`scripts/verify-install.sh` does **not** verify this deployment. It verifies the
Solo Orchestrator framework scaffolding in the repo. Ignore it here.

---

## 1. Topology

Three hosts. The control plane and the data plane are deliberately separate:
byte-pulling and disk-stat must run *beside* the cache, and the NAS is 4-core
and IO-bound, so keeping the API and scheduler there made the control plane
hostage to disk contention.

| host | address | runs |
|---|---|---|
| Proxmox **LXC 1105** | `10.100.23.105` | `orchestrator` — API, scheduler, jobs, SQLite |
| **NAS** (UGREEN DXP4800) | `192.168.1.30` | `lancache-monolithic`, `lancache-dns`, `orchestrator-agent`, `cache-catcher` |
| **CT 1057** | `10.100.23.57:3001` | Uptime Kuma (bare npm + systemd, *not* Docker) |

lancache itself holds `192.168.1.40` and the agent `192.168.1.44`, both via a
macvlan on the NAS — they are distinct LAN addresses, not published ports.

## 2. Prerequisites

Verified on the live hosts:

| | LXC 1105 | NAS | CT 1057 |
|---|---|---|---|
| OS | Debian 13 (trixie) | Debian 13 (trixie), OMV-rebuilt 2026-09-01 | Debian 13 (trixie) |
| Docker | 29.6.0 | 29.7.2, Compose plugin v5.5.0 | not used |

The LXC needs `nesting=1` to run Docker (from the cutover runbook; not
re-verified live). Application Python is 3.12 inside the image —
`pyproject.toml` requires `>=3.12` and `requirements.txt` is pip-compiled for
it. Host Python versions are irrelevant to the app.

## 3. Order of operations

Dependencies are real; this order is not cosmetic.

1. **NAS: macvlan network** — everything on the NAS with a LAN IP needs it.
2. **NAS: `lancache-monolithic`** — must exist before the DNS container, which
   shares its network namespace, and before the agent, whose DNS points at it.
3. **NAS: `lancache-dns`.**
4. **Build the orchestrator image** — one image serves both the control plane
   and the agent, differing only by entrypoint.
5. **NAS: `orchestrator-agent`.**
6. **LXC: `orchestrator`** — needs the agent reachable to report healthy, but
   boots degraded rather than crash-looping if it is not.
7. **CT 1057: Uptime Kuma monitors** — before the cron jobs, which push to them.
8. **Cron jobs** on both the LXC and the NAS.
9. **NAS: `cache-catcher`** — independent of everything else; can go last.

## 4. NAS — the lancache stack

Everything lives in `/volume2/@home/karl/lancache-host/`.

> **Note the path.** `live-configuration.md` refers to `/home/karl/lancache-host/`.
> The real location after the OMV rebuild is `/volume2/@home/karl/...`; the
> former is presumed a symlink but that was **not** confirmed. Use the
> `/volume2` path.

**This directory is the single most important artifact in the entire system and
is not version controlled.** `docker-compose.yml` exists nowhere else — not in
this repo, not in any backup that was located. Back it up.

### 4.1 The macvlan

```sh
docker network create -d macvlan \
  --subnet=192.168.1.0/24 --gateway=192.168.1.1 \
  -o parent=enp2s0 lancache_mv
```

Saved as `create-network.sh` in the compose directory; idempotent.

### 4.2 External volumes — create these first

Compose declares them `external: true`, which means **compose will not create
them and `up` fails if they are absent**:

```sh
docker volume create depotdownloader-config
docker volume create orchestrator-manifests
docker volume create orchestrator-db
docker volume create cache-catcher-log
```

### 4.3 `lancache-monolithic`

Image is pinned **by digest**, not tag:
`lancachenet/monolithic@sha256:1b1378211d192a1e84257382b4e0dd184b8d1d2a17f65ccf90c0310b1624904d`

Macvlan static IP `192.168.1.40`. Mounts `/volume1/cache` → `/data/cache`,
plus `./cachedomains` and `./logs`. Configured from `.env` in the compose
directory — these are tuning values, not secrets:

```
CACHE_INDEX_SIZE=10000m     CACHE_DISK_SIZE=54000g    MIN_FREE_DISK=100g
CACHE_MAX_AGE=3650d         CACHE_SLICE_SIZE=10m      GENERICCACHE_VERSION=2
CACHE_MODE=monolithic       USE_GENERIC_CACHE=true    NOFETCH=true
UPSTREAM_DNS=192.168.1.41; 192.168.1.42                LANCACHE_IP=192.168.1.40
CACHE_DOMAINS_REPO=https://github.com/uklans/cache-domains.git
CACHE_DOMAINS_BRANCH=master                            TZ=America/Denver
```

The resulting nginx cache path, read from the running container:

```
proxy_cache_path /data/cache/cache levels=2:2 keys_zone=generic:10000m
  inactive=3650d max_size=54000g min_free=100g
  loader_files=1000 loader_sleep=50ms loader_threshold=300ms use_temp_path=off;
```

`levels=2:2` is where the **256 buckets** figure used throughout this project
comes from.

> **`CACHE_INDEX_SIZE=10000m` is knowingly too large for this host** — it needs
> 9.77 GiB of shared memory on 15.40 GiB of RAM that also carries the agent's
> 8 GiB limit. Tracked as **#346**, deferred deliberately. Do not "fix" it
> during a rebuild without reading that issue: shrinking it re-creates the
> 2026-07-31 key-eviction failure mode unless `CACHE_DISK_SIZE` shrinks to match.

### 4.4 `lancache-dns`

Digest-pinned: `lancachenet/lancache-dns@sha256:e49e70663cb63ab153cbe57c330de959dbfd02aec84219a8cc07592f9a88b772`

`network_mode: "service:lancache-monolithic"` — it shares lancache's network
namespace entirely and has no address of its own. It **must** start after it.

> **`NOFETCH=true` plus the shared `./cachedomains` mount is load-bearing.**
> Without it the DNS container re-clones upstream cache-domains on every start
> and silently drops the local Epic overlay entry
> (`egs-cloudfront-chunks.epicgamescdn.com`), sending Epic traffic past the
> cache. The symptom looks like an Epic problem, not a DNS problem.

## 5. The orchestrator image

One image, `orchestrator:dpa`, **built locally and tagged by hand — not pulled
from any registry.** Built from this repo's `Dockerfile`. The control plane and
the agent run the same image and differ only by entrypoint.

Migrations need no separate step: `.sql` files ship as Python package data under
`src/orchestrator/db/migrations/` and load via `importlib.resources`. The
FastAPI lifespan runs `migrate.run_migrations()` **before** the connection pool
initialises, atomically, with a `CHECKSUMS` manifest cross-checked at boot — a
mismatch raises `MigrationError` and boot fails loudly. 17 migrations as of this
snapshot (`0001`–`0017_sweep_pass.sql`).

## 6. NAS — `orchestrator-agent`

Entrypoint override: `["/app/.venv/bin/python", "-m", "orchestrator.agent"]`.
Macvlan static IP `192.168.1.44`, port 8780.

Four settings are load-bearing. Each has caused a production incident:

| setting | value | why |
|---|---|---|
| `user` | **`"0:0"`** | cache dirs are mode `0700`; as uid 1000 the agent sees 65 of 256 buckets and reports ~8 % cached on *every* game |
| `mem_limit` | **`8g`** | set after an OOM incident |
| `dns` | **`[192.168.1.40]`** | so `lancache.steamcontent.com` resolves to the cache, not the real CDN |
| `healthcheck` | explicit, probing `:8780` | the inherited image healthcheck probes `:8765` and is wrong for the agent |

Mounts: `/volume1/cache` → `/data/cache` (rw); volumes `orchestrator-db`,
`depotdownloader-config`, `orchestrator-manifests` → `/manifest-archive`; binds
`./steamprefill-cache` and `./SteamPrefill`.

> **`orchestrator-manifests` must be synced before every recreate.** SteamPrefill's
> live manifest cache is at `/tmp/.cache/SteamPrefill` *inside* the container and
> is ephemeral. Run the archive sync first or the manifests are gone permanently.

> **The agent is not reachable from the NAS's own shell.** Being on the macvlan,
> `127.0.0.1`, `.30` and `.31` all fail. Check it with `docker ps` / `docker logs`
> on the NAS, or `curl http://192.168.1.44:8780/v1/health` **from the LXC**.

Env from `agent.env` (not version controlled). Names only:
`ORCH_TOKEN`, `ORCH_POOL_READERS`, `ORCH_DB_CACHE_SIZE_KIB`,
`ORCH_DB_MMAP_SIZE_BYTES`, `ORCH_LANCACHE_HEARTBEAT_URL`, `ORCH_API_HOST`,
`ORCH_ALLOWED_SOURCE_IPS`, `ORCH_AGENT_BASE_URL`, `ORCH_SCHEDULED_PREFILL_ENABLED`,
`ORCH_AGENT_ENABLED`, `ORCH_STEAM_USERNAME`, `ORCH_AGENT_BIND_HOST`,
`ORCH_AGENT_BIND_PORT`, `ORCH_LANCACHE_BASE_URL`.

**Env changes need a recreate, not a restart.** Verify after every recreate:

```sh
docker exec orchestrator-agent python -c "import os; print(os.getuid())"   # expect 0
docker exec orchestrator-agent sh -c "ls /data/cache/cache | wc -l"        # expect 256
docker exec orchestrator-agent getent hosts lancache.steamcontent.com      # expect 192.168.1.40
```

All three have failed in production. Run all three.

## 7. LXC 1105 — the control plane

Container `orchestrator`, image `orchestrator:dpa`, `--network host`, restart
`unless-stopped`, user `orchestrator` (uid 1000, baked into the image). No
memory limit. Single mount: volume `orchestrator-data` → `/var/lib/orchestrator`,
which holds `orchestrator.db` (~1.1 GB), `epic_session.json`, and the
`backup-pre-*.db` files.

Env from `/root/orch-lxc.env` (not version controlled). Names only:
`ORCH_TOKEN`, `ORCH_POOL_READERS`, `ORCH_DB_CACHE_SIZE_KIB`,
`ORCH_DB_MMAP_SIZE_BYTES`, `ORCH_LANCACHE_HEARTBEAT_URL`, `ORCH_API_HOST`,
`ORCH_ALLOWED_SOURCE_IPS`, `ORCH_AGENT_BASE_URL`, `ORCH_SCHEDULED_PREFILL_ENABLED`,
`ORCH_AGENT_ENABLED`, `ORCH_SWEEP_BATCH_SIZE`, and the Kuma push URLs
`ORCH_KUMA_PUSH_LIBRARY_SYNC`, `ORCH_KUMA_PUSH_SWEEP`,
`ORCH_KUMA_PUSH_SCHEDULED_PREFILL`, `ORCH_KUMA_PUSH_FETCH_MANIFESTS`,
`ORCH_KUMA_PUSH_MEASUREMENT_BREAKER`, `ORCH_KUMA_PUSH_DISK`.

Deliberately **absent** so code defaults apply: `ORCH_VALIDATION_SWEEP_ENABLED`
(true), `ORCH_MEASUREMENT_BREAKER_THRESHOLD` (25), `ORCH_SWEEP_DEADLINE_MARGIN_SEC`
(1800).

> **`ORCH_TOKEN` is shared verbatim between `orch-lxc.env` and the NAS's
> `agent.env`.** There is no distribution mechanism — generate once, copy to the
> other by hand, or nothing authenticates.

Before any deploy carrying a migration, back the DB up from *inside* the running
container using SQLite's backup API (WAL-safe) to
`/var/lib/orchestrator/backup-pre-<ref>-<UTC-timestamp>.db`.

## 8. Uptime Kuma — CT 1057

Kuma **2.4.0**, a bare `npm` install under systemd unit `uptime-kuma`. **Not a
container.** DB at `/opt/uptime-kuma/data/kuma.db`.

Eleven push monitors in **two** groups:

| id | name | group |
|---|---|---|
| 176 | `job:steam-prefill` | 120 — `host: DXP4800 (lancache NAS)` |
| 177 | `job:steamprefill-update` | 120 |
| 178 | `job:gog-backup` | 120 |
| 179 | `job:orch-library-sync` | 119 — `host: lancache-orchestrator (1105)` |
| 180 | `job:orch-validation-sweep` | 119 |
| 181 | `job:orch-epic-prefill` | 119 |
| 182 | `job:orch-fetch-manifests` | 119 |
| 216 | `job:orch-measurement-breaker` | 119 |
| 218 | `lancache:key-budget` | 119 |
| 219 | `lancache:cache-guard` | 119 |
| 220 | `lancache:cache-eviction` | 119 |

> **Two groups, not one.** `live-configuration.md` §3.1 says everything is in
> group 119. Verified live: 176–178 are in group **120**. That doc is wrong.

**Every monitor must be bound to notification id 3 (`Telegram - Storage`).**
All eleven are. Binding is not optional — 176–182 spent months red on a
dashboard telling nobody, until 2026-09-09.

To edit in bulk: stop the unit, copy `kuma.db` to
`kuma.db.bak-<reason>-<YYYYMMDD-HHMMSS>`, edit with Python's `sqlite3` module
(the `sqlite3` CLI is **not** on `PATH` on this host), restart. Notification
bindings are read at send time and need no restart; monitor definitions do.

Push tokens are the credential. They live only in this DB.

## 9. Cron jobs

**LXC 1105**, in `/etc/cron.d/`:

| file | schedule | does |
|---|---|---|
| `orch-breaker-heartbeat` | `17 6 * * *` | daily `up` to the breaker monitor, so a healthy-and-silent breaker isn't permanently red |
| `orch-disk-heartbeat` | `*/15 * * * *` | root disk %, DOWN at ≥ 85 % |

**NAS**, `crontab -l` for user `karl`:

| schedule | does |
|---|---|
| `0 */6 * * *` | `run-steam-prefill.sh`. `HOME=/tmp` is set **inside the script**, not the crontab, and must match `settings.steam_prefill_live_cache_dir` |
| `0 23 * * *` | SteamPrefill self-update. Needs a busybox `unzip` shim at `/home/karl/bin/unzip`. `flock`-guarded against the prefill run |
| `0 4,16 * * *` | GOG library backup via a throwaway `python:3.12-slim` container. Deliberately **no** `--dns 192.168.1.40` — a bridge container cannot reach the macvlan |

Epic prefill is **not** a cron job — it is orchestrator-owned via
`ORCH_SCHEDULED_PREFILL_ENABLED`, to avoid a double-prefill lock conflict.

> **Every literal `%` in a crontab line must be escaped `\%`.** An unescaped `%`
> is read as a newline and the rest of the command becomes stdin. It installs
> cleanly, reports no error, and never runs. This has already happened once, to
> the disk-heartbeat cron.

## 10. What this guide CANNOT tell you

Honest gaps. A from-zero rebuild stalls at each of these.

1. **`cache-catcher:latest` has no Dockerfile and no build script.** `docker
   history` shows a debuerreotype Debian bookworm rootfs with `/watch.sh` and
   `/poll.sh` added as committed layers — i.e. `docker commit` of a hand-modified
   container. `cache-catcher/Dockerfile` builds `cache-catcher:guard` *FROM* it
   and assumes it exists. **There is no documented way to reproduce this image.**
   Treat the image itself as a backup artifact (`docker save`).
2. **How lancache was originally provisioned on the NAS** — no install script or
   first-run log was located.
3. **How the NAS was provisioned** (OMV rebuild, 2026-09-01) — no disk layout or
   `/volume1` vs `/volume2` rationale found.
4. **How Uptime Kuma was provisioned** — node/npm versions and the systemd unit
   contents were not read.
5. **How Steam credentials are seeded for the orchestrator's own adapter.**
   `README.md` documents `orchestrator-cli auth steam` writing to
   `ORCH_STEAM_SESSION_PATH` (default `/var/lib/orchestrator/steam_session.json`),
   **but no such file exists on either host.** Either it lives elsewhere, or that
   path is not currently active. Do not write a "first login" step from the
   documented flow until this is resolved.
6. **Whether the agent's own `orchestrator.db`** (stale, 172 KB, dated Aug 6) is
   load-bearing or a vestige of the old single-host topology. The authoritative
   DB is the LXC's.
7. **Whether the nftables rule** from the cutover runbook
   (`tcp dport 8780 ip saddr != <LXC IP> drop`) was ever applied and persisted.
   Only the `ORCH_ALLOWED_SOURCE_IPS` application-level allowlist was confirmed.
8. **`patch_guard.py`, `patch_guard_cli.py`, `patch_healthcheck.py`** in the NAS
   compose directory — present, purpose not established, probably one-off
   remediation scripts rather than anything a rebuild runs.

## 11. Back up these, or you cannot rebuild

Nothing below is in version control.

| host | what |
|---|---|
| NAS | the **entire** `/volume2/@home/karl/lancache-host/` tree — `docker-compose.yml` exists nowhere else |
| NAS | `crontab -l` for `karl` (a stale copy sits in `crontab.kuma`) |
| NAS | the `cache-catcher-log` volume — `/log/alert.env`, `/log/keybudget.env`, `entrypoint.sh` |
| NAS | `docker save cache-catcher:latest` — irreproducible, see §10.1 |
| NAS | `SteamPrefill/Config/` — the prefill tool's own auth store |
| LXC | `/root/orch-lxc.env`, `/root/deploy-orchestrator-lxc.sh`, both `/etc/cron.d/orch-*` files |
| LXC | `orchestrator-data` volume — the database and `epic_session.json` |
| CT 1057 | `/opt/uptime-kuma/data/kuma.db` — every monitor, group, push token and notification |

The `cache-catcher` guard's Python files **are** mirrored in this repo under
`tools/cache_catcher/`, but the deployed copies still have to be `docker cp`'d
in by hand; compose does not bake them into the image.
