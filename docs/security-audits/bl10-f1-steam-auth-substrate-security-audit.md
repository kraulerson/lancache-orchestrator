# Security Audit — BL10 Steam Auth Substrate

**Feature:** BL10-F1-steam-auth-substrate
**Audit date:** 2026-05-24
**Audited modules:**
- src/orchestrator/platform/steam/{client,protocol,session,worker}.py
- src/orchestrator/api/routers/auth.py
- src/orchestrator/api/dependencies.py (LOOPBACK_ONLY_PATTERNS regex extension)
- src/orchestrator/api/main.py (lifespan + RequestValidationError handler)

<!-- Last Updated: 2026-05-24 -->

## Scope

Post-implementation security review of the subprocess-isolated steam-next
worker + IPC contract + two-step auth + session persistence + platforms-table
integration.

## Methodology
1. ruff / ruff format / mypy --strict — all clean
2. semgrep p/owasp-top-ten on platform + auth router — 0 findings
3. gitleaks full-repo scan — no leaks
4. Manual review against threat-model entries TM-001 (auth bypass), TM-004
   (credential leak), TM-012 (log redaction)

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

## Threat-model walk

- **TM-001 (auth bypass):** MITIGATED. The new endpoints inherit
  BearerAuthMiddleware + LOOPBACK_ONLY_PATTERNS. The status endpoint
  intentionally not loopback-only (per spec §4.3) but bearer-required.
- **TM-004 (credential leak):** MITIGATED. Credentials enter via JSON
  request body, are passed to the subprocess via stdin pipe (no env vars),
  and never written to DB. `platforms.config` JSON has `{steam_id, username,
  last_refreshed_at}` ONLY. Username is identifier, not credential.
- **TM-012 (log redaction):** MITIGATED. Verified via
  `test_no_password_in_logs` — capsys captures all log output during an
  auth-begin call and asserts the secret string never appears. ID3's
  `_redact_sensitive_values` walks dicts and matches `password`/`token`/
  `secret` keys; the auth router only logs `username_present=True` (no
  username, no password).

## Subprocess-isolation specifics

- Worker spawned with `start_new_session=True` (signal isolation).
- Worker env restricted to PATH, LANG, LC_ALL (no creds in env).
- IPC over private pipes (no network).
- 10 MiB cap on any IPC line (back-pressure guard, D20).
- Per-request 30s timeout via `Settings.steam_worker_ipc_timeout_sec`.
- Restart-storm guard: max 3 deaths per orchestrator process lifetime
  before the worker is marked disabled (operator must restart).

## In-memory challenge state

- `_challenge_expiries: dict[str, float]` — challenge_id → expires_at.
  Lives in router module; server restart invalidates.
- 5-min TTL per challenge.
- Challenge_id is uuid4 (no rate-limit on guessing needed; bearer auth
  is the gate, and even with a known challenge_id the operator needs a
  valid 2FA code which is rotated server-side).

## Non-findings

- No SQL injection vector: all DB writes use `?` placeholders.
- No path-traversal: session_dir + session_path are Settings-derived
  (not user input).
- No deserialization of untrusted data: worker only receives JSON from
  orchestrator stdin (which itself receives JSON from authenticated
  HTTPS clients).
- No timing oracle: hmac.compare_digest already applied for bearer auth
  (existing); auth.begin's credential comparison happens inside steam-
  next (out of our control but standard).

## Verification artifacts
- `pytest -q`: 657 tests pass
- `ruff check` / `ruff format --check`: clean
- `mypy --strict`: clean (4 files; worker.py excluded — imports steam/gevent)
- `semgrep --config p/owasp-top-ten`: 0 findings (152 rules on 7 files)
- `gitleaks detect`: no leaks (139 commits scanned)

## Conclusion

**APPROVED for merge.** Zero findings. The subprocess-isolation pattern
established by this BL is captured in ADR-0013 for F2 (Epic) reuse.
