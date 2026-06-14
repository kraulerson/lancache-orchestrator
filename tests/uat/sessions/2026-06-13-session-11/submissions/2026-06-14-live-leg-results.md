# UAT-11 — Live-System Validation Results

**Date:** 2026-06-14
**Tester:** Claude (deploy + orchestration) + Karl (Steam credentials / 2FA only)
**Host:** lancache host `192.168.1.40`, container `orchestrator-uat11` (`--network host`, loopback :8765, `--restart unless-stopped --memory=1500m`, cache RO-mounted, named volume holds the Steam session)
**Image under test:** branch `fix/steam-worker-stderr-drain` @ `b70043c` (PR #155), built on the host. Includes the merged #154 Docker fix.

## Summary

**PASS.** Every remediated UAT-11 finding was confirmed live, and the live leg uncovered + fixed a real intermittent worker-crash bug (the `gevent.Timeout` blocker) that the prior legs' stubs could not have caught — exactly the "validate against live systems" principle. The full prefill→cache→validate loop passed on two Steam apps, and the worker now survives the slow-CDN path that previously killed it.

## What the live leg uncovered (and fixed) — the headline

During the live leg the Steam worker crashed mid manifest fetch with `steam_worker.died reason=stdout_closed` and **no diagnostics** (the worker's stderr was an undrained PIPE). Two commits resolved it:

1. **`0653416` — drain + capture worker stderr.** A dedicated task now drains the worker's stderr for its lifetime, logs each line as `steam_worker.stderr`, and attaches the last 10 lines to the `steam_worker.died` breadcrumb. (Also closes a latent 64 KiB pipe-fill stall.) This instrumentation captured the real crash on first recurrence.

2. **`b70043c` — survive `gevent.Timeout` from a slow CDN depot.** Captured stderr showed `gevent.timeout.Timeout: 15 seconds`. Root cause: `gevent.Timeout` subclasses **`BaseException`** (gevent design), so steam-next's 15s CDN timeout escaped every `except Exception` in the handlers AND the dispatch loop, killing the worker process. Intermittent + CDN-timing-dependent (app 211 fetched in 4s on one run; app 340 crashed at 15s on another) — which is why it was never the `#15` cleanup change, never OOM (`oom_kill 0`), never an op timeout (300s budget vs 25s death). Fixed defense-in-depth: the `manifest.fetch` handler converts it to a retryable `SteamCDNTimeout` (no partial-as-success, per #109; temp BLOBs cleaned), and `main()`'s dispatch loop wraps every handler so no op's timeout can take the worker down.

Tests: `TestWorkerStderrDrain`, `test_manifest_fetch_gevent_timeout_does_not_crash_worker`, `test_dispatch_loop_survives_handler_gevent_timeout`. Full suite **1190 pass**; mypy(strict)/ruff/gitleaks/semgrep clean.

## Live evidence

### Remediated findings confirmed live (pre-blocker)
| Finding | Result |
|---|---|
| #154 Docker shebang/entrypoint | ✓ container starts; `orchestrator-cli` works in-container |
| S11-E-06 config redaction | ✓ `epic_token_url` not over-redacted |
| Error-UX exit codes (2/3/1) | ✓ |
| SEV-2 missing-token clean exit | ✓ |
| Colorblind status labels | ✓ |
| F-INT-3 loopback default | ✓ no non-loopback warning |
| Steam auth | ✓ SUCCESS (steam_id 76561197993987535) |
| **library_sync (#109)** | ✓ **2476 games, no IPC timeout — #109 resolved live** |
| Correlation-id quick-win | ✓ per-job `correlation_id`+`job_id` across job lifecycle |

### End-to-end loop on the fixed image
| App | manifest_fetch | prefill | validate | worker survived |
|---|---|---|---|---|
| Source SDK (211, game 9) | ✓ 2 manifests, 4s | ✓ **ok=2430/2430, failed=0** | ✓ **cached=2430, missing=0** | ✓ |
| HL2: Lost Coast (340, game 56) — *the app that crashed the worker* | ✓ 9 manifests, 8.7s | ✓ **ok=729/729, failed=0** | ✓ **cached=729, missing=0** | ✓ |

The worker served `library_sync → manifest_fetch → prefill → validate` in sequence with **zero deaths** — before the fix, one crash failed every subsequent job until restart.

### Epic
Not re-validated this leg (Epic auth needs a fresh authorization code from the operator; UAT-11's remediation was Steam/CLI/Docker-scoped). UAT-10 proved Epic live (Turaco `hit_ratio=1.0`). Optional follow-up if desired.

## Closure status
- Remediation PRs: #151 (merged), #154 (merged), **#155 (open — live-validated, pending merge).**
- After #155 merges: mark `gate_passed`, reset the test-gate counter, archive this session to `docs/test-results/`.
