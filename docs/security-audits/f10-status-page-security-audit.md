# Security Audit — F10 Status Page

**Feature:** F10-status-page
**Audit date:** 2026-05-28
**Audited modules:**
- src/orchestrator/api/routers/status.py
- src/orchestrator/api/dependencies.py (auth exemption)
- src/orchestrator/api/main.py (router wire-up)

<!-- Last Updated: 2026-05-28 -->

## Scope

Post-implementation security review of the single-file HTML status
page at `GET /`, its auth exemption, and the embedded vanilla-JS
client.

## Methodology

1. `ruff check` + `ruff format --check` + `mypy --strict src/` — all clean (42 source files)
2. `gitleaks detect` full-repo scan — no leaks
3. `semgrep p/owasp-top-ten` on `src/orchestrator/api/routers/status.py` — 0 findings
4. Manual review against threat-model entries TM-001 (auth bypass),
   TM-012 (credential leakage), XSS / CSRF surface
5. Test coverage review — 18 new tests cover the security-relevant
   surfaces

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-001 (auth bypass):** MITIGATED. `GET /` is intentionally
  bearer-exempt (Bible §9.3 contract — the page itself is the
  bearer-prompt UI). However:
  - The page returns ONLY static HTML; it carries no sensitive data.
  - All `/api/v1/*` endpoints the JS calls are still bearer-gated by
    `BearerAuthMiddleware`. An attacker accessing `/` without a token
    sees the chrome but no operator data.
  - Loopback-only endpoints (`/auth`, `/auth/{challenge_id}`) are
    NOT reachable from the page even with a token, because
    `LOOPBACK_ONLY_PATTERNS` is enforced on `scope[client]` — a
    browser on a non-loopback host hits the LAN address, not
    127.0.0.1.

- **TM-012 (credential leakage):** MITIGATED.
  - Bearer token stored in `sessionStorage` only (cleared on tab
    close); never written to localStorage, cookies, or URL.
  - Response headers `Cache-Control: no-store` + `Referrer-Policy:
    no-referrer` prevent CDN / proxy / next-hop leakage if the page
    is accidentally exposed.
  - The page emits NO logs server-side beyond the standard request
    log (`api.request.received`); no bearer interaction is
    server-loggable.
  - Token field on `prompt()` uses standard text input — operator
    sees it during typing (consistent with Phase-0 acceptance per
    Bible §9.3).

- **XSS (reflected/stored):** MITIGATED. The HTML is a static
  template constant; no user input is interpolated server-side. The
  JS DOM-update functions all route through an inline `escapeHtml()`
  helper before insertion. No `innerHTML` or `outerHTML` sets raw
  API response fields — every field goes through escape first.

- **CSRF:** MITIGATED by design.
  - The page is operator-driven, LAN-only.
  - All mutating endpoints (`POST /platforms/.../auth`, `POST
    /library/sync`) require bearer auth via `Authorization` header
    (not cookies). CSRF is irrelevant when the credential is in a
    request header rather than an ambient cookie.
  - `<meta name="robots" content="noindex,nofollow">` + the
    no-cache headers prevent search-engine cache poisoning.

- **Clickjacking:** MITIGATED. `X-Frame-Options: DENY` blocks
  framing. The page is not embeddable.

- **MIME-confusion:** MITIGATED. `X-Content-Type-Options: nosniff`
  + explicit `text/html` content type.

- **Supply chain:** MITIGATED. Zero external dependencies — no
  remote `<script src=>` or `<link rel="stylesheet" href=>`.
  Verified by `test_no_external_script_src` +
  `test_no_external_stylesheet_link`. Bundle is fully self-contained
  for LAN-only deployments without internet egress.

## Defensive-programming review

- All DOM insertions in the JS go through `escapeHtml()` which
  handles `<`, `>`, `&`, `"`, `'`. Manually audited every callsite —
  no `innerHTML = ...` with un-escaped user data.
- `apiGet()` clears the bearer from `sessionStorage` on 401 and
  re-prompts on the next call — prevents stuck-with-bad-token loop.
- 5xx triggers backoff to 10 s polling — prevents thundering herd
  against a degraded backend.
- The JS catches all `fetch()` errors and surfaces them in-pill
  rather than throwing uncaught promise rejections.

## Operator surfaces (server-side logs from page hits)

- `api.request.received` (INFO) — standard log; page fetches show
  `path=/` with no `Authorization` header.
- `api.request.completed` (INFO) — duration.

No credentials in logs (the page fetch doesn't send any). The JS's
subsequent `/api/v1/*` fetches DO carry bearer, and those are
already covered by ID3 redaction.

## Sign-off

No SEV findings. F10 cleared to ship.

— Senior Security Engineer persona, 2026-05-28
