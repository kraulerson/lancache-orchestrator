# Spike B Result — Epic Games Auth + Lancache Chunk Download

**Date:** 2026-04-21
**Result:** PASS
**Environment:** Mac Mini (macOS) → Lancache at 192.168.1.40

## What was tested

Full Epic pipeline: OAuth auth code exchange, library enumeration, manifest
download with signed query params, binary manifest parsing, chunk download
through Lancache with correct Host header, cache HIT verification on second pass.

## Results

```
Auth:           PASS
Manifest parse: PASS  (version 17, 309 chunks, 237.9 MB total)
Pass 1 (MISS):  PASS  (avg 48ms)
Pass 2 (HIT):   PASS  (avg 9ms)

CDN hostname:   download.epicgames.com
Cache identifier: epicgames (via hostname map in 30_maps.conf)

OVERALL: [PASS]
```

## Key findings

- **Library API:** Use `library-service.live.use1a.on.epicgames.com/library/api/public/items`
  (paginated with cursor), NOT the deprecated `/assets/v2/platform/{platform}` listing.
- **Manifest URL:** Fetch via `/launcher/api/public/assets/v2/platform/{platform}/namespace/{ns}/catalogItem/{cat}/app/{app}/label/Live`.
  Response includes `queryParams` array — must append as `?name=value&...` to URI for CDN auth.
- **Chunk GUID format:** Four little-endian uint32s formatted as hex without dashes
  (e.g. `347DBA354E64443AB71704AFB08DA85A`), NOT UUID format with dashes.
- **Chunk directory version:** `ChunksV3` (manifest v6-14), `ChunksV4` (v15-21), `ChunksV5` (v22+).
- **User-Agent:** `EpicGamesLauncher/11.0.1-14907503+++Portal+Release-Live` (matches real launcher).
- **CDN hostnames:** Multiple in Lancache hostname map — `download.epicgames.com`,
  `egdownload.fastly-edge.com`, `epicgames-download1.akamaized.net`, etc. All map to `epicgames`.
- **Auth codes:** One-time use, expire within ~30 seconds. Must paste immediately.

## Bugs encountered and fixed

1. Deprecated assets API → switched to library + per-game manifest endpoints
2. Missing queryParams on manifest URL → 403 from CDN
3. GUID formatted as UUID with dashes → S3 path not found (403)
