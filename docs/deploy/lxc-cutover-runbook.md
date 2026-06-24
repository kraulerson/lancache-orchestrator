# Re-arch ④ — LXC cutover runbook (operator)

Concrete, copy-paste steps for moving the control plane to the Proxmox LXC. The
code is done + merged (PRs #195–#197) and Phase 0 is **live-verified on the NAS**.
This runbook is the Phase C (§4/§5 of `docs/superpowers/specs/2026-06-23-lxc-move-design.md`)
cutover, run by the controller on the boxes.

**Fill in before starting:**
- `LXC_IP` — the dedicated orchestrator LXC's address on the Game_shelf VLAN (e.g. `10.100.23.NN`).
- `GAMESHELF_IP` — `10.100.23.102` (current).
- `NAS_IP` — `192.168.1.40` (agent + lancache stay here).

> The old NAS orchestrator + DB are kept intact for rollback through a grace period.

---

## C1 — Provision the LXC (Karl: IP + sizing; controller: the rest)

1. Create a dedicated LXC on the Proxmox cluster: `features: nesting=1`, assign `LXC_IP`, modest vCPU/RAM that beats the NAS VM's CPU-steal share. Install Docker.
2. **Reachability check (OQ4-3)** — from the LXC, confirm the agent + lancache on the NAS are reachable:
   ```sh
   curl -sS -o /dev/null -w "agent:%{http_code}\n" http://NAS_IP:8780/v1/health   # expect 200 (auth-exempt liveness)
   curl -sS -o /dev/null -w "lancache:%{http_code}\n" http://NAS_IP/lancache-heartbeat
   ```
   If blocked, add the Proxmox firewall/route rule before continuing.
3. Build/load `orchestrator:dpa` on the LXC (build from the repo, or `docker save | ssh ... docker load` from the NAS).
4. Write **`/root/orch-lxc.env`** (same token as the NAS):
   ```sh
   ORCH_TOKEN=<same as NAS /home/karl/orch.env>
   ORCH_POOL_READERS=4
   ORCH_DB_CACHE_SIZE_KIB=8192
   ORCH_DB_MMAP_SIZE_BYTES=134217728
   ORCH_API_HOST=0.0.0.0
   ORCH_ALLOWED_SOURCE_IPS=GAMESHELF_IP
   ORCH_AGENT_ENABLED=true
   ORCH_AGENT_BASE_URL=http://NAS_IP:8780
   ORCH_LANCACHE_HEARTBEAT_URL=http://NAS_IP/lancache-heartbeat
   ORCH_SCHEDULED_PREFILL_ENABLED=false
   ```
5. Write **`/root/deploy-orchestrator-lxc.sh`** (no cache mount — validate runs on the agent):
   ```sh
   #!/bin/sh
   # Control-plane orchestrator on the Proxmox LXC (re-arch ④). NO lancache cache
   # mount — validation runs on the data-plane agent on the NAS.
   docker rm -f orchestrator 2>/dev/null
   docker run -d --name orchestrator \
     --network host --restart unless-stopped \
     --env-file /root/orch-lxc.env \
     -v orchestrator-data:/var/lib/orchestrator:z \
     --entrypoint sh orchestrator:dpa \
     -c "exec python -m uvicorn orchestrator.api.main:app --host \"\${ORCH_API_HOST:-127.0.0.1}\" --port \"\${ORCH_API_PORT:-8765}\""
   ```

## C2 — Parallel bring-up + dual-reach (old VM still serving Game_shelf)

1. On the **NAS agent**, allow the LXC and recreate (the old orchestrator keeps reaching the agent via loopback — the allowlist no-ops for loopback):
   ```sh
   # /home/karl/orch.env on the NAS:
   ORCH_AGENT_BIND_HOST=0.0.0.0
   ORCH_ALLOWED_SOURCE_IPS=LXC_IP
   sh /home/karl/deploy-agent.sh
   ```
2. Copy a **point-in-time** DB snapshot NAS → LXC volume:
   ```sh
   # On the NAS: copy the live DB out of the volume (WAL-included is fine for a snapshot smoke test)
   docker run --rm -v orchestrator-uat11-data:/d -v /tmp:/out alpine sh -c "cp /d/orchestrator.db* /out/"
   scp /tmp/orchestrator.db* root@LXC_IP:/tmp/
   # On the LXC: seed the volume
   docker run --rm -v orchestrator-data:/d -v /tmp:/in alpine sh -c "cp /in/orchestrator.db* /d/"
   ```
3. Start the LXC orchestrator: `sh /root/deploy-orchestrator-lxc.sh`. Smoke-test it against the **live agent**:
   ```sh
   T=<ORCH_TOKEN>
   curl -s -H "Authorization: Bearer $T" http://localhost:8765/api/v1/health   # status=ok, validator_healthy=true (agent path), agent_reachable=true
   curl -s -X POST -H "Authorization: Bearer $T" http://localhost:8765/api/v1/games/<known_cached_steam_id>/validate   # → job; confirm outcome matches the NAS baseline
   curl -s -X POST -H "Authorization: Bearer $T" http://localhost:8765/api/v1/platforms/steam/library/sync             # enumerates via agent
   curl -s -X POST -H "Authorization: Bearer $T" http://localhost:8765/api/v1/games/<known_id>/prefill                 # ok → auto-validate
   ```
   The old NAS orchestrator stays untouched; do not manual-trigger on it during the window.

### ⛔ GATE C2 — LXC proven against the live agent before the flip

## C3 — Atomic flip (short maintenance window)

1. **Stop** the old NAS orchestrator: `docker stop orchestrator` (on the NAS — WAL checkpoints).
2. **Final DB sync** NAS → LXC (authoritative; the NAS DB is now static):
   ```sh
   docker run --rm -v orchestrator-uat11-data:/d -v /tmp:/out alpine sh -c "cp /d/orchestrator.db* /out/"
   scp /tmp/orchestrator.db* root@LXC_IP:/tmp/
   ssh root@LXC_IP 'docker rm -f orchestrator; docker run --rm -v orchestrator-data:/d -v /tmp:/in alpine sh -c "cp /in/orchestrator.db* /d/"'
   ```
3. **Restart** the LXC orchestrator on the final DB: `ssh root@LXC_IP 'sh /root/deploy-orchestrator-lxc.sh'`.
4. **Game_shelf** → point at the LXC (separate repo; park lancache_orchestrator on a non-main branch first so its branch-safety hook lets the Game_shelf push through):
   ```
   ORCH_API_URL=http://LXC_IP:8765   # in the Game_shelf backend env; redeploy
   ```
5. **Tighten the NAS agent** to LXC-only + nftables (defense-in-depth over the app allowlist):
   ```sh
   # /home/karl/orch.env already has ORCH_ALLOWED_SOURCE_IPS=LXC_IP from C2; ensure agent is on it.
   nft add rule inet filter input tcp dport 8780 ip saddr != LXC_IP drop   # persist via /etc/nftables.conf
   ```

## C4 — Live verification (spec §5 acceptance table)

| Check | Command / how | Pass |
|---|---|---|
| Control plane up | `curl .../api/v1/health` from Game_shelf's LXC | 200, `validator_healthy:true`, `scheduler_running:true` |
| Cross-host validate | validate a known-cached steam game | outcome + counts match the pre-move baseline |
| Prefill round-trip | prefill a game | `ok=True`, auto-validate succeeds |
| library_sync | trigger steam library_sync | games upserted, no error |
| Game_shelf F14–F17 | load the cache page | badges render, no offline banner, filter works |
| Agent allowlist | hit `NAS_IP:8780` from a non-allowlisted host | refused (403 / nft drop); only `LXC_IP` works |
| No data loss | row counts (games, validation_history, jobs, block_list) LXC vs pre-flip snapshot | equal (± window jobs) |

### ⛔ GATE C4 — all rows pass → ④ accepted

## Rollback (any failure post-flip)

1. `ssh root@LXC_IP 'docker stop orchestrator'`
2. On the NAS: `docker start orchestrator` (the old VM/DB were never touched).
3. Game_shelf `ORCH_API_URL` → `http://NAS_IP:8765`; redeploy.
4. (Optional) loosen the NAS agent allowlist back to `GAMESHELF_IP` if the old orchestrator needs the agent via loopback (it does, allowlist no-ops for loopback — no change needed).

Keep the `orchestrator:pre-4` image tag + the old VM for a grace period before reclaiming.
