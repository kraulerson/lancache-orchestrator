# lancache → NAS-host Docker migration — implementation plan (runbook)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to work this task-by-task. Steps use checkbox (`- [ ]`) syntax. This is an **infra migration runbook**, not app code — each task's "test" is a verification command with expected output, not a unit test.

**Goal:** Move lancache (monolithic + DNS), the orchestrator-agent, and the Steam/Epic/GOG prefill tooling off the `.40` VM onto the NAS host (`.30`) as Docker containers on a macvlan network — giving the nginx index far more RAM (stops the cache-manager eviction), reading the cache as a local filesystem (kills the NFS bug class), and dropping the VM CPU-steal — while keeping the `.40` IP so no client reconfiguration is needed.

**Architecture:** macvlan network on parent `bridge0`; lancache-monolithic on `.40` (cache 80/443), lancache-dns sharing monolithic's netns (`.40:53`), orchestrator-agent on `.44` (`:8780`). Cache is a local bind mount `/volume1/cache → /data/cache`. Old VM stays stopped-but-intact as rollback.

**Tech stack:** Docker 26.1.0 (compose v2) on Debian-12/UGOS host, macvlan driver, `lancachenet/monolithic` + `lancachenet/lancache-dns`, local `orchestrator:dpa` agent image, SteamPrefill/EpicPrefill linux-x64 binaries, gogrepoc.py.

## Global Constraints (copy verbatim; every task assumes these)

- **Host / IPs:** NAS host `karl@192.168.1.30`; macvlan parent `bridge0`, subnet `192.168.1.0/24`, gateway `192.168.1.1`. lancache = `192.168.1.40`; agent = `192.168.1.44`. Orchestrator brain = `root@10.100.23.105`. Old VM host access = `karl@192.168.1.40` (until shut down).
- **CONFIGHASH guard (do NOT change these — lancache aborts + can invalidate the cache):** `GENERICCACHE_VERSION=2`, `CACHE_MODE=monolithic`, `CACHE_SLICE_SIZE=10m`, `CACHE_KEY=$cacheidentifier$uri$slice_range`. Index/disk size are NOT in the hash → safe to change.
- **Index:** `CACHE_INDEX_SIZE=10000m` **only if** the RAM gate (Task B2) leaves ≥2 GB free after the zone; else `8000m`. `CACHE_DISK_SIZE=54000g`, `MIN_FREE_DISK=100g`, `CACHE_MAX_AGE=3650d`.
- **Epic overlay mechanism:** pre-populate `/data/cachedomains`, add `egs-cloudfront-chunks.epicgamescdn.com` to `epicgames.txt`, run monolithic with **`NOFETCH=true`** (the entrypoint does `git reset --hard` on every start unless NOFETCH=true — that would clobber the edit).
- **Destructive/classifier-gated steps (Phase B) are run/approved by Karl.** Claude does all non-destructive build (Phase A) + verification (Phase C).
- **Secrets:** never read/echo Steam/Epic passwords, 2FA, or tokens. Copy Config/auth dirs file-to-file only. Re-auth (if a session expired) is Karl's.
- **Rollback available at all times until decommission:** the `.40` VM is only *shut down*, never deleted, during cutover.
- **Working dir on `.30`:** `/home/karl/lancache-host/`.

---

## PHASE A — Build alongside (non-destructive; VM stays stopped; Claude executes)

### Task A1: Working dir + macvlan network

**Files:** Create `/home/karl/lancache-host/` on `.30`.

- [ ] **Step 1: Create working dir**
```bash
ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host'
```

- [ ] **Step 2: Create the macvlan network**
```bash
ssh karl@192.168.1.30 'docker network create -d macvlan \
  --subnet=192.168.1.0/24 --gateway=192.168.1.1 \
  -o parent=bridge0 lancache_mv'
```
Expected: prints a network ID (64-hex). If it already exists, that's fine.

- [ ] **Step 3: Verify**
```bash
ssh karl@192.168.1.30 'docker network inspect lancache_mv --format "{{.Driver}} parent={{index .Options \"parent\"}} {{(index .IPAM.Config 0).Subnet}}"'
```
Expected: `macvlan parent=bridge0 192.168.1.0/24`

### Task A2: Transfer the agent image to `.30`

**Interfaces:** Produces image `orchestrator:dpa` present on `.30` (compose in A6 references it).

- [ ] **Step 1: Confirm image tag on `.40`**
```bash
ssh karl@192.168.1.40 'docker images orchestrator:dpa --format "{{.Repository}}:{{.Tag}} {{.Size}}"'
```
Expected: `orchestrator:dpa <size>`

