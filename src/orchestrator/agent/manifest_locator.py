"""Locate an app's current manifest .bin files in SteamPrefill's cache.

SteamPrefill records what it prefilled in Config/successfullyDownloadedDepots.json
({app_id_str: [manifest_gid_ints]}) and caches each manifest as
<cache_root>/v1/{app}_{app}_{depot}_{gid}.bin. We pick the .bin for each gid the
app prefilled (the current per-depot manifests).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def locate_manifest_bins(app_id: int, *, cache_root: Path, config_dir: Path) -> list[Path]:
    downloaded_path = config_dir / "successfullyDownloadedDepots.json"
    if not downloaded_path.exists():
        return []
    try:
        downloaded = json.loads(downloaded_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    gids = downloaded.get(str(app_id))
    if not gids:
        return []
    v1 = cache_root / "v1"
    found: list[Path] = []
    for gid in gids:
        # filename is {app}_{app}_{depot}_{gid}.bin; depot is unknown here, glob by gid.
        matches = list(v1.glob(f"{app_id}_{app_id}_*_{gid}.bin"))
        found.extend(matches)
    return found
