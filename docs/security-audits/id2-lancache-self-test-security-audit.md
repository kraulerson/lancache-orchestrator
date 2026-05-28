# Security Audit — ID2 Lancache Self-Test

**Feature:** ID2-lancache-self-test
**Audit date:** 2026-05-27
**Audited modules:**
- src/orchestrator/lancache/__init__.py
- src/orchestrator/lancache/heartbeat.py
- src/orchestrator/api/main.py (lifespan wiring)
- src/orchestrator/api/routers/health.py (probe consumption)
- src/orchestrator/core/settings.py (3 new fields)

<!-- Last Updated: 2026-05-27 -->

## Scope

Post-implementation security review of the lancache heartbeat probe and
its integration with `/api/v1/health`.

## Methodology

1. ruff / ruff format / mypy --strict — all clean (40 source files)
2. gitleaks full-repo scan — no leaks
3. semgrep p/owasp-top-ten on `src/orchestrator/lancache/` — 0 findings
4. Manual review against threat-model entries TM-012 (log redaction),
   TM-013 (SSRF), TM-014 (DoS via unbounded retry)
5. Test coverage review — 27 new tests across 3 files cover the
   security-relevant edge cases (timeout/error fallback, cache TTL
   bounds, URL validation)

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-012 (log redaction):** MITIGATED. The probe logs only
  `url=<configured_url>`, `error=<exception_class>`, `reason=<str(e)[:200]>`,
  and `reachable=<bool>` — no credentials, no response bodies, no headers.
  The lancache heartbeat is a public-LAN endpoint with no auth, so even
  the URL itself isn't sensitive.

- **TM-013 (SSRF — server forces backend to fetch attacker-controlled URLs):**
  MITIGATED.
  - The probe URL is configured via `Settings.lancache_heartbeat_url`,
    which loads only from env vars / Docker secrets / .env files — operator-controlled, not attacker-controlled.
  - Pydantic validates the URL at startup: rejects empty strings, max
    length 2048. The `LancacheProbe.__init__` further rejects any URL
    not starting with `http://` or `https://` — guards against
    `file://`, `gopher://`, etc. that could exfiltrate via SSRF.
  - The probe doesn't follow redirects automatically (httpx default
    is `follow_redirects=False`) — even if lancache is replaced with
    an attacker-controlled host, redirects don't propagate to other
    targets.
  - Probe runs only at /health requests, which are unauthenticated;
    rate-limiting is handled by the 30s cache TTL — at most ~2 probes
    per minute regardless of request volume.

- **TM-014 (resource exhaustion via the probe):** MITIGATED.
  - Per-call timeout: 5.0s (configurable 0–60s). httpx ConnectTimeout +
    ReadTimeout + TimeoutException all caught.
  - Cache TTL: 30.0s default (configurable 0–600s). Even if /health is
    hammered, only one probe fires per TTL window.
  - Concurrent /health requests serialize on `asyncio.Lock` — verified
    by `test_concurrent_probes_collapse_to_single_call` (10 parallel
    callers → 1 outbound HTTP call).
  - No retry loop on failure. A failed probe simply caches False and
    waits for the next TTL window. No backpressure, no hammer.

## Defensive-programming review

- `_refresh()` catches `Exception` as a last-resort guard so unexpected
  failures (DNS pathology, library bugs) don't crash /health. Verified
  by `test_unexpected_exception_returns_false`.
- `last_checked_at_mono()` returns `None` until the first probe runs —
  callers cannot mistakenly believe the cache is fresh before any IO
  has happened.
- URL validation happens in `__init__`, not at probe time — fail-fast
  at lifespan startup if config is malformed.

## Operator surfaces

- `lancache.probe.network_error` (WARN) — every documented network
  failure mode emits this with the exception class + truncated message.
- `lancache.probe.unexpected_error` (ERROR) — defensive catch for
  anything else; emits the exception class.
- `lancache.probe.state_changed` (INFO) — emits when the probe result
  flips, allowing operators to correlate lancache-down windows.

No PII, no credentials, no response bodies in any log event.

## Sign-off

No SEV findings. ID2 cleared to ship.

— Senior Security Engineer persona, 2026-05-27
