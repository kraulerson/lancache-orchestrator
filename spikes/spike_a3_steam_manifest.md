# Spike A3 — steam-next 1.4.4 manifest API (pre-BL12)

**Date:** 2026-05-28
**Trigger:** F1 spec §6 (BL12) made assumptions about manifest serialization
(`pickle.dumps(manifest_object, protocol=5)`). Per the spike-A2 / UAT-6
lesson, validate against the actual library BEFORE implementing.
**Method:** Read steam-next 1.4.4 source directly (installed at
`/tmp/steam-spike2-venv` from spike-A2).
**Scope:** Identify the real APIs for fetching depot manifests, the
shape of the returned objects, and whether the spec's pickle approach
works against them.

## Findings

### F1 — Entry point is `CDNClient`, not `SteamClient`

`steam.client.cdn.CDNClient` is the public surface. Constructor takes
a logged-in `SteamClient` instance:

```python
from steam.client.cdn import CDNClient
cdn = CDNClient(steam_client)
```

Construction triggers:
- `load_licenses()` — reads `steam_client.licenses` (already validated
  in spike-A2; same dict we use for library enumeration)
- `fetch_content_servers()` — web API call to get CS server list

**Implication for BL12:** the worker should create the `CDNClient`
lazily on first manifest.fetch request (not at startup, not per-call).
Cache the instance for the worker's lifetime, like we do with the
SteamClient itself.

### F2 — High-level API: `cdn.get_manifests(app_id, branch='public')`

```python
def get_manifests(self, app_id, branch='public', password=None,
                  filter_func=None, decrypt=True):
    """Get a list of CDNDepotManifest for app
    :returns: list of CDNDepotManifest
    """
```

Returns ALL manifests for an app's depots in one call. Internally:
1. Fetches `get_app_depot_info(app_id)` — returns depot list per branch
2. For each non-shared depot, calls `get_manifest_request_code` + `get_manifest`
3. Returns the constructed `CDNDepotManifest` objects (with `.name` set)

**Implication for BL12:** This is the right API for "fetch all
manifests for game". Simpler than threading depot_id through the
IPC layer.

### F3 — Lower-level API: `cdn.get_manifest(app_id, depot_id, manifest_gid, ...)`

Requires knowing the manifest_gid in advance — typically from
`games.metadata.depots` (we already store depots there from BL11).
Use this when the operator wants to refresh a specific depot.

### F4 — `CDNDepotManifest` shape

```python
class CDNDepotManifest(DepotManifest):
    def __init__(self, cdn_client, app_id, data):
        self.cdn_client = cdn_client   # <-- reference!
        self.app_id = app_id
        DepotManifest.__init__(self, data)
```

Inherits from `DepotManifest`. After construction, the object has:
- `.app_id`, `.depot_id`, `.gid` (manifest_gid as int), `.name`
- `.metadata` (protobuf — creation_time, total_uncompressed, ...)
- `.payload.mappings` (protobuf — list of files with chunks)
- `.signature`, `.filenames_encrypted`
- `.cdn_client` — **back-reference to CDNClient**

**SPEC ASSUMPTION FAILS.** Spec §6.4 says:
```python
pickled = pickle.dumps(manifest_object, protocol=5)
```

This will fail because `CDNClient` is not picklable (holds a
`requests.Session`, a `GPool` thread pool, a `LRUCache`, etc.).

### F5 — Two viable serialization paths

**Option A — Custom `__reduce__` / detach cdn_client before pickle**

```python
mfst = next(iter(cdn.get_manifests(app_id)))
mfst.cdn_client = None  # break the back-reference
pickled = pickle.dumps(mfst, protocol=5)
```

Pros: preserves the `name` field and all protobuf state.
Cons: re-construction needs to know that cdn_client may be None; less
clear API contract.

**Option B — Store raw protobuf bytes via `.serialize()`**

`DepotManifest` has `.serialize()` returning the raw protobuf payload.
Store that; on read, reconstruct via `DepotManifest(data)`. Drops
the `.name` and `.app_id` (must store separately) but the wire shape
is well-defined.

```python
mfst = next(iter(cdn.get_manifests(app_id)))
data = mfst.serialize()  # bytes
# also stash: mfst.depot_id, mfst.gid, mfst.name, mfst.app_id
```

Pros: format is the canonical Steam protobuf — future-proof. Smaller
than pickle. Doesn't depend on Python pickle compatibility across
worker upgrades.
Cons: lose the `name` field unless we round-trip it via a separate
column (which we already have — `manifests.version` carries gid;
games table has the name).