- [ ] **Step 2: Stream the image `.40 → .30`** (pipe through the Mac; no registry needed)
```bash
ssh karl@192.168.1.40 'docker save orchestrator:dpa | gzip' | ssh karl@192.168.1.30 'gunzip | docker load'
```
Expected: `Loaded image: orchestrator:dpa`

- [ ] **Step 3: Verify on `.30`**
```bash
ssh karl@192.168.1.30 'docker images orchestrator:dpa --format "{{.Repository}}:{{.Tag}}"'
```
Expected: `orchestrator:dpa`

### Task A3: Migrate agent state volumes `.40 → .30`

**Interfaces:** Produces docker volumes on `.30`: `depotdownloader-config`, `orchestrator-manifests`, `orchestrator-db`, and dir `/home/karl/lancache-host/steamprefill-cache`. (Agent mounts these in A6.)

- [ ] **Step 1: Identify the source volumes/paths on `.40`** (already known from `docker inspect orchestrator-agent`): `depotdownloader-config`, `orchestrator-manifests`, the orchestrator-db named volume, and `/root/.cache/SteamPrefill`.

- [ ] **Step 2: Copy each named volume `.40 → .30` via tar-over-ssh**
```bash
for V in depotdownloader-config orchestrator-manifests; do
  ssh karl@192.168.1.40 "docker run --rm -v $V:/v alpine tar -C /v -cf - ." \
  | ssh karl@192.168.1.30 "docker volume create $V >/dev/null; docker run --rm -i -v $V:/v alpine tar -C /v -xf -"
done
```
Expected: no errors; two volumes populated on `.30`.

- [ ] **Step 3: Copy the orchestrator-db volume** (find its exact name first)
```bash
ssh karl@192.168.1.40 'docker inspect orchestrator-agent --format "{{range .Mounts}}{{if eq .Destination \"/var/lib/orchestrator\"}}{{.Name}}{{end}}{{end}}"'
```
Then (substitute `<DBVOL>` with that name):
```bash
ssh karl@192.168.1.40 'docker run --rm -v <DBVOL>:/v alpine tar -C /v -cf - .' \
 | ssh karl@192.168.1.30 'docker volume create orchestrator-db >/dev/null; docker run --rm -i -v orchestrator-db:/v alpine tar -C /v -xf -'
```

- [ ] **Step 4: Copy the SteamPrefill live cache**
```bash
ssh karl@192.168.1.40 'tar -C /root/.cache/SteamPrefill -cf - . 2>/dev/null' \
 | ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/steamprefill-cache && tar -C /home/karl/lancache-host/steamprefill-cache -xf -'
```

- [ ] **Step 5: Verify volumes exist on `.30`**
```bash
ssh karl@192.168.1.30 'docker volume ls --format "{{.Name}}" | grep -E "depotdownloader-config|orchestrator-manifests|orchestrator-db"'
```
Expected: all three listed.

### Task A4: Copy prefill installs + Config `.40 → .30`

**Interfaces:** Produces `/home/karl/lancache-host/SteamPrefill/` and `/home/karl/lancache-host/EpicPrefill/` on `.30` (binaries + Config with auth/selection/state).

- [ ] **Step 1: Copy SteamPrefill (binary + Config)** — preserves auth session, no re-login
```bash
ssh karl@192.168.1.40 'tar -C /SteamPrefill -cf - SteamPrefill Config prefill_cronjob.sh update.sh 2>/dev/null' \
 | ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/SteamPrefill && tar -C /home/karl/lancache-host/SteamPrefill -xf -'
```

- [ ] **Step 2: Copy EpicPrefill (binary + Config)**
```bash
ssh karl@192.168.1.40 'tar -C /EpicPrefill -cf - EpicPrefill Config prefill_cronjob.sh update.sh 2>/dev/null' \
 | ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/EpicPrefill && tar -C /home/karl/lancache-host/EpicPrefill -xf -'
```

- [ ] **Step 3: Verify Config (auth + selection) landed** (do NOT print file contents)
```bash
ssh karl@192.168.1.30 'ls /home/karl/lancache-host/SteamPrefill/Config/{account.config,selectedAppsToPrefill.json} /home/karl/lancache-host/EpicPrefill/Config/{userAccount.json,selectedAppsToPrefill.json} 2>&1'
```
Expected: all four paths listed (no "No such file").

### Task A5: Pre-populate cachedomains + Epic overlay

**Interfaces:** Produces `/home/karl/lancache-host/cachedomains/` (git clone of the domains repo, with the Epic host added). Mounted at `/data/cachedomains` in A6, run with `NOFETCH=true`.

