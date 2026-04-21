# Spike C Result — Cache Path Validator

**Date:** 2026-04-20
**Result:** PASS
**Environment:** Lancache server at 192.168.1.40 (Ubuntu 24.04, Docker 27.5.1)

## What was tested

The orchestrator's cache path computation formula was validated against real
Lancache nginx cache files on disk.

## Formula verified

```
cache_key = $cacheidentifier + $uri + $slice_range
disk_path = $cache_root / md5(cache_key)[-2:] / md5(cache_key)[-4:-2] / md5(cache_key)
```

## Deployment-specific parameters discovered

- **Slice size:** 10 MiB (`CACHE_SLICE_SIZE=10m` in .env), NOT the 1 MiB default
- **Cache root (host):** `/lancache/lancache/cache/cache/`
- **Cache root (container):** `/data/cache/cache/`
- **levels:** 2:2
- **Steam cache identifier:** `"steam"` (via UA match OR `lancache.steamcontent.com` host match)
- **Epic cache identifier:** `"epicgames"` (via hostname map in 30_maps.conf)

## Test results

### Single URI verification
```
Cache key: steam/depot/292732/chunk/d3320b3718cea87ecf790ef29eb09ee6342fce0ebytes=0-10485759
MD5:       304f9746b57b02228e64a57a8d283b3b
Path:      /lancache/lancache/cache/cache/3b/3b/304f9746b57b02228e64a57a8d283b3b
Result:    [OK] Extracted KEY matches computed key
```

### Batch validation (20 random files)
```
Sampled: 20 files, 20 pass, 0 fail
Formula: md5(cache_key) -> filename  [levels=2:2 directory structure]
[OK] All sampled files match the cache key formula
```

## Implications for production F7 validator

The `compute_cache_path()` function from the spike script can be lifted directly
into `src/orchestrator/validator/`. The slice size must be configurable (read from
Lancache .env or passed as a config parameter) since deployments can override it.
