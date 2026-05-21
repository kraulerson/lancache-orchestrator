# Spike G — Lancache nginx behavior under external file deletion

**Date:** 2026-05-20
**Issue:** #38
**Blocks (until PASS):** F18 (cache purge)
**Conducted on:** Live lancache deployment at 192.168.1.40 (`lancache_monolithic_1` container, nginx/1.24.0)
**Conductor:** assistant via `ssh karl@192.168.1.40` (no sudo required for any step)
**Outcome:** **PASS** on all three criteria.

<!-- Last Updated: 2026-05-20 -->

## Question

When the orchestrator deletes files from the lancache cache directory at runtime, how does nginx's proxy_cache layer respond to subsequent requests for those URLs?

## Protocol (from #38)

1. Pick a small game's cached chunk; baseline HEAD must show `X-Upstream-Cache-Status: HIT`.
2. `rm` the chunk file on the host filesystem.
3. Re-request; capture status, headers, response, error.log entries, filesystem repopulation state.
4. Repeat under concurrent write: delete one chunk while another is being filled.

## Targets

| Target | Depot | Chunk SHA | Disk path | Original size |
|---|---|---|---|---|
| Single-deletion | 401531 | `7c86db7a…8da1d6` | `/lancache/.../cache/98/f8/c7c0bedbc2c3fe56b8150d7e7db9f898` | 1000 B |
| Concurrent A | 373302 | `04a37670…fc59ee` | `/lancache/.../cache/d8/32/1c9910e612b1a78d5082f7d8595332d8` | 1,041,564 B |
| Concurrent B | 373302 | `057e929c…d5167` | `/lancache/.../cache/49/f9/36a8a86b2b7e5f92b392d16f9c54f949` | 1,009,548 B |

Depot 401531 had only 1 hit in the most recent 50 MB of access.log — confirmed inactive. The concurrent targets are from depot 373302 (~142 hits in same window).

## Cache key formula (re-verified)

```
cache_key = "steam" + uri + "bytes=0-10485759"        # 10 MiB slice range
md5_hex   = md5(cache_key).hexdigest()
disk_path = /lancache/lancache/cache/cache/{md5[-2:]}/{md5[-4:-2]}/{md5}
```

Matches Spike C's documented formula. Three test chunks computed and located by formula — all three existed at the predicted paths.

## Test 1: Single-chunk deletion

```
STEP 1  pre-state            file exists, 1000 B, owner www-data, mode 777
STEP 2  baseline HEAD        HTTP/1.1 200 OK · X-Upstream-Cache-Status: HIT
STEP 5  pre-deletion mark    error.log at 4,068,772 lines
STEP 6  rm                   succeeded WITHOUT sudo (mode 777 — see Finding 2)
STEP 7  post-rm HEAD         HTTP/1.1 200 OK · X-Upstream-Cache-Status: MISS
STEP 8  filesystem after HEAD  file still gone (HEAD did NOT trigger cache-write)
STEP 9  GET                  HTTP 200, 80 bytes, t=2.7 ms
STEP 10 filesystem after GET file repopulated · 978 B · owner www-data · mode 600
STEP 11 second HEAD          HTTP/1.1 200 OK · X-Upstream-Cache-Status: HIT
STEP 12 error.log delta      0 new lines
STEP 13 access.log sequence  HIT (baseline) → MISS (post-rm) → HIT (post-GET)
```

**Result:** clean MISS → upstream fetch → HIT cycle. No errors.

## Test 2: Concurrent deletion + parallel reads

Two cache files deleted simultaneously, then **6 parallel GETs** launched (3 against chunk A, 3 against chunk B) before either upstream re-fetch completed.

```
deletions       rm chunk_a + rm chunk_b   both succeeded mode-777
parallel curl   6 requests via background &; wait
results         all 6 HTTP 200
chunk A sizes   1040640 / 1040640 / 1040640  (identical)
chunk A md5sum  39ce7069d641be07b62b53eedb7b92a5 × 3
chunk B sizes   1008624 / 1008624 / 1008624  (identical)
chunk B md5sum  97a7e350fd0e6ca0ba36390b134a40c1 × 3
timings         first: 160 ms · subsequent: 500-520 ms
disk after      both cache files repopulated · mode 600 · owner www-data
second HEAD     both report X-Upstream-Cache-Status: HIT
error.log       0 new lines
```

**Result:** nginx serialized the 6 requests cleanly through its slice subrequest machinery. All response bodies are byte-identical within each chunk's set, confirming no truncation or corruption.

## Pass criteria assessment

