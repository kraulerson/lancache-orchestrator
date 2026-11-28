# Security Audit — Eviction Re-check (Feature 32)

**Feature:** eviction-recheck. An uncommanded cache eviction makes the orchestrator
re-measure the whole library. It is Feature B of
`docs/superpowers/specs/2026-11-20-new-not-cached-design.md` §3b.

**Modules:**
- `tools/cache_catcher/eviction_events.py` (new): best-effort event append and the mount-status check.
- `tools/cache_catcher/fanotify_guard.py`: `alert()` records sent eviction alerts, and the guard logs the mount status at startup.
- `src/orchestrator/agent/routers/cache_events.py` (new): `GET /v1/cache/eviction-events`.
- `src/orchestrator/clients/agent_client.py`: `eviction_events()`.
- `src/orchestrator/scheduler/eviction_watch.py` (new): `EvictionWatch`.
- `src/orchestrator/jobs/sweep_pass.py`: `restart_pass`; `complete_pass` clears `forced`.
- `src/orchestrator/scheduler/jobs.py`: the sweep cron-dedup promotion.
- `src/orchestrator/db/migrations/0020_sweep_pass_forced.sql`.

**Audit date:** 2026-11-24

**Auditor:** final whole-branch adversarial review (opus, max effort, Senior
Security Engineer persona), followed by the implementer's fix pass. Tools: ruff
(including flake8-bandit `S`), `mypy --strict`, the full suite, the sqlite3 3.51.0
CLI, and off-box probes of the guard with libc stubbed.

**Phase:** 2 (Construction), Build Loop step 2.4.

<!-- Last Updated: 2026-11-24 -->

## Scope

cache-catcher appends one JSON line per sent eviction alert to a dedicated shared
volume. The agent serves it read-only. The control plane polls it, restarts the
sweep pass, and chains sweeps until the pass completes. Karl's N1 applies: nothing
on the NAS calls the orchestrator, and no new credential is introduced.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No security findings. | — |

The review's correctness findings were fixed in the fix pass, each test-first, with
mutation evidence for the alert path:
- **I1:** the pass boundary was at the burst start or on a skewed clock.
- **I2:** monitor 180 was starved during a forced chain.
- **I4:** a missing mount was silent.
- **I5:** the alert-path tests were missing.
- **Ruling 6:** chaining into a tripped breaker.

**I3**, the false DOWNs on monitors 298 and 299 during a forced chain, is a
monitoring decision for Karl. It is not a security issue.

## Non-findings (explicitly checked, clean)

- **Secrets.** The events volume is dedicated, so the agent never sees
  cache-catcher's `/log`, which holds `alert.env` (SMTP) and `keybudget.env` (Kuma
  push URLs).
  - An event line holds only `ts`, `deletes`, `window_s` and `actor`.
  - `actor` comes from the fixed vocabulary in `delete_actor.py`, with no command
    line.
  - `EVENT-WRITE FAILED` logs only the exception type and the path.
- **Authentication and authorisation.** `/v1/cache/eviction-events` is not
  exempt: it sits behind the agent's source-IP allowlist and the bearer check, and
  a test asserts 401.
  - N1 holds: no NAS-to-control-plane call and no new credential.
- **Read-only mount.** The compose mount is `:ro`, and the agent only reads.
- **Injection.** `since` is used only in a Python string comparison. `ts` reaches
  SQL only as a bound parameter, and the boundary expression is static SQL.
- **Input validation.**
  - The agent accepts only `ts` matching `YYYY-MM-DDTHH:MM:SSZ`.
  - An impossible but regex-valid date (for example, month 13) makes `strftime`
    return NULL. `restart_pass` then falls back to LXC now plus the margin, so it
    can neither stall the watch nor violate `NOT NULL`.
  - A future `ts` is clamped to now plus the margin.
- **Denial of service.**
  - The response is capped at the newest 1,000 events.
  - The poll interval is at least 60 s.
  - File growth is bounded by cache-catcher's 15-min alert cooldown, at most 96
    lines a day.
  - Chaining backs off while the latest sweep failed within the breaker window.
  - A forged event needs root on the NAS volume, and its worst case is one extra
    re-check: IO cost, no data loss.
- **Failure isolation (cache-catcher).** The order is email, then Kuma push, then
  the event, and a write failure never raises. Tests cover this with `ctypes.CDLL`
  patched, and mutations reordering or broadening the write were each caught.
- **Single-writer rule.** Cache status is still written only through
  `record_measurement`. `sweep_pass` is not cache truth.

## Decision

**Cleared to advance.** There are no SEV-1, SEV-2, SEV-3 or SEV-4 security
findings.

## Sign-off

- Implementation: uncommitted (commit pending Karl's approval).
- Test suite: 2286 passed, 3 deselected. ruff, format and `mypy --strict` clean.
- No new dependency. One migration (0020). One new NAS volume (`cache-catcher-events`).
