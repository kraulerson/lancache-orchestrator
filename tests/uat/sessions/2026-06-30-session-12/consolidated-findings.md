# UAT Session 12 — Consolidated Findings & Triage (2026-06-30)

**Trigger:** test-gate batch 2/2 (features since UAT-11: cache-UI Partial-badge / validator depot-scoping cluster, and force-prefill #203).

**Method:** Two parallel exploratory agents (no human test template — this batch is
backend/CLI, validated by automated tests + live box validation). Plus live
validation of force-prefill against the real lancache (A Plague Tale: Requiem
prefilled 89.5% → 99.998% over two `--force` runs; root-caused the residual to a
transient agent ConnectTimeout, which led to the resilience finding below).

## Findings

| # | Sev | Area | Finding | Triage |
|---|-----|------|---------|--------|
| 1 | SEV-2 | agent RPC | A transient connect blip on any of the thousands of 0.5s prefill **poll** GETs (or the dispatch POST) fails the whole multi-hour job — `_post_then_poll` has no retry; connect timeout 10s. Same `_post_then_poll` weakness fails Epic `pull` too. | **Fix Now** |
| 2 | SEV-3 | agent RPC | Single-call agent ops (`steam_validate`, `prefilled_apps`) and `/health` `auth_status` also die / flap on one transient blip (validate left a game stuck `downloading`; /health flips to 503). | **Fix Now** (same root fix) |
| 3 | SEV-3 | force-prefill (#203) | Dedup force-upgrade `UPDATE … WHERE id=?` lacked `AND state='queued'` → TOCTOU: worker claims the queued job (runs non-force) before the UPDATE lands, so the DB records `force=true` but the prefill ran without it. | **Fix Now** |
| 4 | SEV-4 | force-prefill (#203) | Epic `?force=true` is accepted + persisted but ignored (Epic path never reads it). Harmless (Epic always re-downloads) but the record/CLI text is misleading. | **Defer** |
| 5 | SEV-4 | force-prefill (#203) | Force that dedups onto an already-**running** prefill no-ops with a success-looking "queued prefill" message; operator gets no signal to re-run. | **Defer** |

## Remediation (this session — branch `fix/agent-poll-resilience`)

- **#1 + #2 → `clients/agent_client.py`:** bounded **connect-phase retry in `_request`**
  (ConnectError/ConnectTimeout/PoolTimeout — request never reached the agent, so safe
  to retry any method; default 2 retries). Connect timeout **10s → 15s**; poll interval
  **0.5s → 3s**. One change covers prefill poll, Epic pull, validate, library_sync, and
  largely stops /health flapping. HTTP-status errors and `agent job failed` still
  propagate immediately; non-connect transport errors are not blind-retried (POST-safety).
  Tests: `tests/clients/test_agent_client.py` (+6: transient-blip tolerated, exhaust-then-raise,
  500-not-retried, POST-blip-retried, 15s/3s defaults).
- **#3 → `api/routers/prefill_trigger.py`:** added `AND state='queued'` to the upgrade
  UPDATE + rowcount-aware logging (only log `force_upgraded` when it actually applied).
  Test: `test_force_upgrade_skips_concurrently_claimed_job` (stale-read pool simulates the race).

## Deferred (filed for follow-up, none SEV-1/2)
- #4 Epic force: reject force for non-steam or store NULL + document steam-only.
- #5 running-force no-op: surface a distinct response so the operator knows to re-run.
- /health smoothing: a consecutive-failure threshold before flipping `agent_reachable`
  (largely mitigated by the #1/#2 retry).

## Result
- 7 new tests; full suite 1302 passed (only the documented `tests/test_licenses.py` local
  pip-licenses failure); ruff + mypy clean.
- No open SEV-1/2 after remediation. Gate cleared.
