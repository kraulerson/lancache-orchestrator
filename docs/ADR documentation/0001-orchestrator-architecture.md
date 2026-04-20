# ADR-0001: Single-Container Monolith with Event-Loop Discipline

**Status:** Accepted
**Date:** 2026-04-20
**Phase:** Phase 1 — Architecture & Technical Planning (Step 1.2)
**Decided by:** Karl (Orchestrator, Light track, personal project)

## Context

The `lancache_orchestrator` is a personal, single-operator service that replaces SteamPrefill/EpicPrefill, validates Lancache state authoritatively via disk-stat, and exposes a REST API consumed by Game_shelf. It runs as a Docker container alongside Lancache on a DXP4800 NAS.

Phase 0 locked these hard constraints (Intake §6.4, Manifesto §2):

- Python 3.12, FastAPI, `aiosqlite` with raw SQL + numbered migrations, no ORM.
- Single-container deployment (Brief §6.2 explicitly rejects multi-container split).
- Env vars + Docker secrets only; no `config.yaml`, no secrets in env vars.
- Docker Compose alongside existing Lancache on the DXP4800.

What remained open for Step 1.2 was **process topology inside the container** and **event-loop discipline under sustained prefill load** — Brief §3.6 flagged Spike F ("API responsiveness during active prefills") as a gating test for the chosen design.

Full evaluation is preserved in `docs/phase-1/architecture-proposal.md`.

