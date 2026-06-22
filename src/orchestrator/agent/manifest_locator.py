"""Locate an app's current manifest .bin files in SteamPrefill's cache.

SteamPrefill caches each depot manifest as
<cache_root>/v1/{app}_{app}_{depot}_{gid}.bin. For an app we take the NEWEST
.bin (by mtime) per depot — the most recently fetched manifest, which tracks
what is currently prefilled into lancache.

This deliberately does NOT use Config/successfullyDownloadedDepots.json: that
file proved unreliable as a manifest index (it omits apps that have cached
manifests and lists only a subset of an app's depot gids — found live during
the ③a gate). The .bin cache itself is the source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def locate_manifest_bins(app_id: int, *, cache_root: Path) -> list[Path]:
    """Return the newest manifest .bin per depot for ``app_id`` (empty if none)."""
    v1 = cache_root / "v1"
    if not v1.is_dir():
        return []
    newest_per_depot: dict[str, Path] = {}
    for path in v1.glob(f"{app_id}_{app_id}_*.bin"):
        parts = path.stem.split("_")
        if len(parts) != 4:
            continue
        depot = parts[2]
        current = newest_per_depot.get(depot)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            newest_per_depot[depot] = path
    return list(newest_per_depot.values())
