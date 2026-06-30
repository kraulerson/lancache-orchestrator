# Agent: Agent-RPC resilience sibling hunt (UAT-12)

No retry/backoff exists anywhere in the control-plane→agent HTTP layer (`_request`
does one `.request()` and wraps any `httpx.HTTPError` into `AgentError`; default
transport `retries=0`).

1. **SEV-2 — `steam_prefill` poll loop** (`agent_client.py` `_post_then_poll`, called from
   prefill.py): GET every 0.5s for up to 7200s; one ConnectTimeout/ReadTimeout/transport
   error on any poll (or the POST) → whole multi-hour Steam prefill fails. → **FIXED** (root).
2. **SEV-2 — Epic `pull` poll loop** (same `_post_then_poll`, called from `_epic_prefill_inner`):
   one transient blip fails the whole Epic prefill, flips game `failed`. → **FIXED** (same change).
3. **SEV-3 — `/health` `auth_status()` flaps `agent_reachable` + `cache_volume_mounted`** →
   a single connect blip makes `/health` return 503/degraded; no threshold/smoothing.
   → **Largely mitigated** by the connect-retry; full smoothing deferred.
4. **SEV-3 — `validate` dies on a transient blip** (`steam_validate`, no try/except up the
   stack); the post-prefill validate that assigns final status can leave a game stuck
   `downloading`. → **FIXED** (same `_request` retry).
5. **SEV-4 — `library_sync` dies on a transient blip** (`prefilled_apps`). Low impact
   (cheap, re-runs on schedule). → **FIXED** (same `_request` retry).

Already-safe: `sweep_handler` (per-game try/except), `validator_self_test` (catches → SKIP).

**Bottom line:** bounded retry on transient httpx errors in `_request` (or the poll GET)
covers #1, #2, #4, #5; #3 additionally wants a /health consecutive-failure threshold.
