# ADR-0014: Epic prefill — pure-Python manifest parsing (no vendored legendary)

**Status:** Accepted (F6, 2026-06-03)
**Context:** F6 (Epic CDN Prefill) needs to authenticate to Epic, enumerate the
owned library, fetch + parse Epic's binary manifests, and download chunks through
the lancache. The Phase-0 Product Manifesto described F6 as "Same pattern as F5
using **vendored legendary modules** + pinned preferred CDN host." `legendary`
(the open-source Epic Games launcher CLI) is GPL-3.0 and pulls a substantial
dependency surface.

## Decision

Implement the entire Epic stack in **pure Python** (`httpx` + stdlib
`struct`/`zlib`/`base64`) in `src/orchestrator/platform/epic/`, **not** by
vendoring `legendary` modules. Epic OAuth, library enumeration, the v2 manifest
API, the binary-manifest parser, and the version-aware chunk-path construction
are all reimplemented from the protocol. This was de-risked end-to-end by
`spikes/spike_b_epic_prefill.py` (PASS, Milestone A), which proved auth → manifest
parse → chunk download → cache-HIT works with httpx alone.

This is a **deliberate deviation** from the Manifesto's F6 wording, recorded here.

## Consequences

- **No `legendary` runtime dependency**, no GPL-3.0 vendoring obligation, and — unlike
  Steam (ADR-0013) — **no gevent / subprocess-worker isolation**. The whole Epic
  pipeline runs as async httpx in the orchestrator process.
- **We own and must maintain the binary manifest parser** (`platform/epic/manifest.py`).
  Risk: Epic's manifest format can change (the version field gates `ChunksV2..V5` and
  the v22+ base64 chunk-name scheme). Mitigation: the parser is small, hard-tested
  against synthetic golden manifests, raises `EpicManifestError` (never `sys.exit` /
  silent truncation), and bounds `chunk_count` against a DoS value.
- **Validation:** F6 ships sample cache-HIT verification (`X-Upstream-Cache-Status:
  HIT`), spike-B-proven. The F7-Epic disk-stat validator is a deferred follow-up — the
  Epic on-disk cache-key (the A4-equivalent) can only be derived from real cached Epic
  chunks, so it waits for the first live Epic prefill. `validator/cache_key.epic_chunk_uri`
  is staged for that follow-up.
- **Credentials:** the Epic OAuth `client_id`/`client_secret` are the well-known public
  legendary launcher credentials (not operator secrets); the per-user refresh token is
  persisted 0600 at `epic_session_path` and never logged.
