# Re-arch ④ — Move the control plane to a Proxmox LXC (design)

**Status:** Approved (design), 2026-06-23
**Parent north-star:** `docs/superpowers/specs/2026-06-19-re-architecture-design.md`
**Predecessors (all complete + live):** ① SteamPrefill driver, ② data-plane agent, ③ delete legacy worker.
**Type:** Topology / cutover change. Minimal application code (one health-gate change + two small ④-folded improvements); the bulk is deploy/config + an operator-collaborative cutover.

---

## 0. Goal

Move the orchestrator **control plane** (the FastAPI "brain" + its SQLite DB) off the UGREEN NAS VM onto a dedicated **Proxmox LXC**, while the **data-plane agent** (chunk-puller + cache disk-stat + SteamPrefill runner) stays on the UGREEN. This frees the brain from the 4-vCPU, CPU-steal-bound NAS VM (see `[[project_infra_topology]]`) without hairpinning prefill byte volume across the LAN.

The ②/③ split already made the control plane location-agnostic: prefill, validate, and library enumeration all run on the agent today. ④ is therefore mostly **where it runs and how the two halves talk across hosts**, not new behavior.

## 1. Topology

**Before (everything on the UGREEN NAS, 192.168.1.40):**

```
                 UGREEN NAS 192.168.1.40 (host network)
   ┌──────────────────────────────────────────────────────┐
   │  orchestrator :8765 ──loopback──▶ agent :8780         │
   │  (brain + DB)         127.0.0.1   (puller/disk-stat/   │
   │                                    SteamPrefill)       │
   │  lancache (nginx) :80 ◀──local prefill pulls──┘        │
   └──────────────────────────────────────────────────────┘
        ▲ :8765 (bearer + allowlist 10.100.23.102)
   Game_shelf LXC 10.100.23.102 (Proxmox VLAN)
```

**After (brain → new Proxmox LXC; agent stays on the NAS):**

```
   Proxmox VLAN 10.100.23.0/24           UGREEN NAS 192.168.1.40
   ┌─────────────────────────┐           ┌────────────────────────────┐
   │ orchestrator LXC         │           │ agent :8780                │
   │  10.100.23.X             │──job JSON▶│  (bearer + allowlist =     │
   │  orchestrator :8765      │  (small)  │   ONLY 10.100.23.X)        │
   │  + orchestrator.db       │           │  puller/disk-stat/         │
   │  (Docker, nesting)       │           │  SteamPrefill              │
   └─────────────────────────┘           │  lancache :80 ◀─local pulls│
        ▲ :8765                          └────────────────────────────┘
   Game_shelf LXC 10.100.23.102
```

**Key properties:**

- **No hairpin.** Prefill byte pulls + validate disk-stat stay local to the agent (the whole reason the agent stays on the UGREEN). Only small job-control JSON crosses the LAN (LXC → agent).
- **Two independent bearer + allowlist links**, both reusing already-shipped primitives (`SourceAllowlistMiddleware`, `BearerAuthMiddleware`): (a) Game_shelf → orchestrator `:8765`; (b) orchestrator → agent `:8780` (the allowlist tightens from loopback to the LXC IP).
- **DB co-locates with the brain** on the LXC's local disk — the agent is stateless (no DB/pool).
- **lancache stays put** on the NAS; Game_shelf and the SteamPrefill cron are unaffected by the brain move.

## 2. Decisions (locked)

| Axis | Decision | Rationale |
|---|---|---|
| **Target host** | New **dedicated LXC** on the Proxmox cluster, `10.100.23.X` (exact IP assigned at provisioning) | clean isolation, own resource limits, easy rollback; same VLAN as Game_shelf |
| **DB cutover** | **Migrate** the existing `orchestrator.db` | preserves the games library (~2487 steam + epic), `validation_history` (the cache badges Game_shelf reads), jobs, block-list |
| **Cross-host link security** | **Bearer + source-IP allowlist, plaintext** over the trusted VLAN, + host nftables rule | identical trust model to the live Game_shelf→orchestrator link; zero new code (primitives shipped) |
| **LXC runtime** | **Docker in the LXC** (`features: nesting=1`), the same tested `orchestrator:dpa` image | the image is the proven artifact; the move is "same container, new host + network config" |
| **Cutover** | **Parallel bring-up → atomic flip**, old VM kept for rollback | F14–F17 Game_shelf is live; prove the new endpoint before flipping, keep the old as rollback |

## 3. Per-host configuration changes