## Options Evaluated

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A — Single-container monolith** | One `uvicorn` process runs FastAPI + APScheduler + all adapters + validator. Main asyncio loop owns all async I/O. Dedicated gevent-patched thread isolates `steam-next`. Default `ThreadPoolExecutor` handles disk-stat bursts. | Lowest ops complexity; fastest iteration; smallest debugging surface; matches Intake hard constraints exactly; smallest image (~200 MB). | Event-loop saturation under prefill load is empirically (not architecturally) mitigated — Spike F gates. Gevent/asyncio coexistence is fragile (R10); requires disciplined-executor boundary + lint enforcement. |
| **B — Single container, subprocess-isolated downloader** | Process A hosts FastAPI + APScheduler + validator + DB; Process B spawned per cycle holds the Steam CM session and performs all chunk fan-out. IPC via stdout JSON or Unix domain socket. Same image, multiple processes. | API responsiveness guaranteed by design; gevent isolated to Process B; failure domain isolated (downloader can be SIGKILL'd). | IPC protocol to version + maintain; debugging crosses process boundary; ~500–1500 ms spawn cost per cycle; slower iteration. **Pre-documented fallback from Option A if Spike F fails** (Brief §3.6). |
| **C — Multi-container split (api + worker)** | `orchestrator-api` and `orchestrator-worker` as separate containers; coordination via shared SQLite with WAL or via a broker (Redis/NATS). | Textbook separation of concerns; independent restart; independent scaling knobs. | **Violates Intake §6.4 and Brief §6.2 single-container hard constraint.** Two images, two restart policies, two secret mounts, shared-SQLite WAL contention, zero value at single-operator scale. |

## Decision

**Option A is accepted for MVP** with **Spike F as a hard empirical gate** before Build Milestone B begins (Steam adapter construction in Phase 2).

**Three work zones on a single process**, per Brief §3.6:

1. **Main asyncio event loop** — FastAPI handlers, `httpx.AsyncClient` chunk fan-out with `aiter_raw` + 32-way `asyncio.Semaphore`, `aiosqlite` queries, APScheduler triggers. Everything here awaits I/O or yields promptly.
2. **Dedicated gevent-patched worker thread** — the `steam-next` `SteamClient` runs in a single long-lived thread launched at boot. Gevent monkey-patching is restricted to that thread. Main loop reaches it only via `loop.run_in_executor(steam_thread_pool, fn)`.
3. **Default `ThreadPoolExecutor`** — disk-stat bursts and MD5 cache-key computation (F7 validator, F13 sweep) run via `loop.run_in_executor(None, ...)` in 256-file batches.

**Lint enforcement** at CI: `requests`, `urllib`, `sqlite3`, and bare `time.sleep` imports rejected outside explicit executor wrappers (Ruff custom rule or Semgrep pattern). This backstops the gevent/asyncio discipline and prevents accidental sync I/O on the main loop.

**Spike F pass criteria** (before Phase 2 Milestone B entry):
- Sustain 32-concurrent chunk downloads to Lancache at ≥ 300 Mbps aggregate for 10 minutes on DXP4800 hardware.
- Simultaneously poll `GET /api/v1/health` from a second client every 500 ms.
- Target: **p99 < 100 ms, p50 < 30 ms** on the health endpoint during load.
- Result archived in `docs/test-results/2026-MM-DD_spike-f_pass|fail.md`.

## Rejected Alternatives

**Option B (subprocess-isolated downloader) — rejected for MVP, retained as pre-documented fallback.** Its architectural guarantees are strictly stronger than Option A's, but the operational and iteration costs are real and measurable. If Spike F fails, Option B is the committed fallback; Option A's layered module structure (`api/ adapters/ core/ validator/ db/ cli/`) is deliberately designed so that `adapters/` and chunk fan-out can be hoisted into a subprocess without touching the API or validator code paths — bounding the fallback cost to ~1 week of IPC-protocol work. A superseding ADR-0005 will record the Spike F result and formalize the final A-vs-B decision.

**Option C (multi-container split) — rejected outright.** Violates an explicit hard constraint from the Intake (§6.4) and the Brief (§6.2). Re-introducing it would require an Intake scope-change recorded in `APPROVAL_LOG.md`. There is no scalability driver to justify revisiting this at any point in the MVP or v1.1 roadmap — the system is single-user, LAN-only, single-Lancache.

## Consequences

### What becomes easier

- **Deployment.** One image, one container, `docker compose pull && up -d`. No broker, no sidecar, no coordination layer.
- **Observability.** Single stdout stream captures every log line with correlation IDs propagated through `contextvars`. Docker logging driver alone handles retention.
- **Debugging.** One `py-spy dump` or `strace` target. No cross-process call graphs to reconstruct.
- **Iteration.** Any change redeploys the whole thing atomically. No IPC-protocol versioning.
- **Operator mental model.** "It's one container running alongside Lancache." Matches the Intake's set-it-and-forget-it goal.

### What becomes harder / requires discipline

- **Event-loop hygiene is discipline-bound, not process-enforced.** Any sync `requests.get` or `time.sleep` on the main loop stalls everything. Mitigations: CI lint rule; PR review checklist; Spike F gate; executor-wrapper library helpers to make the right thing easy.
- **Gevent lives in the same process as asyncio.** Risk R10 from Brief §7 applies. Mitigation: restrict gevent monkey-patching to the dedicated Steam thread (`gevent.monkey.patch_minimal()` scope); never call gevent primitives from main loop; dedicated `ThreadPoolExecutor(max_workers=1)` for the Steam session so there's no race on CM state.
- **Scheduler death is process-visible via `/api/v1/health` (503, per JQ3)** but does not automatically restart the scheduler — container restart is the recovery. Status page + Game_shelf Cache dashboard surface this prominently; operator-driven recovery (`docker compose restart orchestrator`) is documented in Phase 4 handoff.
- **Spike F is a load-bearing, time-boxed experiment.** If it fails, Phase 2 does not begin on Option A — the fallback to Option B is triggered immediately, not re-litigated.

### New constraints imposed

- CI must ship with the Ruff/Semgrep custom rule described above before first Phase 2 commit that introduces chunk fan-out.
- `docs/test-results/2026-MM-DD_spike-f_*.md` is a required artifact before closing Build Milestone A (Spikes).
- Any deviation from the 3-work-zone model (e.g., direct sync I/O on the main loop, gevent called outside the dedicated thread) requires a superseding ADR and CI-lint update.
- Sub-ADRs scheduled for Phase 2 to record downstream decisions that this architecture implies: ADR-0002 (steam-next fork policy from OQ4), ADR-0003 (MemoryJobStore rationale from Brief §3.5), ADR-0004 (raw SQL / no ORM from DQ2–DQ8), ADR-0005 (Spike F result — A vs B final commitment), ADR-0006 (vendored legendary strategy), ADR-0007 (Lancache reached via compose service name).

## References

- `docs/phase-1/architecture-proposal.md` — full 3-option evaluation with 10 first-class decisions per option
- `PRODUCT_MANIFESTO.md` §2, §4, Appendix B
- `lancache-orchestrator-brief.md` §3.6, §6, §7 (R10, R22)
- `PROJECT_INTAKE.md` §6.4 (hard constraints)
