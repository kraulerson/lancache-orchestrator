# lancache → NAS-host Docker migration — design

**Date:** 2026-08-05
**Status:** Approved (design); implementation plan to follow via writing-plans
**Owner:** Karl (orchestrator) + Claude (execution)

## Problem / motivation

lancache currently runs **inside a VM (`.40`, ~10 GB RAM) hosted on the UGREEN DXP4800 NAS (`.30`, 16 GB)**, reaching the cache over NFS. This nesting is the root cause of two production incidents:

1. **Mass game-deletion (2026-08-04).** nginx's cache-manager was evicting cached game chunks directory-by-directory (bpftrace-confirmed: `nginx` ~31 unlinks/s; ~85 k files deleted before the VM was killed). Root cause: the cache **index** (`proxy_cache_path keys_zone=generic:4000m`) holds ~20–32 M keys, but the library is **~30.7 M objects** (sampled: 468 files/leaf-dir × 65 536 leaf dirs). At ~96 % full, every new object forces LRU eviction of real game data — while 29 T of disk sits free (`max_size=54000g`, `min_free=100g`, `inactive=3650d` all far from their limits; atimes normal). The 4 GB index ceiling exists because the keys_zone is shared memory and the VM only has ~10 GB.
   - Object composition (sampled): **Steam ~52 %**, **Epic ~47 %** (split across two identifiers: `epicgames` ~27 % and a raw-CDN `egs-cloudfront-chunks.epicgamescdn.com` ~20 %), everything else <0.1 %. No junk to prune — Epic is object-dense (tiny ChunksV4 files).

2. **NFS stale-attribute 500s / "mode-000 ghosts" (earlier).** The VM's NFSv3 client served incoherent attribute caches under load, producing phantom `EACCES` → nginx HTTP 500 and false-partials. Local-filesystem access removes this entire class.

Additionally the `.40` VM is chronically **CPU-steal-bound** (qemu contention on the NAS).

**Goal:** move lancache (monolithic + DNS), the orchestrator-agent, and the Steam/Epic/GOG prefill tooling **off the VM and onto the NAS host as Docker containers on a macvlan network**, so we can (a) give the index far more RAM (**remove the keys_zone-full eviction trigger at the current ~30.7 M-object library** — the trigger `max_size`/`inactive` remain far from their limits), (b) read the cache as a local filesystem (kill the NFS bug class), and (c) drop the VM CPU-steal (contention remains, unmeasured) — while keeping the `.40` IP so no client reconfiguration is needed.

## Verified feasibility (live, this session)

- Docker **26.1.0** on `.30`; network drivers include **`macvlan`** + `ipvlan`; `macvlan` kernel module loads clean (kernel 6.12).
- **macvlan reachability proven live:** throwaway container at `192.168.1.241` on a macvlan/`bridge0` net was pinged from a separate host (Mac) 3/3, 0.6 ms, then torn down.
- Host is **x86-64 Intel N100, 4 cores, 15.41 GiB RAM** → the `linux-x64` prefill binaries run natively.
- **Privileged + `pid=host` containers allowed** (`cache-catcher` already runs that way).
- Cache is **local ext4** (`/dev/bcache0` → `/volume1/cache/cache`), directly bind-mountable — no NFS.
- **Docker Hub pull works** from the NAS.
- Prefill **Config dirs are portable** — SteamPrefill (`account.config`, `selectedAppsToPrefill.json`, `successfullyDownloadedDepots.json`), EpicPrefill (`userAccount.json`, `selectedAppsToPrefill.json`, `successfullyDownloadedApps.json`).
- LAN is on **`bridge0`** (`192.168.1.30/24`, gw `.1`, physical slave `eth0`) → macvlan parent = `bridge0`.
- Current lancache: compose network `lancache_default`, publishing **`.40:53` (dns)** and **`.40:80/443` (monolithic)**. Agent runs `Net=host` image `orchestrator:dpa` at `.40:8780`, already bind-mounting `/SteamPrefill`, the SteamPrefill cache, DepotDownloader config, and the manifest archive.
- Free LAN IPs: `.44`–`.59` (`.41/.42/.43` are in use).

## Decisions (confirmed with Karl)