**Agent (stays on UGREEN; `deploy-agent.sh`):**

| Change | From → To | Why |
|---|---|---|
| `ORCH_AGENT_BIND_HOST` | `127.0.0.1` → `0.0.0.0` | the LXC must reach it over the LAN |
| `ORCH_ALLOWED_SOURCE_IPS` | (Game_shelf IP) → **`10.100.23.X`** (the LXC) | the allowlist becomes the real gate once off-loopback; the fail-closed boot guard enforces non-empty |
| host nftables | add: `:8780` accept only from `10.100.23.X` | defense-in-depth over the app allowlist (mirrors the documented Game_shelf pattern) |

**Orchestrator (new LXC; its own env file — same `ORCH_TOKEN`, diverged otherwise):**

| Change | From → To | Why |
|---|---|---|
| `ORCH_AGENT_BASE_URL` | `http://127.0.0.1:8780` → `http://192.168.1.40:8780` | agent is now cross-host |
| `ORCH_LANCACHE_HEARTBEAT_URL` | loopback → `http://192.168.1.40/lancache-heartbeat` | lancache stays on the NAS |
| `ORCH_ALLOWED_SOURCE_IPS` | stays `10.100.23.102` (Game_shelf); the LXC's own CLI uses loopback (allowlist no-ops for loopback) | — |
| `orchestrator.db` | migrated file → LXC-local Docker volume | the brain's state moves with it |
| lancache cache mount | **removed** (no `/data/cache` on the LXC) | validate runs on the agent now |

**Game_shelf (separate repo; at cutover):** `ORCH_API_URL` → `http://10.100.23.X:8765`.

### 3a. The one required code change — validator health gate

Today `validator_self_test` stat's the **local** lancache cache path to gate `/health.validator_healthy`. On the LXC there is no cache mount, so it would return unhealthy → `/health` 503s → Game_shelf reads the cache subsystem as offline.

**Fix:** when `agent_enabled`, the control plane's validator-health check **probes the agent's `/health`** (the agent has the cache locally) instead of stat-ing a path it no longer has. Small, well-scoped change to `validator/self_test.py` + the health router, behind the existing `agent_enabled` flag so flag-off behavior is unchanged. **Shipped and verified on the existing NAS orchestrator (Phase 0) before any infra move**, so the LXC reports healthy on day one.

### 3b. Two ④-folded review improvements (deferred here from the 2026-06-23 review)

- **AgentClient connection reuse (HTTP2-1 / HTTPX-2).** `AgentClient` currently builds a fresh `httpx.AsyncClient` per request; acceptable on loopback, wasteful now that every call is a cross-host LAN round-trip. Hold a persistent client (created in the lifespan, closed on shutdown) so the control→agent connection is reused. Optional: enable `http2=True` (the `h2` dep is already installed).
- **Agent → control-plane import decoupling (ARCH-4).** The agent process transitively imports `api.middleware` / `api.main` (and thus the DB pool). Once the agent is the only orchestrator code on the UGREEN, it should not import control-plane DB/router code. Extract the shared bits the agent needs (the LAN-bind detection helper, the two middlewares) into a neutral module both import, so the agent's import graph no longer pulls in `api.main`. (Already-shipped #192 work — the agent lifespan shutdown — stays.)

