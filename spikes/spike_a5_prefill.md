# Spike A5 — Steam prefill download path (F5)

**Date:** 2026-05-29
**Goal:** Verify, against the live lancache host, exactly how the orchestrator
must issue chunk requests so they (a) route through lancache, (b) get cached,
and (c) are cached under the SAME key the F7 validator checks. Done before
writing any F5 code.

## The prefill request shape (VERIFIED)

```
GET http://<lancache>/depot/{depot_id}/chunk/{sha_hex}
User-Agent: Valve/Steam HTTP Client 1.0
Host: lancache.steamcontent.com
# no Range header
```

- The orchestrator deploys with `--network host` on the lancache box, so
  `<lancache>` is `http://127.0.0.1` (the monolithic container binds host :80).
- `User-Agent: Valve/Steam HTTP Client 1.0` makes lancache's nginx
  `$cacheidentifier` map resolve to **`steam`** (confirmed: requests logged as
  `[steam]`). The `Host` also matches the `*lancache.steamcontent.com` map arm.
- Body is **streamed and discarded** — lancache caches asynchronously; the
  orchestrator never writes chunk bytes to disk.

### Key alignment (the whole point)

A `steam`-identified, no-Range GET for `/depot/{id}/chunk/{sha}` is cached
under `md5("steam" + uri + "bytes=0-10485759")` → the path
`<root>/<H[-2:]>/<H[-4:-2]>/<H>` — **exactly** what F7 computes
(`spike_a4_lancache_cache_key.md`). So prefill populates precisely the keys F7
validates; prefill a game → F7 then reports it cached.

**Evidence:**
- Replaying a recent readable steam HIT
  (`/depot/2694491/chunk/602f51c2…`) with the headers above returned
  `200 OK`, `Content-Length: 1048656`, `X-LanCache-Processed-By: …`, and the
  access log recorded `200 … "HIT"`. Request shape serves from cache. ✓
- For depot 529345 chunk `c8e5d44c…` (spike A4's golden vector), lancache's
  `error.log` showed it locating the file at the **exact** path A4 computed
  (`/data/cache/cache/db/a0/22e7d56f787714bc78e23495d93da0db`) before failing
  to open it (see "mode-000" below) — proving the prefill request reproduces
  F7's key. ✓

### No manifest request code for chunk download

Chunk URLs (`/depot/{id}/chunk/{sha}`) are **unauthenticated** — confirmed by
the real-client access log (no token/query string; `"-"` http_range). The
manifest request code (5-min TTL) is only needed to fetch the *manifest*
(BL12 handles that); chunk downloads need none. This removes the
"MRC refresh mid-prefill" risk the FRD flagged.

## Chunk list source

Reuse F7's path: latest manifest per depot (`manifests` table, `depot_id IS
NOT NULL`) → worker `manifest.expand` → `chunk_shas`. Dedup by
`(depot_id, sha)`. The chunk URI is `/depot/{depot_id}/chunk/{sha}`. If the
game has no manifests yet, prefill must fetch them first (reuse the BL12
`manifest_fetch` path).

## Where it runs

Plain async HTTP in the **orchestrator** process (`httpx.AsyncClient`, already
a dependency via the ID2 lancache probe) — NOT the steam worker. No steam-next,
no gevent, no Steam session needed for the download itself (only the prior
manifest fetch needs auth). So prefill works even if the Steam session has
expired, as long as manifests are already stored.

## Live finding: ~1.7% of cache files are mode-000 (unreadable)

Sampling ~5000 cache files: ~74% mode `600` (www-data), ~24% mode `777`,
**~1.7% mode `000`**. Mode-000 files are unreadable even by lancache's own
`www-data` → `open() … failed (13: Permission denied)` in `error.log` → 500 +
re-download. Pre-existing (errors span days, predate the orchestrator);
not orchestrator-caused (read-only mount, stat-only).

**Consequence for F7:** `exists AND size>0` counts mode-000 files as cached,
but lancache can't serve them. F5 enhances F7 to also require the **owner-read
bit** (`st_mode & 0o400`). F7 runs as uid 1000 against `www-data:600` files so
it cannot `open()` them, but `stat()` returns `st_mode` (needs only directory
search perms, which work) — so check the bit, never open. mode-000 → bit
unset → not counted; mode-600/777 → bit set → counted. Operational finding
filed as issue #128.

## Residual risk (validate in F5 UAT)

The true MISS→upstream→cache path wasn't cleanly exercised (the spike's test
chunk hit an existing mode-000 file, so lancache didn't go upstream). The
upstream fetch on a genuine cache miss — and whether `Host:
lancache.steamcontent.com` routes upstream correctly — will be confirmed in
the F5 UAT by prefilling a game's *missing* chunks (e.g. Victoria 3's 5,635
missing) and re-validating to watch the cached count rise. `lancache_base_url`
and `steam_cdn_host` are Settings so the host can be adjusted if needed.
