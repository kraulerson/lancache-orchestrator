# UAT Session 14 — Closure

**Opened:** 2026-08-24 · **Closed:** 2026-08-26 · **Checklist:** 9/9 · **Gate:** reopened

## Planned vs. actual

| | Planned | Actual |
|---|---|---|
| Scope | The 2 features the gate named | **The 2 features, plus the 158-commit surface behind them.** The counter knew about `epic-prefill-status-based` and `f18-cache-purge`; 158 commits had landed since UAT-13 on 2026-07-04. Agent arms were scoped to the real surface. |
| Human template | One template | **Two.** v1 was a broken page — see below. |
| Findings | — | 8 filed (#292–#299) |
| Triage | Fix Now / Defer split proposed | **Orchestrator chose "fix everything"**, ordered small → Kuma → large |
| Remediation | — | 8 fixed, 9 commits, all test-first with RED observed |

## Verdict on the features under test

Both meet their documented acceptance criteria.

- **`epic-prefill-status-based`** — live and behaving as specified. `ORCH_SCHEDULED_PREFILL_ENABLED=true`, so the Epic cutover it was built to unblock has actually happened.
- **`f18-cache-purge`** — satisfies all ten points of its spec coverage against ADR-0015 and issue #37.

Every defect found was in what the system *reported*, not in what it did.

## The theme

Four independent findings, one shape: **a green status that had stopped meaning anything.**

- A validator reporting `cached` over an empty manifest — and an archive copy that made it permanent.
- `fetch_manifests` recording `succeeded` while 669 of 1170 apps failed.
- Game 15035 reading `up_to_date` after 52 days and 27 sweeps without a single successful validation.
- A purged game still reporting 337/337 chunks cached, seconds after its files were deleted.

## Findings and disposition

| # | Finding | Commit |
|---|---|---|
| 295 | Local `pytest` hung indefinitely — no timeout on the tool resolver | `9ede2c2` |
| 296 | `jobs --kind` rejected `purge`/`fetch_manifests`; its guard test was stale | `7ffe3f1` |
| 293 | `chunks_cached` not reset by a purge | `c19d881` |
| 299 | Kuma had taken 854 consecutive 403s — allowlist | config, live |
| — | Kuma push heartbeats (requested by the homelab session) | `abd1cdd` |
| 294 | `fetch_manifests` reported success while half failing | `790e65e` |
| 292 | Zero-chunk manifest classified as `cached`; archive copy non-atomic | `6c00c68` |
| 297 | Validate aborted at a flat 300s | `edc1508` |
| 298 | Agent had no request-body cap | `5c89c7d` |

## Decisions made

- **#294 — job state left as `succeeded`.** I filed it as "the job lies"; it does not. `succeeded` means the work ran. With 669 failures the steady state against 98.7% coverage, failing the job would make `failed` meaningless in the other direction. The tally goes to the monitor instead, with a threshold above today's ratio so the monitor is not born red.
- **#292 — zero chunks reports `error`, not `validation_failed`.** `validate.py`'s `_STATUS_FOR` has no entry for `error`, so an unreadable manifest leaves `games.status` untouched rather than flipping it green *or* falsely failing a healthy game. A test asserts that mapping stays absent.
- **#293 — purge appends to `validation_history` rather than the API suppressing counts.** Widens that table's writers from the validator alone, deliberately: purge changes cache state and should record it.
- **#297 — scaled by chunk count, not a bigger constant.** MechWarrior 5 Editor already sat at 271s against 300s; a flat bump moves the cliff.
- **#298 — the design changed after approval.** Proposed as "wiring": add the API's middleware. Real data showed the API's 32 KiB cap would reject every Epic validation, because the largest manifest travels as an ~88 MB body. Cap set at 128 MiB, with a test asserting it must stay *looser* than the API's.
- **Heartbeats hooked into the worker, not the four handlers.** Scheduled prefill is not its own job kind — it is `kind='prefill'` with `source='scheduler'`. Keying on kind alone would have pushed the monitor *up* whenever someone triggered a prefill by hand, reporting a dead scheduler as healthy.

## Process failures in this session, recorded rather than buried

- **The v1 template was a broken page.** Replace-all substitution hit the placeholder tokens inside the `AGENT:` guidance comments as well as their real slots, injecting content into `<script>`. The page rendered headings and `0 / 0 completed` with no scenarios. This repeated a fault from the Pantheon project. `scripts/lint-uat-scenarios.sh` exists to catch exactly this and was not run; it fails on v1. Rebuilt as v2 and verified three ways — linter, `node --check`, jsdom render.
- **v1 was also mis-sourced.** Five themes invented from agent findings rather than scenarios derived from the features' acceptance criteria. That produces a confirm-these-bugs list, not an acceptance test.
- **An accidental purge of game 2451 during adjudication.** Verifying scenario 5 looped over `GET` and `POST`; `POST` is the destructive verb. Job `45142` was assistant-initiated. Restored immediately (`45145`, `45146`); no lasting impact. Probing an endpoint's shape must use safe verbs only.
- **Scenario 5's own `curl` omitted the bearer token**, so it asserted `405` where auth returns `401` first — it could not have passed however the product behaved. Recorded as UAT-14-D.

## Deferred

- Nine SEV-3s share one root cause with #298: hardening applied to `api/` was never carried across to `agent/`. #298 fixes the worst; the rest remain.
- `platforms` table is stale and misleading (steam reads `expired` while `/health` says OK; epic's expiry 52 days past yet reads `ok`; epic `last_sync_at` NULL despite 6-hourly syncs).
- 1358 `not_downloaded` + 16 `failed` sit permanently outside the sweep's `WHERE`; 36 are real games.
- `192.168.1.40` returns HTTP 508 and is still the target of `ORCH_LANCACHE_HEARTBEAT_URL`.
- `GIT_SHA=unknown` in the deployed container — the running commit cannot be identified.
- A single 1-in-3115 chunk `ReadTimeout` permanently killed Caravan SandWitch.
- LXC root at 75% (12.9 GB reclaimable).
- The shared UAT template still never refreshes its progress counter on load, so a correctly generated page reads `0 / 0` until first click. Fixed in session 14's copy only.

## Outstanding

Deploy — the single one covering all nine commits plus the four `ORCH_KUMA_PUSH_*` URLs. Kuma monitors 179–182 stay paused until it lands.