**Decision: Option B.** Per spec D14 ("orchestrator NEVER unpickles")
+ the discovery that picklability is fragile, raw protobuf bytes are
the right abstraction. zstd compression on top, base64 over IPC,
BLOB in DB.

### F6 — `manifests` schema fit

Existing schema (`migrations/0001_initial.sql`):

```sql
CREATE TABLE manifests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       INTEGER NOT NULL REFERENCES games(id),
    version       TEXT NOT NULL,        -- steam manifest_gid as string
    fetched_at    TEXT NOT NULL,
    chunk_count   INTEGER NOT NULL,
    total_bytes   INTEGER NOT NULL,
    raw           BLOB NOT NULL,        -- the zstd-compressed protobuf
    UNIQUE(game_id, version)
);
```

`game_id` is the FK from games (which we look up via `(platform,
app_id)`). `version` carries the manifest_gid. UNIQUE constraint
deduplicates by (game_id, version) — re-fetching the same manifest
upserts the row.

**One depot manifest per row.** A multi-depot game produces multiple
rows. The IPC response is a list; handler iterates + upserts.

### F7 — `total_bytes` + `chunk_count` extraction

From CDNDepotManifest fields:
- `.metadata.creation_time` → could go into fetched_at
- `.metadata.total_uncompressed` → total_bytes
- Iterate `.payload.mappings` to count chunks across all files → chunk_count

These are protobuf accessors; we extract them in the worker (which
has steam-next available) and pass scalars over IPC.

### F8 — Anonymous/shared depots

Some apps have shared depots (multi-app). `get_manifests` handles the
filter; we don't need to worry about it. But the depot_id might
appear under multiple app_ids. Schema's UNIQUE is per (game_id, gid),
so multi-app sharing is correctly represented as multiple rows.

### F9 — Manifest size estimate

A typical AAA game manifest is 100 KB – 5 MB of protobuf,
compressing to roughly 30 KB – 1.5 MB with zstd level 3. The 128 MB
`manifest_size_cap_bytes` Settings field (from BL3) is the per-row
upper bound enforced by the handler — anything larger is rejected
as anomalous.

### F10 — get_manifest_request_code requires a network round-trip

The implementation calls `steam_client.send_um_and_wait(...)` for EACH
manifest before downloading it. For a game with 8 depots, that's 8
round-trips + 8 actual downloads. Total time can exceed 30 s easily —
**use the per-op timeout policy** the way library_sync does
(`steam_worker_manifest_fetch_timeout_sec` Settings field, default
300 s, range 30..3600).

## Plan implications

The F1 spec §6 was directionally right but the implementation details
need updating:

1. **Worker `manifest.fetch` IPC op** params: `{app_id: int}` (no
   depot_id — fetch all depots for the app in one IPC call)
2. **Worker response shape**: `{manifests: [{depot_id, manifest_gid,
   name, total_bytes, chunk_count, raw_b64}, ...]}`
3. **Serialization**: `data = mfst.serialize()` (raw protobuf),
   `compressed = zstd_level_3.compress(data)`,
   `raw_b64 = base64.b64encode(compressed)`
4. **Handler** in `jobs/handlers/manifest_fetch.py` iterates the list,
   upserts manifests by `(game_id, version)`, updates games.size_bytes
   to the SUM of total_bytes (or MAX — F1 spec §6.3 says max)
5. **Trigger endpoint** `POST /api/v1/games/{game_id}/manifest/fetch`
   (no body needed; could accept `?force=true` to skip dedup later)
6. **Per-op timeout**: new Settings field
   `steam_worker_manifest_fetch_timeout_sec` (default 300)
7. **NotAuthenticated handling**: mirrors library_sync — flips
   `platforms.auth_status='expired'` and re-raises

## Out of scope for BL12

- F5/F6 CDN prefill (uses the manifest's chunk info to actually
  download bytes through lancache)
- F7 validator deserialization path (orchestrator side; will land
  with F7 itself, deserializing the BLOB inside the worker)
- Branch / beta password support (`branch='public'` only for v1)
- Workshop items (`get_manifest_for_workshop_item` — separate API)

## Not validated until UAT-9

- Real manifest fetch against operator's Steam account
- `total_bytes` / `chunk_count` accuracy
- Per-op timeout sufficiency for largest games (Steam's biggest
  games can have 50+ depots; budget needs to cover serial fetch)
- BLOB size distribution + whether 128 MB cap is appropriate
