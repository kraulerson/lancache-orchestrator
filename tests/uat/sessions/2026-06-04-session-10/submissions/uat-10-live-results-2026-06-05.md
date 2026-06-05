# UAT-10 — Live Results (F5 Steam prefill + F6 Epic prefill)

**Date:** 2026-06-05
**Tester:** Karl (auth) + agent-driven (deploy, prefill, verify over SSH)
**Build:** `git_sha=474380a` (main, post-#139 remediation) — dockerized on the lancache host (`orchestrator-uat10`, `--network host`, bound `127.0.0.1:8765`, cache mounted read-only).
**Outcome:** **PASS** — both prefill pipelines proven end-to-end against real Steam + Epic accounts and the real lancache. The remediated code (PR #139) ran live with no regressions; the F5 MISS path and F6 full stack — neither previously live-tested — both work.

## Deploy / health
`/health` → `200`, `git_sha=474380a`, `scheduler_running`, `lancache_reachable`, `cache_volume_mounted`, `validator_healthy` all true. ✅

## F6 — Epic (full PASS)
- OAuth: real `legendary.gl/epiclogin` code → `202`, account **Kraulerson**, `auth_status=ok`; **no token echoed** in response or logs (`epic_auth.authenticated` carries only `account_id`). Refresh token persisted.
- Library: `enumerate.returned app_count=694` → **663 games** upserted (`platform=epic`).
- **Manifest probe (16 titles):** 14 real binary manifests fetched + parsed (chunk counts 158–65 292, sizes 127 MB–62 GB); 2 unowned/DLC → clean `EpicManifestError` (HTTP 403, no crash). Real CDN hosts `egs-cloudfront-chunks.epicgamescdn.com` / `egdownload.fastly-edge.com` **both pass the FQDN/SSRF guard** (UAT-10 #4). No real manifest false-tripped the decompression cap (#1).
- **Prefill via the real endpoint (#5):** `POST /games/8/prefill` → `202`, `platform=epic` (was 400 pre-fix). Game 8 = **Turaco**, v17, 158 chunks, `depot_id=NULL`, `raw=13398B`.
- Download + verify: 158/158 chunks through the lancache, **`hit_ratio=1.0`** → `status=up_to_date`, `size_bytes=133,764,890`.

## F5 — Steam (full PASS)
- Auth: username/password → `202` (`challenge_type=mobile_authenticator`) → Steam Guard code → `200`, `auth_status=ok`. (Earlier failures were a placeholder/typo in the manual curl, not a product issue.)
- Library: `enumerate.returned app_count=2461` → **2461 games** upserted (readable titles).
- Manifest fetch probe (6 small candidates): dedicated-server/editor tools return 0 chunks (no owned depots); Source SDK = smallest real game.
- **Prefill (#5 sibling / F5 MISS path, first live test):** `POST /games/703/prefill` (Source SDK, ~2 GB) → `prefill.completed ok=2430/2430 failed=0` in ~42 s → chained validate enqueued.
- **F7 validate (on-disk):** `outcome=cached`, **chunks_cached=2430 / missing=0** → `status=up_to_date`, `size_bytes=2,173,258,931`. 100% of prefilled chunks confirmed on disk in the lancache (re-confirms the cache-key formula).

## Remediation re-confirmed live (PR #139)
- **#5** Epic prefill trigger works via the real API endpoint (202, not 400).
- **#4** SSRF FQDN guard accepts real Epic CDN hosts (no false rejection) and the manifest fetch validates before the GET.
- **#1** real (compressed) Epic manifests parse fine under the bounded decompressor.
- **#7** real unowned-title 403s surface as clean `EpicManifestError`, no raw traceback.

## New observations (minor, non-blocking — filed as follow-ups)
1. **Epic `title` not resolved to a store title** — the library enumerate stores `app_name` as the title (sometimes a readable slug like `Turaco`, often a hex GUID). Cosmetic; resolving display titles needs a separate Epic catalog lookup.
2. **No `GET /api/v1/games/{id}` detail route** — `games.py` is list-only; a by-id fetch 404s. Minor API-completeness gap (the sub-resource triggers `/games/{id}/prefill` etc. do exist).

Neither is SEV-1/2. No must-fix bugs surfaced live → no further remediation required for UAT-10.

## Notes
- The Steam password was inadvertently pasted in plaintext during auth → recommended Karl rotate it post-UAT.
- `orchestrator-uat10` container left running on the host (holds Karl's Steam session + Epic refresh token in its named volume) pending teardown decision.