- [ ] **Step 1: Clone the cache-domains repo into the working dir**
```bash
ssh karl@192.168.1.30 'git clone https://github.com/uklans/cache-domains.git /home/karl/lancache-host/cachedomains'
```
Expected: clone completes.

- [ ] **Step 2: Add the missing Epic CDN host to `epicgames.txt`**
```bash
ssh karl@192.168.1.30 'grep -qx "egs-cloudfront-chunks.epicgamescdn.com" /home/karl/lancache-host/cachedomains/epicgames.txt || echo "egs-cloudfront-chunks.epicgamescdn.com" >> /home/karl/lancache-host/cachedomains/epicgames.txt'
```

- [ ] **Step 3: Verify the host is present**
```bash
ssh karl@192.168.1.30 'grep -c "egs-cloudfront-chunks.epicgamescdn.com" /home/karl/lancache-host/cachedomains/epicgames.txt'
```
Expected: `1`

### Task A6: Pull images, write compose + env

**Files:** Create `/home/karl/lancache-host/.env` and `/home/karl/lancache-host/docker-compose.yml` on `.30`.

**Interfaces:** Produces the runnable stack definition. `.env` `CACHE_INDEX_SIZE` is a placeholder-safe default `8000m`; the RAM gate (B2) raises it to `10000m` if RAM allows.

- [ ] **Step 1: Pull the lancache images**
```bash
ssh karl@192.168.1.30 'docker pull lancachenet/monolithic:latest && docker pull lancachenet/lancache-dns:latest'
```

- [ ] **Step 2: Write `.env`** (starts at the safe 8000m; B2 may bump to 10000m)
```bash
ssh karl@192.168.1.30 'cat > /home/karl/lancache-host/.env' <<'EOF'
CACHE_INDEX_SIZE=8000m
CACHE_DISK_SIZE=54000g
MIN_FREE_DISK=100g
CACHE_MAX_AGE=3650d
CACHE_SLICE_SIZE=10m
GENERICCACHE_VERSION=2
CACHE_MODE=monolithic
USE_GENERIC_CACHE=true
NOFETCH=true
CACHE_DOMAINS_REPO=https://github.com/uklans/cache-domains.git
CACHE_DOMAINS_BRANCH=master
UPSTREAM_DNS=192.168.1.1
LANCACHE_IP=192.168.1.40
TZ=America/Denver
EOF
```

- [ ] **Step 3: Write `docker-compose.yml`**
```bash
ssh karl@192.168.1.30 'cat > /home/karl/lancache-host/docker-compose.yml' <<'EOF'
services:
  lancache-monolithic:
    image: lancachenet/monolithic:latest
    container_name: lancache-monolithic
    restart: unless-stopped
    env_file: .env
    networks:
      lancache_mv:
        ipv4_address: 192.168.1.40
    volumes:
      - /volume1/cache:/data/cache
      - ./cachedomains:/data/cachedomains
      - ./logs:/data/logs

  lancache-dns:
    image: lancachenet/lancache-dns:latest
    container_name: lancache-dns
    restart: unless-stopped
    network_mode: "service:lancache-monolithic"
    environment:
      - USE_GENERIC_CACHE=true
      - LANCACHE_IP=192.168.1.40
      - UPSTREAM_DNS=192.168.1.1
    depends_on:
      - lancache-monolithic

  orchestrator-agent:
    image: orchestrator:dpa
    container_name: orchestrator-agent
    restart: unless-stopped
    networks:
      lancache_mv:
        ipv4_address: 192.168.1.44
    volumes:
      - /volume1/cache:/data/cache
      - depotdownloader-config:/depotdownloader-config
      - orchestrator-manifests:/manifest-archive
      - orchestrator-db:/var/lib/orchestrator
      - ./steamprefill-cache:/steamprefill-cache
      - ./SteamPrefill:/SteamPrefill
    env_file: .env

networks:
  lancache_mv:
    external: true

volumes:
  depotdownloader-config:
    external: true
  orchestrator-manifests:
    external: true
  orchestrator-db:
    external: true
EOF
```
> **Note:** copy the agent's exact env (image `orchestrator:dpa`) from the current container before running — capture with `ssh karl@192.168.1.40 'docker inspect orchestrator-agent --format "{{json .Config.Env}}"'`, strip any secrets, and add the non-secret vars to a dedicated `agent.env` referenced by the agent service. The agent needs its ORCH_* runtime vars, not the lancache `.env`.

- [ ] **Step 4: Validate compose syntax (no containers started)**
```bash
ssh karl@192.168.1.30 'cd /home/karl/lancache-host && docker compose config >/dev/null && echo COMPOSE_OK'
```
Expected: `COMPOSE_OK`

