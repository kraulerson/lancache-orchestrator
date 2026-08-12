# lancache → NAS-host Docker migration — implementation plan (runbook, rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. This is an **infra migration runbook**, not app code — each task's "test" is a verification command with expected output. **Rev 2** incorporates the 2026-08-05 adversarial review (4 blockers + IMP-1–9 + minors).

**Goal:** Move lancache (monolithic + DNS), the orchestrator-agent, and Steam/Epic/GOG prefill off the `.40` VM onto the NAS host (`.30`) as Docker containers on a macvlan network — **removing the keys_zone-full eviction trigger at the current ~30.7 M-object library** (the incident's cause), reading the cache as a local filesystem (kills the NFS bug class), and dropping the VM CPU-steal — while keeping the `.40` IP so no client reconfiguration is needed.

**Architecture:** macvlan network on parent `bridge0`; lancache-monolithic on `.40` (cache 80/443), lancache-dns sharing monolithic's netns (`.40:53`) with a host restart-watcher, orchestrator-agent on `.44` (`:8780`) with `dns:` pointed at `.40`. Local cache bind mount `/volume1/cache → /data/cache`. A boot-guard systemd unit recreates the macvlan net + brings the stack up after reboot/firmware events. Old VM stays stopped-but-intact as rollback.

**Tech stack:** Docker 26.1.0 (compose v2) on Debian-12/UGOS host, macvlan driver, digest-pinned `lancachenet/monolithic` + `lancachenet/lancache-dns`, local `orchestrator:dpa` agent image, SteamPrefill/EpicPrefill linux-x64 binaries, gogrepoc.py.

## Global Constraints (copy verbatim; every task assumes these)

- **Host / IPs:** NAS host `karl@192.168.1.30`; macvlan parent `bridge0`, subnet `192.168.1.0/24`, gateway `192.168.1.1`. lancache = `192.168.1.40`; agent = `192.168.1.44`; pre-flight scratch IPs `.45`/`.46`. Orchestrator brain = `root@10.100.23.105`. Old VM host = `karl@192.168.1.40` (until shut down); VM domain UUID `7047a7b9-8496-4a01-af1a-c216e6865a5c`.
- **CONFIGHASH guard — do NOT change:** `GENERICCACHE_VERSION=2`, `CACHE_MODE=monolithic`, `CACHE_SLICE_SIZE=10m`, `CACHE_KEY=$cacheidentifier$uri$slice_range`. **Also preserve `proxy_cache_path levels=2:2`** (NOT in the hash but changing it re-paths every object → 25 TB orphan). **NEVER delete `/volume1/cache/CONFIGHASH`** — the upstream abort message suggests it, but on a genuine key/levels change it orphans the whole cache. On ANY mismatch: STOP → rollback to the VM → escalate to Karl. Do NOT put `CACHE_KEY` in `.env` (the image greps it from static config; env is a no-op).
- **Image pinning:** monolithic + dns pinned to the **running VM's exact digest** (Task A3), never a fresh `:latest`. `orchestrator:dpa` is `docker save`d (can't drift).
- **Index:** `CACHE_INDEX_SIZE=10000m` (Karl's decision, 16 GB host). Keep the RAM gate (B2) as a sanity floor + post-cutover memory monitoring (D3); do NOT auto-fallback to 8000m.
- **Agent DNS (blocker B-1):** the agent service MUST set `dns: ["192.168.1.40"]` — a macvlan container does not inherit the host resolver; without this, prefill resolves CDN domains to the real internet and caches nothing.
- **Agent env (blocker B-2):** the agent uses its OWN `agent.env` (captured, secrets stripped), NEVER the lancache `.env`.
- **VM/host mutual exclusion (IMP-4):** the VM lancache and the host lancache must NEVER run simultaneously against `/volume1/cache`. Before `compose up`, confirm `virsh domstate <uuid>` = `shut off`. Before rollback `virsh start`, confirm `docker compose ps` shows the host stack fully down.
- **Epic overlay:** pre-populate `/data/cachedomains`, add `egs-cloudfront-chunks.epicgamescdn.com` to `epicgames.txt`, run monolithic with `NOFETCH=true` (entrypoint does `git reset --hard` every start otherwise).
- **Destructive/classifier-gated steps** (Phase B VM shutdown/start, Phase-C `--privileged --pid=host` trap execs, rollback `virsh start`): **Karl runs/approves.** Claude does all non-destructive build + verification.
- **Secrets:** never read/echo Steam/Epic passwords, 2FA, tokens. Copy Config/auth dirs file-to-file only. Re-auth (if a session expired) is Karl's.
- **Working dir on `.30`:** `/home/karl/lancache-host/` — confirm it's on `/volume1` (Task A9), not the small system disk.

---

## PHASE A — Build alongside (non-destructive; VM stays stopped; Claude executes)

### Task A1: Working dir + macvlan network + boot-guard
- [ ] Create dir: `ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/logs'`
- [ ] Write the idempotent network creator `/home/karl/lancache-host/create-network.sh`:
```bash
ssh karl@192.168.1.30 'cat > /home/karl/lancache-host/create-network.sh' <<'EOF'
#!/bin/bash
docker network inspect lancache_mv >/dev/null 2>&1 || \
  docker network create -d macvlan --subnet=192.168.1.0/24 --gateway=192.168.1.1 \
    -o parent=bridge0 lancache_mv
EOF
ssh karl@192.168.1.30 'chmod +x /home/karl/lancache-host/create-network.sh && /home/karl/lancache-host/create-network.sh'
```
- [ ] Verify: `ssh karl@192.168.1.30 'docker network inspect lancache_mv --format "{{.Driver}} {{(index .IPAM.Config 0).Subnet}}"'` → `macvlan 192.168.1.0/24`.

### Task A2: macvlan L4 pre-flight — PROVE the network BEFORE any destructive step (IMP-1/IMP-2)
> The only prior proof was a 3-packet ICMP ping. This task proves every load-bearing path on a **throwaway** basis while the VM still runs. **Gate B1 on ALL of these passing.**
- [ ] Stand up two scratch macvlan children answering TCP:80 + UDP/TCP:53, on `.45`/`.46`:
```bash
ssh karl@192.168.1.30 'docker run -d --rm --name mvpre80 --network lancache_mv --ip 192.168.1.45 nginx:alpine; \
  docker run -d --rm --name mvpre53 --network lancache_mv --ip 192.168.1.46 --entrypoint sh nginx:alpine -c "apk add --no-cache dnsmasq >/dev/null 2>&1; dnsmasq -k -a 192.168.1.46 -A /probe.test/1.2.3.4 & sleep 600"'
```
- [ ] **(1) Sustained client TCP past the 300 s FDB-ageing window** (from Mac / a real LAN client): fetch a multi-MB body and hold a connection > 300 s:
```bash
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://192.168.1.45/   # expect 200
# sustained: a >5MB transfer + a 310s keepalive; expect no stall/reset
```
- [ ] **(2) DNS over UDP and TCP** to `.46`: `dig @192.168.1.46 +short a.probe.test` and `dig +tcp @192.168.1.46 +short a.probe.test` → both `1.2.3.4`.
- [ ] **(3) same-host sibling path `.45↔.46`** (models `.44→.40`). **Probe the service each peer actually runs** — `mvpre53` serves DNS only, never HTTP, so an HTTP probe at `.46:80` fails on both a healthy and a broken hairpin and proves nothing. Probe in both directions, each against the right port:
```bash
# .45 (nginx) -> .46 (dnsmasq): DNS over the sibling path
ssh karl@192.168.1.30 'docker exec mvpre80 sh -c "nslookup a.probe.test 192.168.1.46 >/dev/null 2>&1 && echo SIBLING_DNS_OK || echo SIBLING_DNS_FAIL"'
# .46 (dnsmasq) -> .45 (nginx): HTTP over the sibling path
ssh karl@192.168.1.30 'docker exec mvpre53 sh -c "wget -qO- http://192.168.1.45/ >/dev/null 2>&1 && echo SIBLING_HTTP_OK || echo SIBLING_HTTP_FAIL"'
```
  Both must print `_OK`. If either FAILs → macvlan same-host hairpin drop; resolve (reflective-relay / parent=eth0 / ipvlan-L2) BEFORE cutover — do NOT proceed. (`nslookup`/`wget` are busybox applets present in `nginx:alpine`; no extra install needed.)
- [ ] **(4) cross-subnet `.105→.45`**: `ssh root@10.100.23.105 'curl -fsS -m5 http://192.168.1.45/ >/dev/null && echo ROUTED_OK || echo ROUTED_FAIL'`.
- [ ] Tear down: `ssh karl@192.168.1.30 'docker rm -f mvpre80 mvpre53'`.
- [ ] Confirm `.1` gateway DHCP pool does NOT span `.40`–`.59` (reserve `.40`/`.44`) — ask Karl to check the router, else risk a lease collision.

### Task A3: Capture the running images' digests + pull them
- [ ] Capture the VM's exact digests (byte-identical to the proven stack):
```bash
ssh karl@192.168.1.40 'docker inspect lancache_monolithic_1 --format "{{.Image}}"; docker inspect lancache_monolithic_1 --format "{{index .Config.Image}}"; docker image inspect $(docker inspect lancache_dns_1 --format "{{.Image}}") --format "{{index .RepoDigests 0}}"'
```
- [ ] Record the two `@sha256:...` digests; pull them on `.30`:
```bash
ssh karl@192.168.1.30 'docker pull lancachenet/monolithic@sha256:<MONO_DIGEST> && docker pull lancachenet/lancache-dns@sha256:<DNS_DIGEST>'
```
- [ ] Use these digest refs in the compose (A8). If the VM image was itself `:latest` with no RepoDigest, tag the loaded image explicitly and reference by local tag.

### Task A4: Transfer the agent image `.40 → .30`
- [ ] `ssh karl@192.168.1.40 'docker save orchestrator:dpa | gzip' | ssh karl@192.168.1.30 'gunzip | docker load'` → `Loaded image: orchestrator:dpa`.

### Task A5: Migrate agent state volumes (with integrity checks — M-1/M-2)
- [ ] Copy each volume with errors visible + integrity verify:
```bash
set -o pipefail
for V in depotdownloader-config orchestrator-manifests; do
  ssh karl@192.168.1.40 "docker run --rm -v $V:/v alpine tar -C /v -cf - ." \
  | ssh karl@192.168.1.30 "docker volume create $V >/dev/null; docker run --rm -i -v $V:/v alpine tar -C /v -xf -"
done
```
- [ ] DB volume (find name, copy, integrity-check — a truncated DB → 25 TB re-download storm):
```bash
DBVOL=$(ssh karl@192.168.1.40 'docker inspect orchestrator-agent --format "{{range .Mounts}}{{if eq .Destination \"/var/lib/orchestrator\"}}{{.Name}}{{end}}{{end}}"')
[ -n "$DBVOL" ] || { echo "DB volume not found — ABORT"; exit 1; }
ssh karl@192.168.1.40 "docker run --rm -v $DBVOL:/v alpine tar -C /v -cf - ." \
 | ssh karl@192.168.1.30 'docker volume create orchestrator-db >/dev/null; docker run --rm -i -v orchestrator-db:/v alpine tar -C /v -xf -'
ssh karl@192.168.1.30 'docker run --rm -v orchestrator-db:/v alpine sh -c "test -s /v/orchestrator.db && echo DB_PRESENT"'
ssh karl@192.168.1.30 'docker run --rm -v orchestrator-db:/v nouchka/sqlite3 /v/orchestrator.db "PRAGMA integrity_check;"'   # expect: ok
```
- [ ] SteamPrefill live cache: `ssh karl@192.168.1.40 'tar -C /root/.cache/SteamPrefill -cf - .' | ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/steamprefill-cache && tar -C /home/karl/lancache-host/steamprefill-cache -xf -'`

### Task A6: Copy prefill installs + Config `.40 → .30`
- [ ] SteamPrefill: `ssh karl@192.168.1.40 'tar -C /SteamPrefill -cf - SteamPrefill Config prefill_cronjob.sh update.sh' | ssh karl@192.168.1.30 'mkdir -p /home/karl/lancache-host/SteamPrefill && tar -C /home/karl/lancache-host/SteamPrefill -xf -'`
- [ ] EpicPrefill: same for `/EpicPrefill`.
- [ ] Verify Config landed (no content print): `ssh karl@192.168.1.30 'ls /home/karl/lancache-host/SteamPrefill/Config/{account.config,selectedAppsToPrefill.json} /home/karl/lancache-host/EpicPrefill/Config/{userAccount.json,selectedAppsToPrefill.json}'`

### Task A7: cachedomains + Epic overlay (M-3)
- [ ] `ssh karl@192.168.1.30 'git clone -b master https://github.com/uklans/cache-domains.git /home/karl/lancache-host/cachedomains'`
- [ ] `ssh karl@192.168.1.30 'grep -qx "egs-cloudfront-chunks.epicgamescdn.com" /home/karl/lancache-host/cachedomains/epicgames.txt || echo egs-cloudfront-chunks.epicgamescdn.com >> /home/karl/lancache-host/cachedomains/epicgames.txt'`
- [ ] Verify present: `grep -c egs-cloudfront-chunks /home/karl/lancache-host/cachedomains/epicgames.txt` → `1`. (Map-gen verification happens in C-phase against the generated `30_maps.conf`.)

### Task A8: Capture agent env, write compose + both env files (B-1, B-2, IMP-5)
- [ ] **Capture agent env, strip secrets → `agent.env`:**
```bash
ssh karl@192.168.1.40 'docker inspect orchestrator-agent --format "{{range .Config.Env}}{{println .}}{{end}}"' \
 | grep -E '^(ORCH_|HOME=|TZ=)' | grep -viE 'PASSWORD|SECRET|2FA|OTP' \
 | ssh karl@192.168.1.30 'cat > /home/karl/lancache-host/agent.env'
```
Then confirm no `ORCH_*_HOST`/`_BIND` hard-codes `.40` (if any bind var exists, set it to `.44`).
- [ ] **What the strip does and does NOT remove.** The `grep -viE` above drops
  `PASSWORD|SECRET|2FA|OTP` only. **`ORCH_TOKEN` is deliberately KEPT** — it is the agent's
  shared-secret for control-plane RPC and the agent will not serve without it. So `agent.env`
  **does** contain a credential. Do not "verify no secret keys are present"; verify instead
  that the *account* secrets are gone and the token survived:
```bash
ssh karl@192.168.1.30 'cd /home/karl/lancache-host && \
  grep -cE "PASSWORD|SECRET|2FA|OTP" agent.env; \
  grep -c "^ORCH_TOKEN=" agent.env'      # expect: 0 then 1
```
- [ ] **Lock the file down — it holds a credential.** `docker inspect`/`env_file` do not care
  about mode, but the file lands world-readable by default under a shared `/home/karl`:
```bash
ssh karl@192.168.1.30 'chmod 600 /home/karl/lancache-host/agent.env && \
  stat -c "%a %n" /home/karl/lancache-host/agent.env'    # expect: 600
```
  Apply the same to `.env` if it ever gains a secret, and note this file in the D4 backup as
  credential-bearing.
- [ ] **Append `ORCH_LANCACHE_BASE_URL=http://192.168.1.40` to `agent.env`.** The default is `http://127.0.0.1`, correct ONLY when the agent is co-located with lancache. On this split topology every Epic chunk silently fails to cache while the job appears to run (see AS-BUILT). Steam is unaffected, which is what makes it look like an Epic bug.
- [ ] Write `/home/karl/lancache-host/.env` (lancache only): `CACHE_INDEX_SIZE=10000m`, `CACHE_DISK_SIZE=54000g`, `MIN_FREE_DISK=100g`, `CACHE_MAX_AGE=3650d`, `CACHE_SLICE_SIZE=10m`, `GENERICCACHE_VERSION=2`, `CACHE_MODE=monolithic`, `USE_GENERIC_CACHE=true`, `NOFETCH=true`, `CACHE_DOMAINS_REPO=https://github.com/uklans/cache-domains.git`, `CACHE_DOMAINS_BRANCH=master`, `UPSTREAM_DNS=192.168.1.41; 192.168.1.42`, `LANCACHE_IP=192.168.1.40`, `TZ=America/Denver`.
- [ ] Write `docker-compose.yml` — monolithic (digest-pinned, `.40`, logging caps), dns (shared netns), agent (`.44`, `dns:["192.168.1.40"]`, `agent.env`, logging caps):
```yaml
services:
  lancache-monolithic:
    image: lancachenet/monolithic@sha256:<MONO_DIGEST>
    container_name: lancache-monolithic
    restart: unless-stopped
    env_file: .env
    networks: { lancache_mv: { ipv4_address: 192.168.1.40 } }
    volumes:
      - /volume1/cache:/data/cache
      - ./cachedomains:/data/cachedomains
      - ./logs:/data/logs
    logging: { driver: json-file, options: { max-size: "50m", max-file: "5" } }
  lancache-dns:
    image: lancachenet/lancache-dns@sha256:<DNS_DIGEST>
    container_name: lancache-dns
    restart: unless-stopped
    network_mode: "service:lancache-monolithic"
    # NOFETCH + the SHARED cachedomains mount are load-bearing. The dns image runs
    # `dnstool generate`, which re-clones uklans/cache-domains into /opt/cache-domains
    # at EVERY start. Without these two lines the container silently ignores the local
    # Epic overlay, so `egs-cloudfront-chunks.epicgamescdn.com` resolves to the real
    # CloudFront and every Epic Launcher download bypasses the cache — while the
    # orchestrator's own Epic prefill keeps working (it targets .40 by IP with a Host
    # header), which is exactly what hides the fault. See AS-BUILT deviation 6.
    environment: [ "USE_GENERIC_CACHE=true", "LANCACHE_IP=192.168.1.40", "UPSTREAM_DNS=192.168.1.41; 192.168.1.42", "NOFETCH=true" ]
    volumes:
      - ./cachedomains:/opt/cache-domains
    depends_on: [ lancache-monolithic ]
    logging: { driver: json-file, options: { max-size: "50m", max-file: "5" } }
  orchestrator-agent:
    image: orchestrator:dpa
    container_name: orchestrator-agent
    restart: unless-stopped
    # REQUIRED — the image's default entrypoint is the CONTROL BRAIN (uvicorn :8765),
    # not the agent. Without this the container comes up healthy-looking and nothing
    # ever answers :8780. See AS-BUILT deviation 1.
    entrypoint: [ "/app/.venv/bin/python", "-m", "orchestrator.agent" ]
    dns: [ "192.168.1.40" ]
    env_file: agent.env
    networks: { lancache_mv: { ipv4_address: 192.168.1.44 } }
    volumes:
      - /volume1/cache:/data/cache
      - depotdownloader-config:/depotdownloader-config
      - orchestrator-manifests:/manifest-archive
      - orchestrator-db:/var/lib/orchestrator
      - ./steamprefill-cache:/steamprefill-cache
      - ./SteamPrefill:/SteamPrefill
    # REQUIRED — the image's inherited healthcheck probes the BRAIN on :8765, which the
    # agent never binds, so the container sits permanently "unhealthy". That is not
    # cosmetic: a health signal that is always red cannot report a real agent outage.
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://127.0.0.1:8780/v1/health').raise_for_status()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    logging: { driver: json-file, options: { max-size: "50m", max-file: "5" } }
  # Standing eviction + mode-000 monitor (D3 part 1). A compose service, NOT a
  # `docker exec` side-process — see C5: the guard must be the container's MAIN
  # process or any restart silently ends alerting.
  cache-catcher:
    image: cache-catcher:guard          # python3 + ca-certificates BAKED IN, not apt-installed live
    container_name: cache-catcher
    restart: unless-stopped
    privileged: true                    # fanotify FAN_MARK_FILESYSTEM
    pid: host                           # resolve culprit pid/comm via /proc
    network_mode: bridge                # needs egress to SMTP; must not claim a macvlan IP
    entrypoint: ["/bin/sh", "/log/entrypoint.sh"]
    volumes:
      - cache-catcher-log:/log
      - /volume1:/volume1
    logging: { driver: json-file, options: { max-size: "50m", max-file: "5" } }
networks: { lancache_mv: { external: true } }
volumes:
  depotdownloader-config: { external: true }
  orchestrator-manifests: { external: true }
  orchestrator-db: { external: true }
  cache-catcher-log: { external: true }
```
- [ ] `ssh karl@192.168.1.30 'cd /home/karl/lancache-host && docker compose config >/dev/null && echo COMPOSE_OK'`

### Task A9: Disk-location + log safety (IMP-5)
- [ ] `ssh karl@192.168.1.30 'df -h /home/karl/lancache-host /volume1'`. If the working dir is on the small system/root partition, relocate it (and bind-mount dirs `logs/`, `cachedomains/`, `SteamPrefill/`, `steamprefill-cache/`) under `/volume1/` and update compose paths. (The monolithic image already rotates `/data/logs` internally; the json-file caps in A8 bound `docker logs`.)

### Task A10: DNS-restart watcher unit (blocker B-3)
> **`docker run` needs `-i` to accept the heredoc.** Without it the container gets an
> immediately-closed stdin, `cat >` writes a **zero-byte unit file**, and `systemctl enable`
> "succeeds" against an empty unit — the failure is silent until the day you need it. Every
> heredoc-fed `docker run` in A10/A11 uses `--rm -i`.

- [ ] Install a systemd unit on `.30` (host) that recovers `lancache-dns` whenever
  `lancache-monolithic` starts. DNS shares monolithic's netns, so:
  - a monolithic **restart** orphans DNS → `docker restart lancache-dns` is enough;
  - a monolithic **recreate** (any `compose up -d` after a config edit) destroys the netns the
    DNS container is joined to → a restart cannot re-attach it and DNS must be **recreated**.
    This is the case that cost ~12 h of dead Steam manifest fetches (AS-BUILT deviation 3), so
    the unit does the recreate form, which is correct for both.
```bash
ssh karl@192.168.1.30 'docker run --rm -i --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "cat > /etc/systemd/system/lancache-dns-watch.service"' <<'EOF'
[Unit]
Description=Recreate lancache-dns when monolithic (re)starts
After=docker.service
Requires=docker.service
[Service]
Restart=always
RestartSec=5
WorkingDirectory=/home/karl/lancache-host
ExecStart=/bin/sh -c 'docker events --filter container=lancache-monolithic --filter event=start --format "{{.Actor.Attributes.name}}" | while read _; do sleep 3; cd /home/karl/lancache-host && docker compose up -d --force-recreate lancache-dns; done'
[Install]
WantedBy=multi-user.target
EOF
ssh karl@192.168.1.30 'docker run --rm -i --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "systemctl daemon-reload && systemctl enable lancache-dns-watch"'
```
- [ ] **Verify the unit is not empty** (the whole point of `-i`):
```bash
ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "wc -c < /etc/systemd/system/lancache-dns-watch.service"'   # expect: >0 (~500)
```
(Enable now; it starts firing once the stack is up in B3.) **This unit is a backstop, not a
guarantee** — D3 part 3's independent `:53` probe from a macvlan sibling is what actually
detects a DNS outage.

### Task A11: Boot-guard unit — survive reboot AND firmware/Docker-state wipe (blocker B-4)
- [ ] Install a systemd unit on `.30` that, after `docker.service`, runs `create-network.sh` then `compose up -d` (so a firmware update that wipes the docker network is self-healing):
```bash
ssh karl@192.168.1.30 'docker run --rm -i --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "cat > /etc/systemd/system/lancache-stack.service"' <<'EOF'
[Unit]
Description=lancache host stack (macvlan net + compose)
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/karl/lancache-host
ExecStart=/bin/sh -c '/home/karl/lancache-host/create-network.sh && docker compose up -d'
ExecStop=/bin/sh -c 'cd /home/karl/lancache-host && docker compose down'
[Install]
WantedBy=multi-user.target
EOF
ssh karl@192.168.1.30 'docker run --rm -i --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "systemctl daemon-reload && systemctl enable lancache-stack"'
```
- [ ] **Verify the unit is not empty** (see A10 on `-i`):
```bash
ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "wc -c < /etc/systemd/system/lancache-stack.service"'   # expect: >0 (~380)
```
- [ ] Verify UGOS's own container manager won't fight this on boot (ask Karl / inspect UGOS docker-app autostart).

### Task A12: Prefill cron artifact + GOG runtime inventory (IMP-6, M-12)
- [ ] Inventory GOG on `.40` BEFORE shutdown: `ssh karl@192.168.1.40 'crontab -l | grep -i gog; head -1 /lancache/lancache/cache/GOG/downloadscript; python3 --version; pip3 freeze 2>/dev/null | grep -iE "requests|html5lib|xml" '` — capture interpreter + deps + cookie/config location.
- [ ] Write `/home/karl/lancache-host/prefill-cron` as a **concrete crontab** (installed in D2, NOT now):
```
# Steam every 6h — through the agent (inherits dns .40). Do NOT inline this with a
# bare `flock -n`: SteamPrefill has been observed to FINISH and then hang forever
# (AS-BUILT deviation 8), and a hung holder would skip every later tick in silence.
# run-steam-prefill.sh keeps the lock, counts skips, and emails after 3 in a row.
0 */6 * * * /home/karl/lancache-host/run-steam-prefill.sh
# Nightly SteamPrefill self-update
0 23 * * * docker exec orchestrator-agent sh -c "cd /SteamPrefill && HOME=/tmp ./update.sh" >> /home/karl/lancache-host/steamprefill-cache/update.log 2>&1
# GOG 04:00,16:00 — see C6. NO --dns 192.168.1.40: a bridge-network container CANNOT
# reach the macvlan lancache at .40 (verified RESOLVE-FAILED), and gogrepoc writes
# installers straight to disk, so lancache interception is neither possible nor wanted.
# Mount path must be /lancache/lancache/cache/GOG — downloadscript hard-codes it and the
# #222 manual-download scanner reads the same tree.
0 4,16 * * * docker run --rm -v /volume1/cache/GOG:/lancache/lancache/cache/GOG -w /lancache/lancache/cache/GOG python:3.12-slim sh -c 'pip install -q html5lib psutil requests && python3 gogrepoc.py update && sh /lancache/lancache/cache/GOG/downloadscript' >> /volume1/cache/GOG/cron.log 2>&1
# NOTE: EpicPrefill prefill_cronjob.sh is copied but NOT installed — Epic prefill is orchestrator-owned (avoid double-prefill).
```
(GOG line finalized in C6. **`karl` cannot install a crontab on UGOS** — `/var/spool/cron` is
denied and there is no passwordless sudo. The operator installs it: `sudo crontab -u karl <file>`.)

---

## PHASE B — Cutover (destructive; **Karl runs/approves**)

### Task B1: Pre-flight gate + shut down the VM (IMP-4)
- [ ] **GATE:** confirm ALL of Task A2's L4 checks passed. If the sibling `.44→.40` path was unproven/failed, do NOT proceed.
- [ ] Shut down the VM: `ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host --net=host debian:12 nsenter -t 1 -m -u -i -n -p -- virsh shutdown 7047a7b9-8496-4a01-af1a-c216e6865a5c'`
- [ ] **Poll to `shut off`** (not just ping): loop `virsh domstate <uuid>` until `shut off` before B2/B3; if graceful stall > ~90 s, escalate to Karl (do NOT `compose up` while the VM may still be flushing/deleting).

### Task B2: RAM gate (default 10000m; sanity only)
- [ ] `ssh karl@192.168.1.30 'free -g'` — record. Keep `CACHE_INDEX_SIZE=10000m` (Karl's decision). Only flag Karl if available RAM is implausibly low (< 11 GiB with VM down), do not auto-change.

### Task B3: Bring up the stack
- [ ] `ssh karl@192.168.1.30 'cd /home/karl/lancache-host && ./create-network.sh && docker compose up -d'`
- [ ] **CONFIGHASH check (danger point):** `docker logs lancache-monolithic 2>&1 | grep -iE "CONFIGHASH|ABORTING|ERROR|Detected:|Current:"`. Expect `CONFIGHASH matches current configuration`, NO `ABORTING`. On mismatch → STOP, rollback, escalate (NEVER delete CONFIGHASH).
- [ ] GARP so LAN forgets the dead VM's MAC (M-4): `ssh karl@192.168.1.30 'docker run --rm --privileged --net=host debian:12 arping -U -c3 -I bridge0 192.168.1.40'` (or from inside the monolithic container). Pause the orchestrator lancache heartbeat + Game_shelf cache-health check for the window.

### Task B4: Repoint the orchestrator (gated on agent reachability — IMP-2)
- [ ] **GATE:** `ssh root@10.100.23.105 'curl -fsS -m5 http://192.168.1.44:8780/v1/health && echo AGENT_REACHABLE'`. Only proceed on success.
- [ ] `ssh root@10.100.23.105 'sed -i "s#^ORCH_AGENT_BASE_URL=.*#ORCH_AGENT_BASE_URL=http://192.168.1.44:8780#" /root/orch-lxc.env && grep ORCH_AGENT_BASE_URL /root/orch-lxc.env'` (heartbeat URL stays `.40`).
- [ ] `ssh root@10.100.23.105 'bash /root/deploy-orchestrator-lxc.sh'`

---

## PHASE C — Verify (Claude; gates decommission)

### Task C1: Health + DNS bind
- [ ] All three `Up`: `docker ps | grep -E "lancache-monolithic|lancache-dns|orchestrator-agent"`.
- [ ] DNS bound on BOTH protocols inside the shared netns: `docker exec lancache-dns sh -c 'ss -lntu | grep :53'` → udp+tcp.
- [ ] Cache is local (not nfs): `docker exec lancache-monolithic sh -c 'mount | grep /data/cache'` → bind, not `nfs`.

### Task C2: Path-continuity HIT + sustained transfer (IMP-3, IMP-1)
- [ ] Epic map generated: `docker exec lancache-monolithic grep -c egs-cloudfront-chunks /etc/nginx/conf.d/30_maps.conf` → ≥1 (ends ` epicgames;`).
- [ ] Pick a known-cached object from `access.log`, compute `md5("steam"+uri+"bytes=0-10485759")` → `H[-2:]/H[-4:-2]/H`, `stat` it on `/volume1/cache/cache/...` (exists, size>0), request that exact URL through the new nginx → assert **`X-Cache: HIT`** and a multi-MB body transfers. **Gate decommission on this HIT.**

### Task C3: Agent validate + DNS resolution
- [ ] `docker exec orchestrator-agent getent hosts lancache.steamcontent.com` → **`192.168.1.40`** (proves B-1 fix).
- [ ] Trigger validate of one cached Steam game via the orchestrator; agent logs show `POST /v1/steam/validate ... 200 OK` reading local `/data/cache`.

### Task C4: ACCEPTANCE — forced prefill WROTE to cache AND zero new evictions (B-1, M-6)
- [ ] Positive-control the trap (local ext4 now, not nfsd): `docker exec lancache-monolithic sh -c 'touch /data/cache/__probe && rm /data/cache/__probe'` → cache-catcher logs the nginx-host-side unlink.
- [ ] Snapshot: trap deletion count; object/byte count under a chosen depot dir on `/volume1/cache`.
- [ ] Run a small forced Steam prefill: `docker exec orchestrator-agent sh -c 'cd /SteamPrefill && HOME=/tmp ./SteamPrefill prefill --force --no-ansi 2>&1 | tail -5'`.
- [ ] **PASS requires ALL:** (a) C3 `getent` returned `.40`; (b) the depot's object/byte count on `/volume1/cache` **INCREASED** (proves it wrote through the cache, not WAN); (c) the trap deletion count did **NOT** increase (proves no eviction); (d) new Epic objects key under `epicgames/...`, none `egs-cloudfront-...`.

### Task C5: Durability drills (blockers B-3, B-4)
> **Every DNS assertion here must be made from a macvlan sibling, never from `.30`.** The NAS
> host cannot reach its own macvlan children, so `dig @192.168.1.40` run on `.30` times out
> whether DNS is healthy or dead (AS-BUILT deviation 4). Define once and reuse:
> `DIGPROBE='docker exec orchestrator-agent getent hosts lancache.steamcontent.com'`
> (the agent is on `.44`, a sibling, with `dns: [192.168.1.40]`).

- [ ] **DNS drill 1 — monolithic RESTART:** `docker restart lancache-monolithic`; wait 15 s; run
  `DIGPROBE` → `192.168.1.40`.
- [ ] **DNS drill 2 — monolithic RECREATE (the case that actually bit us):**
  `docker compose up -d --force-recreate lancache-monolithic`; wait 20 s; run `DIGPROBE` →
  `192.168.1.40`. A watcher that only `docker restart`s DNS **fails this drill** — the netns it
  was joined to no longer exists. This drill is the acceptance test for the A10 recreate fix.
- [ ] **Graceful reboot** (Karl-approved): reboot `.30`; confirm **all four** containers
  (`lancache-monolithic`, `lancache-dns`, `orchestrator-agent`, `cache-catcher`) plus the
  macvlan network auto-start via A11, cache serves, and the fanotify guard is alive
  (`docker exec cache-catcher pgrep -f fanotify_guard`).
- [ ] **Firmware-case (simulated) — `systemctl start` is a NO-OP here; use `restart`.** The unit
  is `Type=oneshot RemainAfterExit=yes`, so after boot it is already `active (exited)` and
  `systemctl start` returns success **without running `ExecStart`** — the drill would pass
  while proving nothing. Force the unit through a real stop/start:
```bash
ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host debian:12 nsenter -t 1 -m -u -i -n -p -- sh -c "
  systemctl is-active lancache-stack;                      # expect: active
  systemctl stop lancache-stack;                            # runs ExecStop (compose down)
  docker network rm lancache_mv || true;                    # simulate the firmware wipe
  systemctl is-active lancache-stack;                       # expect: inactive
  systemctl start lancache-stack;                           # NOW ExecStart really runs
  sleep 20; docker ps --format \"{{.Names}}\" | sort"'
```
  Expect all four container names back and the network recreated. Then re-run `DIGPROBE` and a
  cache HIT.

### Task C6: GOG runtime on `.30` (IMP-6)
> **Correction to the pre-cutover assumption.** This task originally said to run GOG "inside a
> `python:3.12` container with `--dns 192.168.1.40`". That is **impossible on this topology**: a
> bridge-network container cannot reach a macvlan address on the same host (verified
> RESOLVE-FAILED, AS-BUILT deviation 4), and to put the job on the macvlan it would need its own
> reserved IP. It is also **unnecessary** — `gogrepoc.py` downloads full installers straight to
> disk; there is no chunked CDN traffic for lancache to intercept, and the copy on disk *is* the
> cache that Game_shelf's manual-coverage scanner (#222) reads.

- [ ] Reproduce GOG per the A12 inventory in a plain `python:3.12-slim` container on the default
  bridge, with the cookie/config carried in via the mounted GOG dir:
```bash
ssh karl@192.168.1.30 'docker run --rm -v /volume1/cache/GOG:/lancache/lancache/cache/GOG \
  -w /lancache/lancache/cache/GOG python:3.12-slim \
  sh -c "pip install -q html5lib psutil requests && python3 gogrepoc.py update"'
```
- [ ] Confirm it authenticated and wrote a manifest (no credential echoed):
  `ssh karl@192.168.1.30 'ls -la /volume1/cache/GOG/gog-manifest.dat'` → recent mtime, size > 0.
- [ ] Confirm the download target matches the scanner: `downloadscript` writes under
  `/lancache/lancache/cache/GOG/` (**not** a `Game_backup/` subdirectory — that path does not
  exist and was a pre-cutover guess).
- [ ] Finalize the D2 GOG cron line. Do this BEFORE D2 and BEFORE D4 destroys the only
  known-good GOG runtime.

---

## Rollback (any Phase B/C failure) — steps are ORDERED; do not reorder

- [ ] **STEP 1 — disable the boot guard BEFORE anything else.** Otherwise the next boot re-runs
  `compose up`, a macvlan container claims `192.168.1.40` while the VM already holds it, and two
  nginx write one cache tree (duplicate IP; violates the plan's own IMP-4 mutual exclusion).
  `karl` has **no passwordless sudo on UGOS** — `sudo systemctl …` will prompt and fail over
  a non-interactive ssh. Use the same nsenter form as A10/A11:
```bash
ssh karl@192.168.1.30 'docker run --rm --privileged --pid=host debian:12 \
  nsenter -t 1 -m -u -i -n -p -- systemctl disable --now lancache-stack.service'
```
- [ ] **STEP 2 — bring the host stack down, but keep the monitor alive.** A bare
  `docker compose down` now also stops **`cache-catcher`** (it became a compose service in C5),
  which is the only thing watching for the very deletions rollback re-exposes. Stop the lancache
  services by name and leave the catcher running:
```bash
ssh karl@192.168.1.30 'cd /home/karl/lancache-host && \
  docker compose stop lancache-monolithic lancache-dns orchestrator-agent && \
  docker ps --filter name=cache-catcher --format "{{.Names}} {{.Status}}"'   # expect: cache-catcher Up
```
  If you must use `docker compose down`, immediately `docker compose up -d cache-catcher`.
- [ ] **STEP 3 — (Karl-run, classifier-gated)** Restart the intact VM:
  `nsenter … virsh start 7047a7b9-8496-4a01-af1a-c216e6865a5c`. Confirm `domstate` = `running`
  and the VM's lancache serves before declaring rollback complete.
- [ ] **STEP 4** — Revert `ORCH_AGENT_BASE_URL` to `192.168.1.40:8780` on `.105` + redeploy.
- [ ] Cached OBJECTS are shared (the CONFIGHASH-pinned CACHE_KEY makes either stack's objects valid HITs for the other), so no re-download is needed. But rollback is **not risk-free**: the VM reaches the cache over **NFS**, which is how nginx's evictions reached the disk during the incident, and the VM's `keys_zone` is back to `4000m` — the original trigger. Rolling back re-enables the exact failure mode. Treat it as an emergency measure, keep the deletion monitor running throughout (STEP 2), and get off it quickly.

---

## PHASE D — Post-cutover (after C passes)
- [ ] **D1:** Resume the paused recovery — restart the Steam force-refill against the fixed cache, then a full re-validate sweep to flip Game_shelf statuses.
- [x] **D2 (Steam):** DONE 2026-08-10. `karl` cannot install a crontab on UGOS (`/var/spool/cron` denied, no passwordless sudo) — installed by the operator via `sudo crontab -u karl`. **`HOME=/tmp`, NOT `/steamprefill-cache`** (see AS-BUILT). **Superseded by `prefill-cron-v4`** (2026-08-12): the inline job is replaced by `run-steam-prefill.sh`, which makes a stalled prefill email instead of skipping silently. Install still needs the operator.
- [ ] **D2 (GOG):** NOT installed — the documented job cannot work (bridge container + `--dns 192.168.1.40` is blocked by macvlan isolation; target `Game_backup/` does not exist). Tracked in issue #267.
- [x] **D2 (Epic):** DONE 2026-08-10 — Epic is orchestrator-owned via `ORCH_SCHEDULED_PREFILL_ENABLED`, which was **false** and is now **true**. Both halves of the prefill split were off until this date.
- [x] **D3 part 1 (unlink watcher):** DONE — the cache-catcher fanotify guard (see AS-BUILT). Counts only real `DELETE` (unlink) of non-temp files; `MOVED_FROM` is nginx *writing*.
- [ ] **D3 part 2 (key-budget alarm): STILL REQUIRED.** An earlier draft of the AS-BUILT dropped this on the strength of a root-cause retraction that was itself wrong (see **ROOT CAUSE** above). At 4000m the zone held ~32.8M keys and the library was ~30.7M — **94% full**, which is the trigger that caused the incident. At 10000m (~81.9M keys) it is ~42%. Alarm at ~80% of the key budget, i.e. ~65M objects.
- [ ] **D3 part 3 (DNS `:53` probe): STILL REQUIRED.** Not implemented. This is exactly the check that would have caught the ~12h `lancache-dns` orphan recorded in the AS-BUILT.
- [ ] **D4 (IMP-9):** Off-host backup of `/home/karl/lancache-host/` (compose, `.env`, `agent.env`, `cachedomains/` incl. Epic overlay, prefill `Config/`, `create-network.sh`, the two systemd units, **the `cache-catcher/Dockerfile` and the `cache-catcher-log` volume — which holds `alert.env` (SMTP credential), `fanotify_guard.py`, `entrypoint.sh` and the persisted CA bundle; without it a restored stack has NO alerting**, **`run-steam-prefill.sh` + `prefill-cron-v4`**, and **the installed crontab**) to `.105`/Mac. Note `agent.env` is credential-bearing (`ORCH_TOKEN`) and mode `600` — back it up as a secret, not as config. Decommission the VM (`virsh undefine` + reclaim disk) ONLY after: C5 reboot survival PASSED, the firmware-case rebuild-from-backup drill PASSED, the standing monitor (D3) verified firing, the mode-000/index_serv class stayed quiet ≥1 reboot cycle, AND ~1 week floor elapsed. Remove the temporary trap only after D3 is confirmed firing.

---

## Review-coverage map (2026-08-05 adversarial review)
- Blockers: **B-1**→A8(dns)+C3+C4; **B-2**→A8(agent.env); **B-3**→A10+C5; **B-4**→A1+A11+C5.
- Important: **IMP-1**→A2+C2; **IMP-2**→A2+B4-gate; **IMP-3**→A3+C2; **IMP-4**→B1(domstate)+Global; **IMP-5**→A8(logging)+A9; **IMP-6**→A12+C6; **IMP-7**→10000m kept + B2 gate + D3 monitor (Karl's RAM decision); **IMP-8**→D3; **IMP-9**→D4.
- Minors: **M-1/M-2**→A5; **M-3**→A7/C2; **M-4**→B3(GARP)+heartbeat pause; **M-5**→Global(CONFIGHASH); **M-6**→C4(positive-control); **M-7**→Rollback wording (no `:ro` on agent — F18 purge needs RW); **M-8**→C4/D4 (verify UGOS indexing exclusion; do NOT port chmod_fix); **M-9**→CPU contention note (optional `cpus:` cap); **M-10**→IPv6 note; **M-11**→Rollback classifier ownership; **M-12**→A12 crontab.

---

# AS-BUILT (2026-08-06 → 2026-08-10)

Everything above is the plan **as written before cutover**. It is preserved unedited: the
reasoning — including a wrong turn — is the most useful part of the record. This section
records what actually happened. Where the two disagree, **this section is correct**.

## ROOT CAUSE — the plan's original attribution is correct and stands

**Actor: nginx's cache-manager. Trigger: a `keys_zone` at ~94 % of its key capacity.** This is
what the plan and spec said before cutover, and it is what the evidence supports.

**The two independent observations agree.**

| Vantage | Tool | What it saw |
|---|---|---|
| `.40` VM — NFS **client**, sees the originating process | bpftrace | `nginx` unlinking at **~31/s**; ~85 k files gone before the VM was killed |
| `.30` NAS — NFS **server**, sees only its own daemon | fanotify (6 days) | **85,427** cache-file `DELETE`s, all `comm=nfsd` |

`nfsd` is the kernel NFS *server* daemon. It unlinks only because a remote client asked it to,
and the NAS-side watcher sits on the server end of the export, so **every** VM-initiated
deletion — nginx's cache-manager included — is logged as `comm=nfsd` and never as `comm=nginx`.
`nfsd` therefore names the **transport, not the actor**. The client-side bpftrace names the
actor. The two are the same operations seen from opposite ends, and the counts match.

**Two later retractions were wrong and are withdrawn.**

1. A draft read the `comm=nfsd` attribution as proof nginx never evicted. That conflated
   transport with actor.
2. A second draft, correcting the first, over-corrected to "the cause was never established" —
   while the bpftrace result identifying nginx sat in §1 of the spec being edited.

**Evidence-preservation caveat (stated so it is not rediscovered as a contradiction).** The raw
bpftrace output lived on the `.40` VM and was **not copied off before decommissioning**. The
surviving primary artifact is the contemporaneous written record made during the incident with
the VM live (spec §1, committed 2026-08-05), corroborated by the NAS-side fanotify counts. Do
not treat the absence of the raw trace as absence of the finding — but do preserve raw evidence
off-host next time.

**Capacity arithmetic.** nginx sizes its key zone at roughly 128 bytes per key:

| | keys_zone | capacity | objects | utilisation |
|---|---|---|---|---|
| VM, at incident | 4000m | ~32.8 M keys | ~30.7 M | **~94 %** |
| NAS host, now | 10000m | ~81.9 M keys | ~34.4 M | ~42 % |

At ~94 % the zone was effectively full — the condition under which the cache-manager evicts to
reclaim keys — while 29 TB of disk sat free and `max_size`/`min_free`/`inactive` were all far
from their limits. Post-cutover the cache grew *past* the old ~30.7 M ceiling with zero nginx
unlinks, which is what a larger zone predicts. That post-cutover observation is **corroborating,
not decisive** on its own: the zone enlargement and the NFS removal landed in the same change.
The decisive evidence is the client-side trace.

**Corrected acceptance criterion.** The plan's "zero `nfsd` chunk-deletions proves the index no
longer evicts" is a non-sequitur regardless: absence of `nfsd` deletions proves NFS is gone. The
meaningful eviction check is **zero nginx `DELETE` (unlink) events**, distinguished from
`MOVED_FROM` — with `use_temp_path=off` nginx renames `<hash>.NNNN` into place when *writing*,
so `MOVED_FROM` is write traffic. Conflating the two produced a false "cache eviction detected"
alarm on 2026-08-09 during the recovery refill.

**Operational consequence.** Capacity is the live failure mode, so the **key-budget alarm (D3
part 2) must be implemented**, not dropped. Alarm at ~80 % of the key budget (~65 M objects).

## Execution deviations (things the plan did not anticipate)

1. **Agent booted the wrong process.** `orchestrator:dpa`'s default entrypoint is the control
   brain (uvicorn:8765). The agent needs an explicit
   `entrypoint: ["/app/.venv/bin/python", "-m", "orchestrator.agent"]` (binds :8780).
2. **`ORCH_LANCACHE_BASE_URL` — silent total Epic failure.** The Epic byte-puller streams every
   chunk against `settings.lancache_base_url`, default `http://127.0.0.1`. Correct only when
   the agent is co-located with lancache (the old VM, `Net=host`). On the split topology
   (agent `.44`, lancache `.40`) localhost has no lancache, so **every** chunk failed to
   connect: the pull "ran" for hours, `done` climbed, and **nothing cached**. Steam was
   unaffected (SteamPrefill resolves lancache via DNS). Fix: `ORCH_LANCACHE_BASE_URL=http://192.168.1.40`
   in `agent.env`. This is the single highest-impact trap found; nothing in the plan covered it.
3. **The DNS-restart watcher (B-3) did not save us.** Recreating `lancache-monolithic` for an
   unrelated config change orphaned `lancache-dns` for ~12 h; Steam manifest fetches timed out
   the whole time. The unit exists and is enabled — it simply missed this case. Verify `:53`
   answers **from a macvlan sibling** after any monolithic recreate.
4. **macvlan host isolation is real and bit the diagnosis.** The NAS host `.30` cannot reach its
   own macvlan containers, so `dig @192.168.1.40` from `.30` always times out — a false
   negative that briefly looked like a DNS outage. Always test from `.44` (a macvlan sibling)
   or another LAN host.
5. **`--allow-unsafe` matters when regenerating lockfiles**, and `git checkout <file>` restores
   from the *index*, not from `origin/main`.
6. **The Epic overlay never reached DNS — clients bypassed the cache entirely.** (Found
   2026-08-12.) A7 adds `egs-cloudfront-chunks.epicgamescdn.com` to `./cachedomains/epicgames.txt`,
   and A8 mounts that directory into **monolithic only**. `lancache-dns` runs `dnstool generate`,
   which **re-clones** uklans/cache-domains into `/opt/cache-domains` at every start — so the
   overlay domain was absent from DNS and resolved to the real CloudFront
   (`2600:9000:2162:…`) while the control domain `epicgames-download1.akamaized.net` correctly
   returned `192.168.1.40`. nginx would have cached the traffic; DNS never sent it there.
   **The orchestrator's own Epic prefill kept working throughout** — it targets
   `ORCH_LANCACHE_BASE_URL=http://192.168.1.40` by IP with a `Host:` header and never consults
   DNS — which is exactly what hid the fault: prefill green, real Epic Launcher downloads
   uncached. Fix: mount the shared `./cachedomains` into `lancache-dns` at `/opt/cache-domains`
   **and** set `NOFETCH=true` so it stops re-cloning over it. Verified after the fix: the
   overlay domain resolves to `192.168.1.40`, control domains unchanged, and non-cached
   hostnames (e.g. `github.com`) still pass through to upstream.
7. **The agent's healthcheck probed the wrong process, permanently.** (Found 2026-08-12.)
   `orchestrator:dpa` carries the control brain's healthcheck
   (`httpx.get('http://127.0.0.1:8765/api/v1/health')`). Deviation 1 overrides the *entrypoint*
   to run the agent, which binds **:8780** and never :8765 — so every probe failed with
   `ConnectError` and the container read `unhealthy` for 5 days while serving normally. This is
   not cosmetic: a signal that is always red cannot report a real outage. A8 now carries an
   explicit `healthcheck:` on :8780. **Rule: whenever you override `entrypoint:` on an inherited
   image, re-check its `HEALTHCHECK` — it was written for the process you just replaced.**
8. **SteamPrefill can finish and then hang forever.** (Found 2026-08-12.) A `--force` run started
   2026-08-06 was still alive **5 d 18 h** later, having printed `Prefill complete! Prefilled
   1132 apps totaling 10.83 TiB` and `Disconnecting` on 2026-08-08 — completed work, process
   never exited. Consequences: (a) it was reaped only incidentally, by the agent-container
   recreate; (b) it invalidates a bare `flock -n` schedule, because one hung holder silently
   skips every subsequent tick. See "Prefill scheduling" below.
9. **The eviction monitor watched too wide an area and cried wolf.** (Found 2026-08-12.) The
   guard marks the whole filesystem, so GOG's `gogrepoc.py` deleting its own
   `gog-manifest.dat.bak` files tripped the ≥10-deletes/60 s threshold and **emailed a false
   "cache eviction detected" alert on 2026-08-10 20:23**. Over the guard's whole life there have
   been **zero** lancache evictions; all 37 recorded `DELETE`s were GOG's own housekeeping. Fix
   in "D3 as-built" below.

## Outcome

- **Recovery complete:** Epic **654** `up_to_date`, Steam **1138** `up_to_date` (from 450
  partial + 177 failed at the worst point). Residual at 2026-08-12: 15 dead/delisted Epic apps
  (404, blocked); 4 Steam `failed` + 3 `validation_failed` — of which `RPG Maker VX Ace`,
  `RPG Maker XP`, `Half-Life 2: Lost Coast` and `Lossless Scaling` are the known
  no-downloadable-content tools. (Steam also shows ~1363 `not_downloaded`: the library outside
  the prefill selection list, not a regression.)
- **Cache:** ~34.4 M objects / 26 TB of 55 TB, **zero real eviction across the guard's entire
  life** — all 37 recorded `DELETE`s were GOG's own housekeeping (deviation 9). Local ext4, no NFS.
- **D1** done. **D2** — see the corrected D2 entries above: BOTH halves of the prefill split were off until 2026-08-10 (the host cron was never installed AND `ORCH_SCHEDULED_PREFILL_ENABLED` was false). Steam + Epic now live; GOG blocked (issue #267). **D3** part 1 done; parts 2 and 3 still required. **D4** still gated.

## D3 as-built — the standing monitor (and its C5 durability fix)

Implemented as a single fanotify guard in the `cache-catcher` container
(`/log/fanotify_guard.py`), replacing the two earlier single-purpose watchers. It emails on
either failure mode (15-min cooldown per type): **eviction** (≥10 real cache-file *unlinks* in
60 s) and **mode-000** (a non-nginx process making a cache file unreadable by `www-data`).

Two durability defects were found and fixed on 2026-08-09, both of which would have killed
alerting silently:

- The guard ran as a `docker exec` side-process, so **any** container restart lost it.
- The base `cache-catcher:latest` image contained **neither python3 nor ca-certificates** —
  both had been apt-installed into the container's *writable layer*, so a `docker rm` +
  recreate would have destroyed alerting outright (proved: the recreated container
  restart-looped on `exec: python3: not found`).

Now: image `cache-catcher:guard` (python3 + ca-certificates baked in), guard runs as the
container **entrypoint** via `/log/entrypoint.sh` in the persistent volume, and cache-catcher
is a **compose service** (`restart: unless-stopped`) covered by the `lancache-stack` boot
guard. Drilled: `docker rm -f` + recreate → guard auto-started, test email delivered;
`docker restart` → guard auto-started; chmod-000 probe → alert fired + email sent.

### D3 corrections applied 2026-08-12

Three defects in the guard, all found by reading its own output rather than by an alert:

1. **Scope.** `FAN_MARK_FILESYSTEM` watches all of `/volume1`, so *any* deletion counted as
   eviction — which is how GOG's manifest churn emailed a false alarm (deviation 9). The
   eviction counter now counts only real cache objects: under `levels=2:2` an nginx cache file
   is named by its full md5, i.e. `^[0-9a-f]{32}$`. Non-matching deletes are still logged, as
   `DEL-ignored (not a cache object)`, so nothing becomes invisible.
   **The mode-000 detector is deliberately NOT scoped this way** — the incident it exists for
   was a `chmod 000` on the *directory* `/volume1/cache/cache`, whose name is not an md5.
   Verified against the real names that caused the false alarm: `gog-manifest.dat.bak`,
   `gog-resume-manifest.dat.bak`, `fallout_london` → ignored; `162bf4a3…5109` → counted;
   `162bf4a3…5109.0001482060` (write-temp) → ignored; `cache` (the directory) → still watched
   by the ATTRIB path. Positive-controlled live.
2. **Log volume.** Every cache *write* (`MOVED_FROM`, the `use_temp_path=off` rename) was logged
   verbatim into **both** `/log/deletions.log` **and** stdout→`/log/guard.out` — the same content
   twice, 795 MB + 212 MB and growing, plus 583 MB of a retired watcher's output. Writes are now
   aggregated to one `WRITES n in 300s` line; `DELETE`/`ATTRIB` stay verbatim; the entrypoint no
   longer duplicates stdout into a file (docker's json-file driver already caps it at 50m×5).
   1.5 GB reclaimed.
3. **No rotation.** `deletions.log` now rotates at 64 MiB keeping one generation.

The guard also gained a one-shot `--send-alert "<subject>" "<body>"` mode, so other jobs reuse
its configured SMTP path instead of copying the credential handling. Nothing else changed in
the mail path.

### Prefill scheduling as-built (supersedes the v3 crontab)

Deviation 8 (SteamPrefill hangs after completing) makes a bare `flock -n` unsafe: one hung
holder skips every later tick **in silence**, which is the original outage. `run-steam-prefill.sh`
keeps the lock, counts consecutive skips, and **emails after 3 in a row (~18 h)**; it also bounds
the run with `timeout` and alerts separately on that. Staged as `prefill-cron-v4`. Both alert
paths were drilled end-to-end (test mail delivered; lock held artificially → 3 skips → alert
mail delivered).

**Still open before D4:** a NAS reboot drill (needs Karl), the firmware-case rebuild-from-backup
drill, the off-host backup itself, and installing `prefill-cron-v4` (needs the operator:
`sudo crontab -u karl /home/karl/lancache-host/prefill-cron-v4`).