These two are **in scope for ④** but are independent, test-first changes that can land before the cutover (they don't change behavior on the current co-located deployment).

## 4. Cutover sequence (parallel bring-up + atomic flip)

**Phase 0 — Ship the health-gate code change first (before any infra).**
The §3a change goes out as its own PR, merged, image rebuilt, and verified on the *existing* NAS orchestrator (which still has the cache mount, so both the local-stat and agent-probe health paths can be confirmed there). De-risks the move: the LXC reports healthy on day one. (The §3b improvements may ride along or land as their own PRs — all are no-ops on the current co-located deploy.)

**Phase 1 — Provision the LXC (no cutover; old stays fully live).**
- Create the dedicated LXC on Proxmox (`features: nesting=1`), assign `10.100.23.X`, install Docker.
- Build/load `orchestrator:dpa` on the LXC; write its env file (same `ORCH_TOKEN`; `ORCH_AGENT_BASE_URL=192.168.1.40:8780`, heartbeat → `192.168.1.40`, allowlist `10.100.23.102`, `API_HOST=0.0.0.0`, **no** cache mount).
- Copy a **point-in-time snapshot** of `orchestrator.db` to the LXC volume.

**Phase 2 — Parallel bring-up + dual-reach (test; Game_shelf still on the old endpoint).**
- On the agent (NAS): bind `0.0.0.0` + allowlist `[10.100.23.X]`. The **old orchestrator keeps working via loopback** (the allowlist no-ops for loopback), so both reach the agent during the window.
- Start the LXC orchestrator against the snapshot DB. Smoke-test it end-to-end against the real agent: `/health` healthy (agent-probe), one validate, one library_sync, one prefill — all crossing LXC→agent.
- The old NAS orchestrator still serves Game_shelf throughout. Scheduled-prefill is already OFF on it (`ORCH_SCHEDULED_PREFILL_ENABLED=false`); avoid manual triggers on the old one during the window.

**Phase 3 — Atomic flip (short maintenance window).**
1. **Stop** the old NAS orchestrator (quiesces the DB; WAL checkpointed).
2. **Final DB sync** — rsync the now-static `orchestrator.db` (+ `-wal`/`-shm`) NAS → LXC; this is authoritative.
3. **Restart** the LXC orchestrator against the final DB.
4. **Game_shelf** `ORCH_API_URL` → `http://10.100.23.X:8765`, redeploy.
5. **nftables** rule on the NAS: `:8780` accept only from `10.100.23.X`.

**Phase 4 — Verify live (see §5).**

**Rollback (old VM + DB left intact for a grace period):**
- Pre-flip: trivial — stop the LXC orchestrator; the old one never stopped.
- Post-flip: stop LXC orchestrator → restart old NAS orchestrator → revert Game_shelf `ORCH_API_URL` → `192.168.1.40:8765`. Since the old DB seeded the final sync, rollback loses only LXC-side changes since the flip (minutes). Keep the `orchestrator:pre-4` image + the old VM until ④ is proven stable.

## 5. Live verification (post-cutover acceptance)

The move is accepted only when all pass live:

| Check | How | Pass criteria |
|---|---|---|
| Control plane up on LXC | `GET 10.100.23.X:8765/api/v1/health` from Game_shelf's LXC | 200, `validator_healthy: true` (agent-probe path), scheduler running |
| Cross-host control→agent | trigger one **validate** of a known-cached Steam game | job succeeds; outcome + chunk counts match the pre-move baseline |
| Prefill round-trips | trigger one **prefill** via the agent | `ok=True`, auto-enqueued validate succeeds (full loop crosses LXC→agent→SteamPrefill→lancache) |
| library_sync | trigger a Steam library_sync | enumerates via the agent, games upserted, no error |
| Game_shelf F14–F17 live | load the cache page (now pointing at `10.100.23.X:8765`) | badges render, no offline/degraded banner, cache-status filter works |
| Agent allowlist tightened | from a host NOT on the allowlist, hit `:8780` | refused (403 / nftables drop); only the LXC IP succeeds |
| No data loss | row counts (games, validation_history, jobs, block_list) LXC-DB vs pre-flip snapshot | equal (± jobs created during the window) |

**Rollback trigger:** any of validate / prefill / Game_shelf failing post-flip → execute the §4 rollback.

## 6. Scope boundaries

- **In scope:** the §3a health-gate change; the §3b AgentClient reuse + agent-import decoupling; the LXC provisioning + deploy scripts; the cutover + verification.
- **Out of scope:** any change to prefill/validate/enumerate behavior (already location-agnostic); Game_shelf internals beyond the `ORCH_API_URL` switch; lancache; the SteamPrefill cron.
- **Operator-collaborative (run by the operator/Claude on the boxes, not a subagent):** Phase 1 LXC provisioning, the Phase 3 atomic flip, and Phase 4 live verification (§5). The code changes (§3a, §3b) are normal test-first PRs.

## 7. Open questions

- **OQ4-1:** Exact LXC IP (`10.100.23.X`) — assigned at Phase 1. Drives the agent allowlist, the nftables rule, and Game_shelf's `ORCH_API_URL`.
- **OQ4-2:** LXC resource allocation (vCPU / RAM) — pick at provisioning; the brain is light (FastAPI + SQLite + scheduler), so modest is fine, but it should exceed the current CPU-steal-bound NAS VM share to realize the win.
- **OQ4-3:** Does the Proxmox cluster's firewalling already segment `10.100.23.0/24` from `192.168.1.0/24`, or is the NAS reachable from the VLAN as-is? Confirm the LXC→`192.168.1.40:8780` path is open before Phase 2.