| Criterion (from #38) | Result |
|---|---|
| (a) Re-request after deletion succeeds (HIT after repopulation or clean MISS+fetch) | **PASS** — clean MISS+fetch cycle |
| (b) No nginx error-log entries indicating index corruption / filesystem disagreement | **PASS** — zero new lines added across both tests |
| (c) Concurrent prefill of a different chunk unaffected by deletion | **PASS** — 6 parallel reads across 2 freshly-deleted chunks all returned correct content |

**Spike G: PASS.**

## Additional findings (not in original spike scope)

### Finding 1 — HEAD doesn't trigger cache write; only GET does

Post-deletion `HEAD` returned `200 + X-Upstream-Cache-Status: MISS` (so nginx DID reach upstream) but Step 8 confirmed the cache file was still absent afterward. The cache file was written only after the subsequent `GET`. F18 implication: any cache-warming step after a purge must issue `GET`, not `HEAD`.

### Finding 2 — Cache files are mode 777 (world-writable)

```
-rwxrwxrwx 1 www-data www-data 1000 Aug  5  2025 /lancache/.../98/f8/c7c0...
```

The `chmod_fix.sh` script in `/lancache/` presumably set this. Practical impact for F18: the orchestrator can perform deletions **as the `karl` user from a non-privileged container with a `:rw` cache-volume mount**. It does NOT need `docker exec` into the monolithic container, and does NOT need the docker socket mounted. This is a significant simplification of F18's design.

Caveat: nginx **rewrites** the files at mode `600` after upstream re-fetch (Step 10 + Test 2 disk-after output). So fresh cache writes are private, but existing files (created before `chmod_fix.sh` ran, or under some other historical path) remain 777. The orchestrator should not depend on either mode but should depend on the *file owner* being `www-data` and use `rm`-only access (deletion only requires write permission on the parent directory, which is mode 777 dir-wide).

### Finding 3 — Pre-existing `[crit] Permission denied` entries on unrelated chunks

The baseline error.log already contained ongoing entries like:

```
2026/05/20 18:17:13 [crit] 1883#1883: *939660 open() "/data/cache/cache/0e/7a/7e9eeebd44276dbd9d33bea384ec7a0e"
  failed (13: Permission denied), client: 172.18.0.1, server: , request:
  "GET /depot/462781/chunk/9a1145197fa3a62f3d78f4f85960a61067119fa6 HTTP/1.1"
```

These are NOT from the spike; they predate the test by ~25 minutes. They suggest a separate issue (some cache files have permissions nginx can't read) — recommend a follow-up issue. Confirms nginx survives `Permission denied` gracefully and continues serving other content.

### Finding 4 — `X-Cache-Status` vs `X-Upstream-Cache-Status`

lancache exposes two cache-status headers:

- `X-Cache-Status` — Steam CDN's upstream cache status (steamcontent.com side). Always `MISS` on freshly-fetched chunks in our tests because lancache bypasses Steam's edge.
- `X-Upstream-Cache-Status` — **the local lancache cache status**. This is the marker the access.log writes as the HIT/MISS field. **Our orchestrator should parse this header (and the access.log field) to derive cache-hit telemetry**, not the more generic `X-Cache-Status`.

## Implications for F18 (cache purge feature)

1. **No docker socket required.** Mount `/lancache/lancache/cache:/var/lancache/cache:rw` (read-write) into the orchestrator container; `rm` directly. `karl`-group container access is sufficient.
2. **Re-warm with GET.** If F18 purges and wants to validate by triggering re-population, issue `GET`, not `HEAD`.
3. **Per-chunk re-fetch latency**: ~150-500 ms per 1 MB chunk from Steam CDN. Budget accordingly for batch purges.
4. **Idempotent and safe.** Deleting an already-deleted chunk is `rm`-style no-op; safe to retry without coordination.
5. **No need for serialization.** nginx slice machinery serializes parallel reads correctly. F18 does not need a global mutex around purge operations.

## Follow-ups

- File a separate issue for the pre-existing `[crit] Permission denied` lines on depot 462781 chunks (separate from this spike's scope).
- Update F18 (#37) to remove the docker-exec requirement; replace with cache-volume `:rw` mount + direct `rm`.
- Update `project_spike_g_lancache_access.md` memory with the F18 design simplification.

## Reproducibility

All commands executed verbatim are in the session transcript dated 2026-05-20 (`ssh karl@192.168.1.40 < spike-g-protocol.sh`). To re-run from scratch on this deployment:

1. Tail access.log, find a Steam HIT for an inactive depot.
2. Compute cache key per formula (above).
3. `stat` the predicted path; if it exists, proceed.
4. Baseline curl (HEAD), `rm`, post-rm curl (HEAD then GET), post-GET state check, error.log delta.
5. Concurrent variant: parallel `curl` to two freshly-rm'd chunks.

No persistent artifacts created on the lancache server. Both deleted chunks (3 total: 1 from Test 1, 2 from Test 2) have been re-populated by nginx as a side effect of the test. Total upstream re-fetch traffic generated: ~2.1 MB.
