# Spike A Result — Steam Auth + Lancache Chunk Download

**Date:** 2026-04-21
**Result:** PASS
**Environment:** Lancache server at 192.168.1.40 (Ubuntu 24.04, Python 3.12.3)

## What was tested

Full Steam pipeline: authenticate with Steam via steam-next, retrieve depot
manifests via CDNClient, download chunks through Lancache with correct Host
header and User-Agent, verify second-pass cache HITs.

## Results

```
App ID:  228980 (Steamworks Common Redistributables)
Depot:   228981
Chunks:  5

Chunk SHA (short)    P1 Status  P1 Cache  P2 Status  P2 Cache
-----------------------------------------------------------------
652b6c9b4aa15a25...  OK         MISS      OK         HIT
d95d63cf994fb955...  OK         MISS      OK         HIT
94cc03eb2863cbb3...  OK         MISS      OK         HIT
ad6da26eb06e8bc7...  OK         MISS      OK         HIT
db7a5b02746d8ac7...  OK         MISS      OK         HIT

Pass 1 timing: min=101ms, max=271ms, avg=195ms
Pass 2 timing: min=6ms, max=13ms, avg=9ms

OVERALL: [PASS]
```

## Key findings

- **2FA handling:** `login()` returns `EResult.AccountLoginDeniedNeedTwoFactor`
  before firing the `auth_code_required` event. Must check EResult and retry
  with `two_factor_code` parameter directly.
- **steam-next 1.4.4 manifest format:** `depot_info['manifests']['public']`
  returns `{'gid': '...', 'size': '...'}` (dict), not a plain GID string.
  Requires monkey-patching `get_app_depot_info()` to normalize.
- **Cache performance:** Pass 2 (HIT) is ~20x faster than Pass 1 (MISS).
- **Host header:** `lancache.steamcontent.com` required for cache identifier match.
- **User-Agent:** `Valve/Steam HTTP Client 1.0` required for Steam cache identifier.

## Implications for production

- The orchestrator's prefill worker can use `httpx.AsyncClient` with the correct
  Host header and User-Agent to download chunks through Lancache.
- steam-next 1.4.4 needs the GID normalization patch — pin the version and
  include the patch in the production steam worker.
- 2FA must be handled at the orchestrator level (initial setup/credential storage).
