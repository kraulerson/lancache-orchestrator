# Security Audit — Heartbeat delivery gates the breaker dedupe stamp (#313)

**Feature:** 313-heartbeat-delivery-gates-dedupe-stamp
**Modules:**
- `src/orchestrator/clients/heartbeat.py` — `push()` now returns a delivery bool
- `src/orchestrator/jobs/measurement.py` — `_breaker_notice_due()` / `_stamp_breaker_notice()` split, `_BREAKER_RETRY_SEC`, `_notify_breaker()` returns delivery
**Audit date:** 2026-09-15
**Auditor:** self-review (Senior Security Engineer persona) + semgrep `--config=auto` + gitleaks + ruff (flake8-bandit `S`) + mypy
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-09-15 -->

## Scope

An availability/alerting-integrity defect, not a data defect. `_breaker_notice_due()` stamped its once-per-window dedupe clock **before** attempting the Kuma push, and `heartbeat.push` swallowed every failure internally — so a push that never arrived silenced a library-wide cache-loss incident for the full 60-minute window. The sweep aborts on the **first** trip, so a sweep makes exactly one push attempt; and an unreachable NAS or network is precisely correlated with the mass eviction that trips the breaker, so the two failure modes are not independent.

## Methodology

1. **SAST.** `semgrep --config=auto --error` over both changed files — clean. `ruff check src tests` — clean.
2. **Secrets.** `gitleaks detect` — no leaks. Specifically checked that the new `heartbeat.push_rejected` log line carries no URL (the push URL is a secret token).
3. **Type safety.** `mypy src` — clean, 110 files.
4. **Single-writer guard.** `tests/test_measurement_writer_guard.py` green — this change touches notification only, never cache truth.
5. **Threat-model cross-check:** secret exposure in logs, availability/self-DoS, alert suppression, information disclosure.
6. **Tests.** Full suite 1912 passed, 3 deselected. 9 new tests.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No new findings. | — |

The issue being fixed (#313) was itself the SEV-3.

## Non-findings (explicitly checked, clean)

- **No secret in the new log line.** `heartbeat.push_rejected` logs `status` and `http_status` only. The push URL embeds the monitor's token and is deliberately never logged — matching the existing `heartbeat.push_failed` line, which logs only the exception text. Verified the exception text itself: httpx `ConnectError`/`ReadTimeout` carry host, not the token path, and the field is truncated to 200 chars regardless.
- **The retry backoff cannot become a self-DoS.** This was the real risk in fixing #313, because the naive fix — stamp only on success — reopens the defect the stamp was originally added for: a refused write records no transition row, so the count stays frozen and every later downward measurement recomputes the same trip. With Kuma unreachable that is ~1000 pushes and ~20 minutes of pure 10-second timeouts inside one sweep. `_BREAKER_RETRY_SEC = 60.0` bounds it to at most one attempt per minute, and `test_a_failed_push_backs_off_instead_of_retrying_every_game` pins it: 40 games, one attempt.
- **Alert suppression is now strictly harder, never easier.** Every path that previously silenced a notice still silences it only after a *delivered* push. The `no URL configured` case returns True (nothing to retry) — that is an operator's deliberate disable, and retrying a push to a monitor that does not exist every 60 s would be pure waste. No configuration makes the breaker quieter than before this change.
- **A rejected HTTP status is now treated as undelivered.** A mistyped push token answers 404: the request completed and nobody was told. Previously indistinguishable from success, so a typo'd `ORCH_KUMA_PUSH_MEASUREMENT_BREAKER` silently disabled breaker alerting forever. Now it is retried and logged. This is a **latent defect this change also closes**, not just a refactor.
- **Still never raises.** `push()` keeps its no-raise contract, and `_notify_breaker` keeps its `except` guard (now returning False). A dead monitor still cannot suppress the `CircuitBreakerTripped` exception that actually halts writing — pinned by the pre-existing `test_failed_push_does_not_suppress_the_trip`.
- **No change to cache truth, the breaker threshold, or the window.** Only *when a notice is attempted* changed. The refusal behaviour, the transition log, and the counted predicate are untouched.
- **Other `push()` callers are unaffected.** `worker._emit_heartbeat` ignores the return value; the signature change is additive.

## Decision

**Cleared to advance.** No new findings. Fixes a SEV-3 alerting-integrity defect plus a latent one (a 404 push token silently disabling alerts), without reopening the self-DoS the dedupe stamp exists to prevent.

## Sign-off

- Implementation: commit `<pending>`
- Test suite: 1912 passed, 3 deselected; 9 new tests (heartbeat delivery 6, breaker dedupe 3)
- semgrep, gitleaks, ruff, mypy clean
- No migration, no new dependency, no schema change
- **Not yet exercised in production** — verified by tests only. The breaker has not tripped since deploy.
