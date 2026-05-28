# Spike A4 — Lancache cache-key derivation (F7 validator)

**Date:** 2026-05-28
**Goal:** Determine the EXACT on-disk cache-key formula and path layout
used by the live lancache `monolithic` deployment, so the F7 disk-stat
validator can compute, for a given Steam depot chunk, the precise file
path to `os.stat()`. Verified empirically against the running lancache
host (`karl@192.168.1.40`), not assumed from the FRD.

## Why a spike (not just the FRD)

`docs/phase-0/frd.md` §2.2.7 specifies the validator but contains **two
errors** that would make F7 report every chunk as missing:

1. **Slice size:** FRD assumes a **1 MiB** slice. The live lancache runs
   **`slice 10m;`** (10 MiB). The cache key embeds `$slice_range`, so a
   wrong slice size produces a wrong key → wrong path → false "missing".
2. **Levels path ordering:** FRD writes the path as
   `<H[28:30]>/<H[30:32]>/<H>`. The real `levels=2:2` layout is
   **`<H[30:32]>/<H[28:30]>/<H>`** (last-2 hex as the FIRST directory,
   prev-2 as the second). Reversed in the FRD.

Both were caught by reading the live nginx config and reverse-checking
real cached files. **The implementation follows the verified formula
below, not the FRD.** (Priority Hierarchy: Correctness over adherence to
a known-wrong spec.)

## Live nginx config (authoritative)

From `lancache_monolithic_1` (`lancachenet/monolithic:latest`):

```
# /etc/nginx/conf.d/20_proxy_cache_path.conf
proxy_cache_path /data/cache/cache levels=2:2 keys_zone=generic:2000m ... ;

# /etc/nginx/sites-available/cache.conf.d/root/20_cache.conf
slice 10m;
proxy_set_header  Range $slice_range;

# .../30_cache_key.conf
proxy_cache_key   $cacheidentifier$uri$slice_range;

# /etc/nginx/conf.d/30_maps.conf  (cacheidentifier map)
map "$http_user_agent£££$http_host" $cacheidentifier {
    default $http_host;
    ~Valve\/Steam\ HTTP\ Client\ 1\.0£££.*           steam;
    ~.*£££.*?lancache\.steamcontent\.com            steam;
    ...
}
```

Container mount: host `/lancache/lancache/cache` → container `/data/cache`.
So on the host the cache tree is `/lancache/lancache/cache/cache/...`;
inside the orchestrator container (read-only mount) it is
`/data/cache/cache/...` (matches `Settings.lancache_nginx_cache_path`).

## The formula (verified)

For a Steam depot chunk:

- **cacheidentifier** = `steam` (literal string — from the map above).
- **uri** = the nginx `$uri` (decoded path, no query string), which for a
  depot chunk is `/depot/{depot_id}/chunk/{chunk_sha_hex}`.
  - `chunk_sha_hex` = the chunk's 20-byte SHA-1 as 40 lowercase hex chars
    (steam-next `chunk.sha.hex()`).
- **slice_range** = `bytes=0-{slice-1}`. With `slice 10m`,
  slice 0 = `bytes=0-10485759`. Steam depot chunks are ≤ ~1 MiB, fetched
  whole with **no client Range header**, so each chunk is always exactly
  **one slice (slice 0)**. F7 never needs multi-slice expansion per chunk.
- **key** = `cacheidentifier + uri + slice_range` (no separators).
- **H** = `md5(key)` as 32 lowercase hex chars.
- **path** = `{cache_root}/{H[30:32]}/{H[28:30]}/{H}` for `levels=2:2`.

`cache_root`, slice size, and levels all come from `Settings`
(`lancache_nginx_cache_path`, `cache_slice_size_bytes`, `cache_levels`),
whose defaults already match this deployment.

### nginx `levels` generalization

For `levels=L1:L2:...:Ln`, nginx consumes hex chars from the **end** of
H. The last level uses the final `Ln` chars; the next dir uses the `Ln-1`
chars immediately before, etc. For `2:2`: dir1 = `H[-2:]`, dir2 =
`H[-4:-2]`. F7 implements the general algorithm so a deployment using a
different `levels` string still works.

## Empirical verification

Three real Steam HIT chunks taken from the live `access.log`
(`[steam] ... "GET /depot/529345/chunk/<sha> HTTP/1.1" 200 ... "HIT"`),
each fed through `md5("steam"+uri+"bytes=0-10485759")` and the 2:2 path —
**all three matched an existing non-empty cache file**:

| uri (depot 529345) | H = md5 | file size |
|---|---|---|
| `/chunk/c8e5d44c…54ff` | `22e7d56f787714bc78e23495d93da0db` | 498283 |
| `/chunk/234a47ed…5bd79` | `c083a3b195ee7992b4df83b4488a9791` | 462311 |
| `/chunk/dbff8764…f15e` | `cccaab923f4242ac691d701331a26129` | 1049580 |

The empty slice (`""`) and 1 MiB slice (`bytes=0-1048575`) variants both
**missed** for all three — confirming the 10 MiB slice is required.

### Presence check, not size match

The cached file size (e.g. 498283) is **larger** than the logged
`body_bytes_sent` (497360) because nginx prepends its cache-entry header
(status line, upstream headers, key) before the body. Therefore F7 tests
**file exists AND `st_size > 0`** — it must NOT compare against the
manifest's chunk size.

## Implications for F7 design

- **Cache-key derivation is pure + offline** — no network, no auth. Lives
  in the orchestrator process (`validator/cache_key.py`).
- **Manifest deserialization stays in the worker venv** (ADR-0013 D14):
  a `manifest.expand` IPC op takes the stored `base64(zstd(protobuf))`
  BLOB, reconstructs via `DepotManifest(zstd.decompress(...))`, and
  returns `{depot_id, chunk_shas: [hex, ...]}`. **No Steam session
  required** — so F7 validation works even when the operator's auth has
  expired (unlike the BL12 fetch).
- **stat() loop runs in the orchestrator** over the read-only cache mount,
  batched via `loop.run_in_executor` to keep the event loop responsive.
- **Dedup chunk SHAs** per depot before stat — the same chunk SHA can
  appear in multiple file mappings (Steam content dedup); in the cache it
  is one file (keyed by URL = by SHA).
- **BL12 gap:** `manifests` rows store `version` (= manifest_gid) but NOT
  `depot_id`, which F7 needs to build the chunk URL and to pick the latest
  manifest per depot. F7 adds a `depot_id` column (migration 0003) and
  updates the BL12 handler to populate it. No backfill needed — no live
  manifest data exists yet (BL12 live fetch awaits UAT).

## Path-traversal safety

`depot_id` and `chunk_sha_hex` are interpolated into a filesystem path.
F7 validates `depot_id` is a non-negative int and `chunk_sha_hex` matches
`^[0-9a-f]{40}$` before building any path. The computed path is also
confirmed to resolve under `lancache_nginx_cache_path` (no `..`, no
absolute escape) — defense in depth even though md5 hex can't contain
traversal chars.
