# Spike D Result — Gevent/Asyncio Bridge (Mock + Real httpx)

**Date:** 2026-04-20
**Result:** PASS
**Environment:** Lancache server (Ubuntu 24.04, Python 3.12.3) + Mac Mini (macOS)

## What was tested

ADR-0001's "three work zones" architecture: can gevent-patched sockets
(`steam.monkey.patch_minimal()`) coexist with asyncio's event loop and httpx
AsyncClient without deadlocking?

## Mock mode results (on lancache server)

```
Phase 1 (Auth):              [OK]  503ms, no deadlock
Phase 2 (15 concurrent):     [OK]  15/15 completed, 0 deadlocked
Phase 3 (30s mixed workload):[OK]  147 ops, 0 deadlocked, avg=204ms p95=209ms
Phase 4 (Cleanup):           [OK]  102ms
OVERALL: [PASS]
```

## Real httpx + gevent test (from Mac → Lancache)

```
10 requests: all HTTP 200, all cache HIT
Latency: avg=19ms  p95=83ms  min=11ms  max=83ms
[OK] httpx AsyncClient + gevent monkey_patch + real Lancache = WORKS
```

## Key finding

`steam.monkey.patch_minimal()` patches socket, ssl, and dns at the process
level. Despite this, asyncio's selector-based event loop continues to work
correctly, and httpx AsyncClient can make real HTTP connections that return
valid responses. The gevent hub and asyncio event loop do not interfere.

## Remaining: Live Steam test

Mock mode validates the threading/deadlock behavior. Live mode (actual
SteamClient auth + CDNClient manifest retrieval in the gevent executor thread)
requires interactive Steam credentials. To be tested when Spike A runs.