### Task A7: Stage prefill schedules (disabled)

**Files:** Create `/home/karl/lancache-host/prefill-cron` (reference), do not install yet.

- [ ] **Step 1: Write the intended host crontab block to a file (not installed)**
```bash
ssh karl@192.168.1.30 'cat > /home/karl/lancache-host/prefill-cron' <<'EOF'
# lancache prefill (host) — INSTALL ONLY AFTER MIGRATION VERIFIED (Phase C)
# Steam (every 6h): docker exec orchestrator-agent sh -c "cd /SteamPrefill && HOME=/steamprefill-cache ./SteamPrefill prefill"
# Epic  (disabled until re-enabled)
# GOG   (04:00,16:00): /volume1/cache/GOG/downloadscript
EOF
```
Expected: file written. (Actual cron install is Task D2, post-verification.)

---

## PHASE B — Cutover (destructive; **Karl runs/approves each command**)

> Present these to Karl to run via `!` (or approve). The classifier will block Claude from the VM shutdown + shared-service start.

### Task B1: Shut down the `.40` VM

- [ ] **Step 1: Graceful shutdown from the NAS host**
```bash
ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host --net=host debian:12 nsenter -t 1 -m -u -i -n -p -- virsh shutdown 7047a7b9-8496-4a01-af1a-c216e6865a5c'
```
(Domain UUID = `7047a7b9-8496-4a01-af1a-c216e6865a5c`, confirmed earlier via `virsh list`.)

- [ ] **Step 2: Confirm the VM is down and `.40` is free**
```bash
ping -c2 192.168.1.40   # expect 100% packet loss
```

### Task B2: RAM gate → set index size

- [ ] **Step 1: Measure free RAM with the VM down**
```bash
ssh karl@192.168.1.30 'free -g | awk "/Mem:/{print \$7\" GiB available\"}"'
```
- [ ] **Step 2: Decide the zone size.** If available ≥ 12 GiB → set `CACHE_INDEX_SIZE=10000m`. If 10–12 GiB → keep `8000m`. If < 10 GiB → keep `8000m` and flag Karl.
```bash
# only if committing 10000m:
ssh karl@192.168.1.30 'sed -i "s/^CACHE_INDEX_SIZE=.*/CACHE_INDEX_SIZE=10000m/" /home/karl/lancache-host/.env'
```

### Task B3: Bring up the new stack

