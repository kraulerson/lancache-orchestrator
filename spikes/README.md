# Build Milestone A — Spikes

Exploration harnesses for ADR-0001. Each spike proves one architectural assumption
before feature construction begins in Build Milestone B. Run on DXP4800 unless noted.

## Execution Order (approved)

| Order | Spike | What it proves | Hardware required |
|-------|-------|---------------|-------------------|
| 1 | **A** — Steam prefill | steam-next auth + httpx chunk download through Lancache with correct Host/UA headers | DXP4800 + Lancache |
| 2 | **D** — gevent bridge | gevent-patched sockets coexist with asyncio event loop without deadlock | Any (has `--mock` mode) |
| 3 | **C** — Validator | Disk-stat cache path formula matches actual Lancache nginx cache layout | DXP4800 + Lancache cache volume |
| 4 | **B** — Epic prefill | Epic OAuth + binary manifest parse + httpx chunk download through Lancache | DXP4800 + Lancache |
| 5 | **E** — E2E topology | Container reaches Lancache via compose network; Game_shelf reaches REST API | DXP4800 full compose stack |
| 6 | **F** — Load test (HARD GATE) | p99 /health < 100ms under 32 concurrent chunk downloads at >= 300 Mbps for 10 min | DXP4800 production-like |

Spikes E and F harnesses will be written after A-D results are in.

## Setup

```bash
# Create a separate venv for spike scripts (do NOT mix with project deps)
python -m venv .venv-spikes
source .venv-spikes/bin/activate
pip install -r spikes/requirements-spikes.txt
```

## Running

```bash
# Spike A — Steam prefill through Lancache
python spikes/spike_a_steam_prefill.py --app-id 228980 --lancache-host <LANCACHE_IP>

# Spike D — gevent/asyncio bridge (mock mode, no Steam credentials needed)
python spikes/spike_d_gevent_bridge.py --mock --duration 60

# Spike D — gevent/asyncio bridge (live mode, requires Steam credentials)
python spikes/spike_d_gevent_bridge.py --duration 120 --concurrent-tasks 20

# Spike C — cache path validator (requires cached chunks from Spike A)
python spikes/spike_c_validator.py --cache-root /path/to/lancache/cache \
    --depot-id <DEPOT_FROM_SPIKE_A> --chunk-sha <SHA_FROM_SPIKE_A>

# Spike C — scan entire cache directory
python spikes/spike_c_validator.py --cache-root /path/to/lancache/cache --scan-dir

# Spike B — Epic prefill through Lancache
python spikes/spike_b_epic_prefill.py --lancache-host <LANCACHE_IP>
```

## Key Discoveries (from research)

1. **Steam User-Agent is critical**: Lancache uses `User-Agent: Valve/Steam HTTP Client 1.0`
   to map the cache identifier to `"steam"`. Without this exact UA, cache entries won't match
   what the real Steam client creates. Spike A sets this header.

2. **Lancache cache key**: `$cacheidentifier$uri$slice_range` where slice = 1 MiB.
   Disk path: `md5(key)` with nginx `levels=2:2` directory mapping.

3. **Epic HTTP by design**: Epic's client uses plaintext HTTP for CDN downloads specifically
   to enable DNS-based Lancache redirection (`disable_https` flag).

4. **gevent is process-global**: `steam.monkey.patch_minimal()` patches socket/ssl/dns for the
   entire process. Spike D tests whether asyncio survives this.

## Results

Archive each spike's output to `docs/test-results/YYYY-MM-DD_spike-X_pass|fail.md`.
Spike F result triggers ADR-0005 (Option A vs Option B final commitment).
