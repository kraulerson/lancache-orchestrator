# ADR-0013: Steam-next Subprocess Isolation Pattern

> **SUPERSEDED (2026-06-22, re-arch ③c).** The ValvePython/steam-next gevent
> subprocess worker described here was **deleted**. Steam prefill, validation,
> and library enumeration now go through the data-plane agent + the host
> SteamPrefill binary (persistent auth); there is no in-orchestrator Steam
> subprocess or gevent dependency anymore. This ADR is kept for historical
> context. See the re-architecture design (`docs/superpowers/specs/2026-06-19-re-architecture-design.md`)
> and the worker-deletion spec (`docs/superpowers/specs/2026-06-21-steam-worker-deletion-design.md`).

**Status:** Superseded by re-arch ③c (was: Accepted, BL10 / F1, 2026-05-24)
**Context:** F1 needs steam-next for Steam authentication + manifest
fetching. steam-next requires `gevent.monkey.patch_minimal()` as the
first import, which globally patches socket/ssl/dns — incompatible with
the orchestrator's asyncio loop.

## Decision

Run steam-next in a **separate Python process** with its own venv. The
orchestrator process communicates via newline-delimited JSON over
stdin/stdout pipes. The subprocess is the ONLY place gevent ever
exists in our deployment.

## Architecture (locked)

1. **Worker venv:** `/opt/orchestrator/venv-steam-worker/` (Dockerfile-
   provisioned in Phase 4). Pinned: `steam[client]==1.4.4`,
   `gevent==24.10.3`, `zstandard==0.23.0`, `httpx==0.28.1`.
2. **Worker entrypoint:** `python -u -m orchestrator.platform.steam.worker`.
   First line of worker.py is `from steam import monkey; monkey.patch_minimal()`.
3. **Orchestrator process:** uses `SteamWorkerClient` (asyncio) — NEVER
   imports `steam`, `gevent`, or any monkey-patched stdlib variant.
4. **IPC protocol:** newline-delimited JSON, 10 MiB line cap (response
   direction only — stdin is trusted), msg_id correlation, 30s per-request
   timeout.
5. **Restart-storm guard:** `Settings.steam_worker_max_restart_attempts`
   is a **budget**, not a deaths-allowed count. The guard fires when
   `_restart_attempts > max` — i.e. with default budget `3`, deaths
   1/2/3 produce warnings + allow respawn, **death 4 fires the guard**.
   This off-by-one vs. the plain reading "max 3 attempts" is intentional
   and tested (see `test_max_restart_attempts_exhausted_marks_disabled`).
   Future-rename candidate: `steam_worker_restart_budget` (deferred —
   would require coordinating the env-var migration across deployment).

## Consequences

- **Pro:** asyncio loop is pristine. steam-next bugs don't crash the
  orchestrator. Restart-storm guard contained to subprocess restarts.
- **Pro:** F2 (Epic) reuses the pattern: subprocess + IPC contract +
  worker venv. Only the worker's internals change.
- **Con:** ~150 LoC of IPC plumbing. One extra process to monitor.
  Dual-venv shape complicates the Dockerfile (Phase 4 concern).
- **Con:** Live Steam validation can't run in CI — manual operator
  validation during UAT-6.

## Alternatives considered

1. **In-process with monkey-patch at orchestrator __init__**: Spike D
   passed in isolation but the long-term risk of any future asyncio
   library breaking under gevent-patched stdlib was deemed unacceptable.
2. **Dedicated thread with own gevent loop**: gevent's monkey-patch is
   process-global, not thread-local — same risk profile as in-process
   with extra thread overhead.

## References

- F1 design spec: docs/superpowers/specs/2026-05-24-f1-steam-credentials-fetcher-design.md
- Spike A (validated steam-next flow): spikes/spike_a_steam_prefill.py
- Spike D (validated gevent+asyncio coexistence): spikes/spike_d_gevent_bridge.py
