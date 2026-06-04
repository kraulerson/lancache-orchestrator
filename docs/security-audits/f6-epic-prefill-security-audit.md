# Security Audit — F6 Epic CDN Prefill

**Date:** 2026-06-03
**Scope:** `src/orchestrator/platform/epic/*`, `prefill/epic_downloader.py`,
`jobs/handlers/{library_sync,prefill}.py` (Epic branches),
`api/routers/{epic_auth,epic_sync}.py`, the Epic settings.
**Origin:** F6 build. Persona: Senior Security Engineer. A 4-lens adversarial
workflow (manifest-parser, secret-handling, SSRF/correctness, test-quality) was
run over the batch; its material findings were triaged and the security-relevant
ones fixed in-batch (below).

## Threat review

| Vector | Assessment |
|--------|------------|
| **Token / auth-code disclosure** | Epic access/refresh tokens and the auth code never enter log event fields or response bodies. `oauth._grant` logs only `status` + `errorCode` (never the response body, which can echo the rejected token). `epic_auth` echoes `account_id`/`display_name` only. The refresh token persists 0600 (now via `O_NOFOLLOW`, see below) and is loaded only for the refresh grant. `_redact_sensitive_values` covers `token`/`access_token`/`refresh_token`/`authorization`. |
| **Manifest-parser OOM / DoS** | **Fixed:** `fetch_manifest` now enforces `manifest_size_cap_bytes` on the downloaded binary (was unbounded — an OOM vector via a hostile CDN response). The parser bounds `chunk_count` (`_MAX_CHUNKS`) **and** `prereq_count` (`_MAX_PREREQ`, new — was an unbounded loop), validates `meta_size <= len(body)`, and wraps `struct/zlib` errors as `EpicManifestError`. |
| **SSRF / Host-header injection** | The chunk download always targets the **lancache** (`base_url`), never the Epic CDN directly — but the `Host` header (which routes the lancache's upstream fetch) comes from Epic's signed manifest response. **Fixed:** `fetch_manifest` now validates `cdn_host` against a public-FQDN regex (rejects bare internal hostnames like `internal-service`) and rejects `..` in `cdn_base`. A MITM/compromised Epic response can no longer point the lancache at an arbitrary bare host. |
| **Refresh-token file TOCTOU / symlink** | **Fixed:** `save_refresh_token` opens with `O_NOFOLLOW` (refuses to write through a planted symlink) in addition to `O_CREAT|O_TRUNC` and mode `0600`. |
| **Stuck job state on failure** | **Fixed:** `_epic_prefill` wraps the work so any failure (auth, manifest, network) marks `games.status='failed'` + `last_error` rather than leaving the row stuck in `downloading`. |
| **Token expiry mid-prefill** | **Fixed:** `EpicClient` now checks the cached access token's `expires_at` against `epic_refresh_buffer_sec` and refreshes proactively (was: never refreshed a cached token → a long prefill could 401 mid-flight). |
| **SQL injection** | All Epic SQL is parameterized (`?`); the only interpolation is the constant `_EPIC_MANIFEST_UPSERT` template. No f-string SQL. |
| **Credential handling** | `epic_client_id`/`epic_client_secret` are the **public** legendary launcher credentials (every Epic CLI uses them) — not operator secrets; documented as such. |

## Findings

**0 open security findings.** The adversarial review surfaced a strong set; the
security/correctness-material ones were **fixed in this PR** (size cap, prereq
bound, `meta_size` bound, CDN host/base validation, `O_NOFOLLOW`, token-expiry
refresh, stuck-`downloading` guard) and covered with regression tests (incl. the
previously-untested zlib-compressed-body and UTF-16-FString parser paths).

## Residual / accepted

- **Validation-error `input` echo:** the app-wide FastAPI `RequestValidationError`
  handler returns `exc.errors()`, whose `input` field could echo a malformed
  `/epic/auth` body (the auth code) back to the **submitter**. Accepted for F6:
  OAuth codes are single-use and short-lived, the echo goes to the caller (not a
  third party), and changing the global handler is out of scope. Flagged for a
  follow-up (strip `input` for sensitive models).
- **`group_num` / manifest `version` not range-checked:** an out-of-range value
  produces a chunk path that 404s on the CDN → the chunk fails → the job fails.
  Self-correcting; not a security issue. Left unbounded to avoid rejecting a
  future valid format.
- **HIT-ratio is informational, not a hard gate:** a low post-prefill HIT ratio
  logs a WARN but does not fail the job — lancache caches asynchronously, so an
  immediate re-request can legitimately MISS; gating would cause false failures.
  Download success (all chunks 2xx through the lancache) is the success signal;
  F7-Epic disk-stat validation is the deferred authoritative check.
