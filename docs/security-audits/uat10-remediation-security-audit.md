# Security Audit — UAT-10 Remediation

**Date:** 2026-06-04
**Branch:** `fix/uat10-remediation`
**Scope:** the 11 confirmed findings + 1 observation from the UAT-10 automated
adversarial sweep (`tests/uat/sessions/2026-06-04-session-10/agent-results/uat-10-agent-sweep-2026-06-04.md`),
covering F5 (Steam prefill) and F6 (Epic prefill).
**Persona:** Senior Security Engineer. Each finding was confirmed by an
independent skeptic agent against the actual code before remediation; every fix
landed test-first (failing test → fix → green).

## Security-relevant fixes

| Finding | Vector | Fix | Regression test |
|---------|--------|-----|-----------------|
| **#1 (SEV-2)** | **Decompression bomb / DoS.** `manifest.py:_parse` used unbounded `zlib.decompress`; a tiny compressed manifest (well under the 128 MiB *compressed* cap) could inflate to GBs and OOM the process (reproduced: 510 KB → ~500 MB RSS). | `_decompress_capped()` uses `zlib.decompressobj().decompress(body_raw, max_bytes)` and raises `EpicManifestError` when `unconsumed_tail`/`not eof` (cap hit or truncated). `parse_manifest(raw, *, max_decompressed=_MAX_DECOMPRESSED_BYTES=256 MiB)`; the bomb is rejected **before** allocation. | `test_decompression_bomb_is_capped` (tiny compressed body, `max_decompressed=1024` → `EpicManifestError`); `test_compressed_body_within_cap_still_parses` (normal compressed manifest still parses). |
| **#4 (SEV-3)** | **SSRF on the manifest fetch.** `fetch_manifest` issued `client.get(uri)` to the response-supplied (attacker-influenceable under MITM/CDN-compromise) `uri` **before** validating it; the FQDN + `..` checks ran only after the GET, so they protected the chunk path but not the manifest GET itself (an internal host / `169.254.169.254` could be fetched). | The `urlparse` + `_HOSTNAME_RE` FQDN guard + `..`-rejection now run **before** the GET. The FQDN regex requires a dotted public host with an alpha TLD, so bare internal hosts, IP literals, and `file://`/`gopher://` (empty `hostname`) are all rejected pre-fetch. | `test_fetch_manifest_rejects_non_fqdn_host_before_get` and `test_fetch_manifest_rejects_path_traversal_before_get` both assert `EpicManifestError` **and** that the unvalidated host was never fetched (`downloaded["hit"] is False`). |
| **#9 (SEV-4)** | Traversal-guard regression risk — the `..`-in-`cdn_base` mitigation had no dedicated test. | Covered by `test_fetch_manifest_rejects_path_traversal_before_get` (above). | — |
| **#10 (SEV-4)** | Symlink/TOCTOU regression risk — the `O_NOFOLLOW` token-file guard had no test, so a refactor to `Path.write_text`/`open()` could silently re-open arbitrary-file-truncation. | Pinned by `test_save_refresh_token_refuses_symlink` (plants a symlink at the token path, asserts `OSError` + the link target is untouched). | — |

## Correctness / stability fixes (security-adjacent)

- **#2 (SEV-3)** — Steam prefill no longer leaves a game stuck `downloading` on an
  IPC/worker/auth failure; `_steam_prefill` now wraps the work and marks `failed`
  (`WHERE status='downloading'`), mirroring the Epic guard. Accurate status is a
  precondition for trustworthy cache-coverage reporting.
- **#3 (SEV-3)** — A post-prefill `validate` with `outcome='error'` (e.g. cache
  unmounted) now resolves the *transient* `downloading` state to `failed`
  (scoped `WHERE status='downloading'`) without clobbering an
  already-classified status.
- **#5 (SEV-3)** — The prefill trigger is now platform-parameterized
  (`steam`/`epic`); the Epic prefill handler is no longer dead code reachable
  only by manual DB insert. Inputs remain parameterized SQL; the platform is
  validated against an allow-list (`{steam, epic}`) with a 400 otherwise.
- **#7 (SEV-4)** — Epic OAuth's 200-success path now raises `EpicAuthError` on a
  malformed/non-JSON body instead of a raw `KeyError`/`JSONDecodeError`, so a
  hostile/proxy 200 maps to the documented 401/NotAuthenticated rather than a
  500. The new log event carries only `what`+`status` — **never** the body/token.
- **Observation** — the Steam auth auto-enqueue now uses atomic
  `INSERT ... ON CONFLICT DO NOTHING` (was a SELECT-then-INSERT straddling an
  `await`, which could raise a benign `IntegrityViolationError` against the
  0004 UNIQUE index), consistent with the four other call sites.

## Non-security test-quality fixes

- **#6 (SEV-3)** — Epic downloader retry loop now has tests for transient-5xx
  recovery (503→200) and transport-error retry-then-fail.
- **#8 (SEV-4)** — `verify_cached` MISS arithmetic (ratio 0.5) and the
  `hit_ratio<0.5` non-gating warning branch are now exercised.
- **#11 (nit)** — removed a redundant module-level `pytest.mark.asyncio`
  (`asyncio_mode=auto` already collects the coroutines) that warned on two sync
  tests.

## Findings status

**0 open security findings.** The two security-material findings (#1 DoS, #4
SSRF) are fixed and regression-tested; the two security-relevant test gaps (#9,
#10) now pin their guards. No token/credential material is logged by any changed
path. No new dependencies, no new SQL interpolation. Full suite **1051 pass**;
`mypy --strict`, `ruff`, `gitleaks`, and the custom `semgrep` rules are clean.

## Residual / accepted (unchanged from F6)

- The app-wide `RequestValidationError` handler can still echo a malformed
  `/epic/auth` body (the single-use, short-lived OAuth code) back to the
  **submitter** — out of scope for this remediation (a global-handler change);
  remains flagged for a follow-up to strip `input` for sensitive models.
- The #4 FQDN guard still permits an attacker-controlled *public* FQDN that
  resolves to an internal IP (DNS rebinding / `metadata.google.internal`).
  Reordering the check before the GET is a strict improvement; resolve-and-reject
  RFC1918/link-local (or domain pinning) is noted as defense-in-depth for a
  future pass.