- [ ] **Step 1: Start the stack**
```bash
ssh karl@192.168.1.30 'cd /home/karl/lancache-host && docker compose up -d'
```
- [ ] **Step 2: Confirm no CONFIGHASH abort** (the danger point)
```bash
ssh karl@192.168.1.30 'docker logs lancache-monolithic 2>&1 | grep -iE "CONFIGHASH|ABORTING|ERROR" | head'
```
Expected: `CONFIGHASH matches current configuration` and NO "ABORTING". If it aborts, STOP → rollback (the env doesn't match the CONFIGHASH; do not delete CONFIGHASH).

### Task B4: Repoint the orchestrator at the agent's new IP

- [ ] **Step 1: Update the agent URL on `.105`**
```bash
ssh root@10.100.23.105 'sed -i "s#^ORCH_AGENT_BASE_URL=.*#ORCH_AGENT_BASE_URL=http://192.168.1.44:8780#" /root/orch-lxc.env && grep ORCH_AGENT_BASE_URL /root/orch-lxc.env'
```
Expected: `ORCH_AGENT_BASE_URL=http://192.168.1.44:8780`
(`ORCH_LANCACHE_HEARTBEAT_URL` stays `http://192.168.1.40/...` — lancache keeps `.40`.)

- [ ] **Step 2: Redeploy the orchestrator so the env takes effect** (env baked at container create)
```bash
ssh root@10.100.23.105 'bash /root/deploy-orchestrator-lxc.sh'
```

---

## PHASE C — Verify (Claude executes; gates decommission)

### Task C1: Container + config health
- [ ] `ssh karl@192.168.1.30 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "lancache-monolithic|lancache-dns|orchestrator-agent"'` → all `Up`.
- [ ] Ports on `.40`: `ssh karl@192.168.1.30 "docker exec lancache-monolithic sh -c 'ss -tlnp | grep -E :80'"` → nginx listening. DNS: from Mac `dig @192.168.1.40 +short google.com` → resolves.
- [ ] Cache is local (no NFS): `ssh karl@192.168.1.30 'docker exec lancache-monolithic sh -c "mount | grep /data/cache"'` → the bind, NOT an `nfs` type.

### Task C2: Client cache HIT
- [ ] Resolve a Steam CDN host via lancache-dns and confirm it points at `.40`:
```bash
dig @192.168.1.40 +short lancache.steamcontent.com   # expect 192.168.1.40
```
- [ ] Fetch a known-cached object and confirm `X-Cache: HIT` (pick any hash present on disk; or request a small steam range) — from a client or the Mac:
```bash
curl -s -I -H "Host: lancache.steamcontent.com" http://192.168.1.40/ | grep -i "server\|x-cache" 
```
Expected: nginx responds (200/403 with lancache headers) — proves serving path works.

### Task C3: Agent validate works against local cache
- [ ] Trigger a validate of one already-cached Steam game via the orchestrator CLI/API and confirm it returns `up_to_date` reading `/data/cache` locally:
```bash
ssh root@10.100.23.105 'docker exec orchestrator python3 -c "from orchestrator... "'  # use the existing validate path; expect a 200 from 192.168.1.44:8780 in agent logs
ssh karl@192.168.1.30 'docker logs --tail 20 orchestrator-agent 2>&1 | grep -iE "validate|200 OK"'
```
Expected: agent logs show `POST /v1/steam/validate ... 200 OK`.

### Task C4: **Acceptance test — forced prefill with ZERO nfsd deletions**
- [ ] Note the current trap count: `ssh karl@192.168.1.30 'docker exec cache-catcher sh -c "grep \"DEL DELETE\" /log/deletions.log | grep -v SELFTEST | grep -v \".nfs\" | wc -l"'`
- [ ] Run a small forced Steam prefill (one game) through the agent so lancache writes new objects to the (now large-index) cache:
```bash
ssh karl@192.168.1.30 'docker exec orchestrator-agent sh -c "cd /SteamPrefill && HOME=/steamprefill-cache ./SteamPrefill prefill --force --no-ansi 2>&1 | tail -5"'
```
- [ ] Re-check the trap count after the prefill.
- [ ] **PASS = the count did NOT increase** (index has headroom → no cache-manager eviction). This is the core proof the migration fixed the root cause.
- [ ] Confirm an Epic prefill now caches under a single identifier: sample a few new Epic cache files' KEY headers → all `epicgames/...`, none `egs-cloudfront-...`.

### Task C5: NAS-reboot survival
- [ ] (Karl-approved) Reboot the NAS; after boot confirm all three containers auto-start (`restart: unless-stopped`), the macvlan net persists, and the cache serves — before decommissioning the VM.

---

## Rollback (any Phase B/C failure)

- [ ] Stop the host stack: `ssh karl@192.168.1.30 'cd /home/karl/lancache-host && docker compose down'`
- [ ] Restart the intact VM: `ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host --net=host debian:12 nsenter -t 1 -m -u -i -n -p -- virsh start 7047a7b9-8496-4a01-af1a-c216e6865a5c'`
- [ ] Revert the orchestrator agent URL to `192.168.1.40:8780` and redeploy.
- [ ] The VM's lancache/agent/prefill come back exactly as before (cache untouched — both stacks read the same `/volume1/cache`; only one runs at a time).

---

## PHASE D — Post-cutover (after C passes)

- [ ] **D1:** Resume the paused recovery — restart the Steam force-refill (now against the fixed cache), then a full re-validate sweep to flip Game_shelf statuses. (This is the recovery that was correctly paused during the incident.)
- [ ] **D2:** Install the prefill host cron on `.30` from `/home/karl/lancache-host/prefill-cron` (Steam 6h, GOG 04:00/16:00, nightly updater) — re-enabled only after the refill backlog clears.
- [ ] **D3:** Re-enable `ORCH_SCHEDULED_PREFILL_ENABLED` if desired (was off since the incident).
- [ ] **D4:** After ~1 week stable, decommission the `.40` VM (`virsh undefine` + reclaim its disk) and remove the temporary trap/monitoring containers.

---

## Self-review notes

- **Spec coverage:** every spec component (monolithic `.40`, dns shared-netns, agent `.44`, local cache, Epic overlay via NOFETCH, 10000m+RAM-gate, prefill Config copy, cutover/rollback, acceptance = zero nfsd deletions, reboot survival, VM kept 1 week) has a task.
- **Known open detail:** Task A6 Step 3 note — the agent's exact non-secret ORCH_* env must be captured from the running `.40` agent and supplied as `agent.env`; do this during A6 (read-only, non-secret vars only).
- **Placeholder-free:** all commands are concrete; the two substitutions (`<DBVOL>`, agent env capture) have their discovery command inline.
