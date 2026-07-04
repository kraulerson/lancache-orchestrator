# Security Audit — Manual-download folder listing (agent + orch proxy, #222)

**Feature:** manual-download-checker (agent side) — `GET /v1/manual-downloads/{launcher}` on the
agent lists the game folders under `manual_downloads_cache_path/<launcher>/` (where Karl stores
hand-downloaded GOG/Humble/Itch/Amazon games); the orchestrator proxies it at
`GET /api/v1/manual-downloads/{launcher}` so Game_shelf can diff the owned library against what
was actually downloaded. Read-only.
**Modules:** `agent/routers/manual_downloads.py`, `api/routers/manual_downloads.py`,
`clients/agent_client.py`, `core/settings.py` (new `manual_downloads_cache_path`)
**Audit date:** 2026-07-04 · **Auditor:** self-review (Senior Security Engineer persona) + ruff (S) + mypy + full suite (1508) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
ruff `--select S` clean, mypy clean (100 files), agent + api suites + full suite green
(1508 passed; only the pre-existing `test_licenses` tooling gap fails locally). Primary threat:
**path traversal** (the launcher is a filesystem path component). Also checked: auth, info
disclosure, availability, DoS.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **Path traversal — defended in depth.** `{launcher}` becomes a path component
  (`manual_downloads_cache_path / launcher`), the classic traversal vector. Three layers block it:
  (1) `_LAUNCHER_RE = ^[A-Za-z0-9_-]+$` at BOTH the orchestrator route and the agent endpoint —
  no `.` and no `/`, so `..` and absolute paths are unrepresentable; (2) the agent additionally
  `resolve()`s the target and rejects it unless `target.parent == cache_root.resolve()` (a direct
  child); (3) FastAPI path params never span `/`. `test_rejects_path_traversal_launcher` exercises
  `..`, `../cache`, `GOG/..`, `%2e%2e` → all 400/404, never a traversal.
- **Read-only, bounded disclosure.** The endpoint only `iterdir()`s ONE launcher subfolder and
  returns directory NAMES (game folder names — not sensitive, and the whole point). It cannot list
  an arbitrary path (traversal blocked), read file contents, or write anything. Dotfiles, `!*`
  control entries, and non-directories (README.md) are filtered out.
- **Auth on both hops.** Bearer token required on the agent endpoint and the orchestrator proxy
  (`test_requires_auth` on each). The agent is additionally source-IP-gated in the LAN deploy.
- **Availability — never 500s.** The orchestrator proxy catches ALL agent errors (down/transport)
  → 503; a missing agent_client → 503; a missing launcher folder → `present:false` (200, not an
  error). The agent returns `present:false` for an absent folder rather than raising.
- **DoS bounded.** A single `iterdir` over one launcher folder (hundreds of entries in practice);
  no recursion, no unbounded work, no writes.
- **No injection.** `launcher` is validated against the allowlist before any use; no SQL, no shell,
  no format-string of untrusted data into a path beyond the sanitized single component.

## Decision
No findings. Ship. (Control-plane + agent; the agent already mounts `/lancache/lancache/cache ->
/data/cache`, so `manual_downloads_cache_path=/data/cache` needs no new mount. The Game_shelf
slug-diff + coverage report is a separate follow-up PR.)