| Decision | Choice |
|---|---|
| Interim cache during prep | **Leave OFF until cutover** (no throwaway VM index bump) |
| Agent placement | **Own macvlan IP `.44`** (isolated from lancache) |
| `CACHE_INDEX_SIZE` | **`10000m`** (~80 M keys; ~2.6× today's library) — subject to the RAM gate below |
| Epic cache-domains fix | **Include** — add `egs-cloudfront-chunks.epicgamescdn.com` to the Epic identifier via a persistent custom-domains overlay |
| Prefill consolidation | Run via the **agent** (already mounts `/SteamPrefill` + caches); GOG script already lives in the cache dir; schedules become host cron on `.30` |
| VM lifecycle | **Stopped, not deleted**, kept ~1 week as rollback after the new stack is proven |

## Target architecture

```
NAS .30 host (VM retired) ──► docker macvlan net  (parent bridge0, 192.168.1.0/24, gw .1)
  ├─ lancache-monolithic   macvlan IP .40   :80 :443   keys_zone 10000m, max_size 54000g
  ├─ lancache-dns          --net=container:lancache-monolithic  → answers .40 :53
  ├─ orchestrator-agent    macvlan IP .44   :8780   (image orchestrator:dpa)
  └─ prefill schedules     host cron on .30 → agent drivers (Steam/Epic) + GOG script
  cache: /volume1/cache → /data/cache  (LOCAL bind mount, no NFS)
```

- **Clients unchanged:** still resolve DNS at `.40:53` and fetch cache at `.40:80/443`.
- **Orchestrator (`.105`):** agent-URL changes `.40:8780 → .44:8780` (cross-host, no macvlan isolation).
- **macvlan intra-net:** agent `.44` ↔ lancache `.40` communicate (same macvlan parent); only the NAS *host* can't reach macvlan IPs directly, which nothing in this design requires.

## Components

### lancache-monolithic (`.40`)
- Image `lancachenet/monolithic:latest`. Macvlan endpoint `.40`.
- Bind: `/volume1/cache → /data/cache`, plus logs volume.
- Env: `CACHE_INDEX_SIZE=10000m`, `CACHE_DISK_SIZE=54000g`, `CACHE_SLICE_SIZE=10m`, `CACHE_MAX_AGE=3650d`, `MIN_FREE_DISK=100g`, `USE_GENERIC_CACHE=true`, `CACHE_DOMAINS_REPO`/`BRANCH` as today, plus the **custom Epic domain overlay** (see below).
- **RAM gate:** with the VM down, measure free RAM; commit `10000m` only if ≥2 GB remains for nginx workers + agent + prefill + page cache; else reduce to `8000m` and flag Karl.

### lancache-dns (shares `.40` netns)
- Image `lancachenet/lancache-dns:latest`, `--net=container:lancache-monolithic` → binds `.40:53`.
- Restart coupling: if monolithic restarts, dns must restart (shares its namespace). Handled via restart ordering / a small supervisor.

### Epic custom-domains overlay
- Add `egs-cloudfront-chunks.epicgamescdn.com` (and any sibling Epic CDN hosts observed) to the `epicgames` identifier, persistently so the auto-pulled uklans repo doesn't clobber it (custom overlay file or a pinned fork). Stops future Epic split-caching. Does **not** retroactively de-dup existing objects (they age out / are absorbed by the bigger index).

### orchestrator-agent (`.44`)
- Image `orchestrator:dpa`. Macvlan endpoint `.44`, publishes `:8780`.
- Mounts as today but cache is now **local**: `/volume1/cache → /data/cache`, DepotDownloader config volume, SteamPrefill cache, manifest archive, orchestrator DB volume, and the prefill install dir.
- Orchestrator config on `.105` updated to reach the agent at `.44:8780`.

### Prefill (Steam/Epic/GOG)
- Copy `/SteamPrefill` and `/EpicPrefill` (binaries + `Config/`, incl. auth sessions + selections + state) to the NAS. **Config copied file-to-file — auth sessions preserved, no re-auth; if a session has expired, the 2FA step is Karl's (never handled by Claude).**
- Prefill runs through the agent's mounts; downloads resolve CDN domains via lancache-dns → `.40` → cache. GOG `gogrepoc.py`/`downloadscript` already in `/volume1/cache/GOG/`; only its schedule moves.
- Schedules recreated as host cron on `.30` (Steam 6 h, GOG 04:00/16:00, nightly updater), initially **disabled** until the migration is verified.

## Cutover & rollback

**Phase A — build alongside (VM still stopped, non-destructive):**
1. Create macvlan network (parent `bridge0`, `192.168.1.0/24`, gw `.1`).
2. Pull images; copy prefill Config to `.30`; stage compose/run definitions + Epic overlay.

**Phase B — cutover (destructive, classifier-gated → Karl runs/approves):**
3. Shut down the `.40` VM (frees RAM, releases the `.40` IP).
4. **RAM gate** (measure free RAM; set zone 10000m or 8000m).
5. Start lancache-monolithic (`.40`) → lancache-dns → agent (`.44`).
6. Point orchestrator (`.105`) at `.44:8780`.

**Phase C — verify:**
7. Client resolves a Steam CDN domain → `.40`; fetch → cache HIT.
8. A validate call via `.44` succeeds against local cache.
9. A small forced prefill caches under a single Epic identifier; **the `.30` deletion trap shows zero `nfsd` chunk-deletions** under prefill load (the acceptance test — proves the index no longer evicts).

**Rollback:** if any verify step fails, stop the host stack and `virsh start` the VM (untouched). Keep the VM stopped-but-present ~1 week before deletion.

## Risks & mitigations

- **RAM pressure at 10000m** → RAM gate at cutover; fallback 8000m; monitor swap/page-cache post-cutover.
- **macvlan host↔container isolation** → nothing in this design needs host→macvlan; agent↔lancache is macvlan↔macvlan (works).
- **DNS/agent coupling to monolithic netns** (dns only) → restart ordering; agent is isolated on `.44` so unaffected.
- **UGOS reboot / persistence** → containers use `restart=unless-stopped`; macvlan net + compose defined on persistent storage; cron on host; verify survives a NAS reboot before decommissioning the VM.
- **Custom Epic domain clobbered by repo pull** → persistent overlay, not an edit to the tracked `epicgames.txt`.
- **Auth/2FA** → Config copied as files; Claude never reads/echoes tokens; re-auth (if needed) is Karl's.

## Review hardening (2026-08-05 adversarial review — 4 blockers + IMP-1–9)

The design is sound and the benefits are real, but the first-draft runbook had defects that would let the migration silently fail its own acceptance test. Material design changes now baked into the plan:

- **Agent DNS (blocker):** a macvlan container does NOT inherit the host resolver — the agent MUST set `dns: ["192.168.1.40"]`, else prefill resolves CDN domains to the real internet and caches nothing. The acceptance test must assert both that the agent resolves to `.40` AND that cache bytes on `/volume1/cache` **increased** (not merely that the deletion trap stayed flat).
- **Agent env (blocker):** the agent runs its own captured `agent.env` (secrets stripped), never the lancache `.env`.
- **DNS-restart watcher (blocker):** because dns shares monolithic's netns, a monolithic restart silently kills `.40:53`. A host systemd watcher (`docker events … event=start → docker restart lancache-dns`) fixes it; verified by a restart drill.
- **Boot-guard network (blocker):** the macvlan net is imperative + `external` → a UGOS firmware update that re-provisions Docker wipes it. A `lancache-stack` systemd unit recreates the net + `compose up` after boot; verified by a firmware-case rebuild drill.
- **Prove the network before cutover:** the only prior proof was a 3-packet ICMP ping on a non-standard macvlan-on-a-Linux-bridge topology. A full L4 pre-flight (sustained TCP 80/443 past the 300 s FDB window, UDP+TCP 53, same-host sibling `.44→.40`, cross-subnet `.105→.44`) runs on throwaway IPs `.45/.46` **before** the destructive VM shutdown, and B4 is gated on `.105→.44:8780` reachability.
- **Digest-pin images:** monolithic + dns pinned to the running VM's exact digest (not a fresh `:latest`), because a `:latest` that changed `levels=2:2`/cache-key would orphan all ~25 TB.
- **VM/host mutual exclusion:** confirm `virsh domstate = shut off` before `compose up`; the two lancache stacks must never run simultaneously against the shared cache.
- **Standing eviction monitor:** replace the one-shot acceptance test with a permanent monitor (nginx-unlink watch on local ext4 + object-count-vs-key-budget alert to uptime-kuma `.57`) before removing the trap; decommission the VM only on firmware-survival evidence + an off-host backup, not a bare timer.
- **Index size:** `10000m` kept per Karl's decision (16 GB host); the honest caveat is that zone RSS grows with key count and competes with page cache — mitigated by post-cutover memory monitoring, not a fallback to 8000m.

## ⚠ Root cause NOT ascertained — read the as-built section before relying on this

This design attributes the mass chunk-deletion to **nginx cache-manager eviction from a full
`keys_zone`**, and derives its acceptance criterion from that. **The cause was never
established.** All 85,427 real deletions were attributed to `comm=nfsd` — but `nfsd` is the NFS
*server* daemon, and the watcher sits on the server side, so it cannot see which process on the
`.40` VM issued the unlink. nginx's own cache-manager evicting would look identical.

What is known: the zone was **~94% full** (~30.7 M objects against a ~32.8 M key capacity at
`4000m`), which is the condition under which nginx evicts; and post-cutover the cache grew past
that ceiling with no deletions — but NFS removal and the zone enlargement landed together, so
that observation cannot separate the two hypotheses.

The acceptance criterion here is also wrong: "zero `nfsd` deletions proves the index no longer
evicts" is a non-sequitur — it proves NFS is gone. The meaningful check is zero nginx `DELETE`
(unlink) events, distinct from `MOVED_FROM` (which is nginx *writing*).

Because capacity remains live, the key-budget alarm is **required**, not optional. See
**"AS-BUILT"** at the end of
`docs/superpowers/plans/2026-08-05-lancache-nas-host-migration.md`.

## Out of scope

- Retroactive de-duplication of the existing `egs-cloudfront` Epic objects (bigger index absorbs them; separate cleanup later if desired).
- Increasing NAS RAM beyond 16 GB (hardware max for DXP4800 non-plus).
- The paused Steam force-refill / full re-validate recovery — resumes **after** the migration is verified (no point refilling into a still-evicting cache).

## Acceptance criteria

- New stack serves clients at `.40` (DNS + cache) with no client changes.
- Orchestrator reaches the agent at `.44`; validate + prefill work against the local cache.
- Under a forced prefill, **zero `nfsd` cache-chunk deletions** in the `.30` trap (eviction resolved).
- Survives a NAS reboot (all containers auto-start, cache intact).
- VM remains available for rollback for ~1 week.
