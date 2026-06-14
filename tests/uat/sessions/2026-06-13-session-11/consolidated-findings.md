# UAT-11 Consolidated Findings & Triage — 2026-06-13

Features under test: F11 orchestrator-cli, F13 validation sweep, operability/self-recovery (post-audit).
Legs: automated-suite (PASS), exploratory (operator UX), integration/self-recovery (system/deploy).

## Automated leg: PASS
1165 tests / mypy --strict / ruff / semgrep / gitleaks / license — all green, no pool hang.
Non-blocking follow-ups: no dedicated `tests/cli/test_base.py`; defensive double-failure branches uncovered.

## Triage table

| ID | Sev | Area | Finding | Recommended |
|----|-----|------|---------|-------------|
| S11-E-01 | SEV-2 | cli | missing `ORCH_TOKEN` → raw traceback (`handles_local_errors` misses the scrubbed `ValueError`) | **Fix now** (non-deferrable) |
| F-INT-1 | SEV-3 | jobs | per-job timeout cancels handler via `CancelledError` (BaseException) → game stuck `downloading`; also on crash | **Fix now** (regression from quick-win) |
| F-INT-3 | SEV-3 | deploy/sec | Dockerfile hardcodes `--host 0.0.0.0` → exposes trigger endpoints to LAN; fires non-loopback warning every boot | **Decision needed** (network model) |
| S11-E-03 | SEV-3 | cli | wrong Steam/Epic creds reports "check ORCH_TOKEN" (discards server 401 detail) | Fix now (cheap) |
| S11-E-04 | SEV-3 | cli | invalid `--state/--kind/--status` silently returns empty table | Fix now (click.Choice) |
| S11-E-05 | SEV-3 | cli | `game show -5` → "No such option" not "invalid id" | Fix now (cheap) |
| F-INT-2 | SEV-3 | db pool | writer has no self-heal after storm guard (reader does); restart-only recovery | Defer (surfaces via health→503; more involved) |
| S11-E-06 | SEV-4 | cli | `epic_token_url` over-redacted (URL, not secret) | Fix now (allow-list redaction) |
| S11-E-07 | SEV-4 | cli | noisy `/run/secrets` UserWarning on every off-host invocation | Fix now (cheap) |
| S11-E-08 | SEV-4 | cli | `db vacuum` error omits DB path (`db migrate` includes it) | Fix now (cheap) |
| F-INT-4 | SEV-4 | docs | no compose/`docker run` recipe; README env table omits ~20 `ORCH_*` incl. job_max_runtime | Fix now (docs) + CLI docs |
| F-INT-5 | SEV-4 | db | `manifest_fetch` lacks in-flight UNIQUE index (race-prone) | Fix now (migration 0007, mirror 0006) |
| F-INT-6 | SEV-4 | steam | timeout-cancelled steam handler doesn't free serial worker subprocess | Defer (edge; tied to F-INT-1 fix) |
| S11-E-09 | nit | cli | `python -m orchestrator.cli.main` no-op (no `__main__` guard) | Fix now (trivial) |
| S11-E-10 | nit | cli | `config show` column overflow on long keys | Fix now (trivial) |
| S11-E-11 | nit | cli | `--limit` help omits the 500 max | Fix now (trivial) |
| docs | — | docs | CLI undocumented in README/user-guide | Fix now (README operator-CLI section) |

## Live/credentialed leg (needs Karl)
Scenarios 3 & 4 (auth steam 2FA, auth epic OAuth) + on-screen confirmation — to run post-remediation against the live dockerized deploy.
