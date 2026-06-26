"""Locate an app's current manifest files in the Steam manifest cache.

Two manifest formats live side by side under <cache_root>/v1/, both named
{app}_{app}_{depot}_{gid}.<ext>:
  * .bin   — SteamPrefill's protobuf manifest (what SteamPrefill prefilled).
  * .shas  — sidecar chunk list (one lowercase 40-hex SHA1 per line) written by
             the independent fetcher, covering apps SteamPrefill never cached.
For an app we take the NEWEST file (by mtime) per depot regardless of extension
— the most recently fetched manifest, which tracks what is currently prefilled
into lancache. A .bin and a .shas for the same depot de-dupe to whichever is
newer.

This deliberately does NOT use Config/successfullyDownloadedDepots.json: that
file proved unreliable as a manifest index (it omits apps that have cached
manifests and lists only a subset of an app's depot gids — found live during
the ③a gate). The manifest cache itself is the source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Manifest filename extensions the locator recognizes (see module docstring).
_MANIFEST_EXTS = ("bin", "shas")


def list_prefilled_app_ids(*, cache_roots: list[Path]) -> list[int]:
    """Distinct app_ids with a cached manifest (.bin or .shas) across all roots."""
    apps: set[int] = set()
    for root in cache_roots:
        v1 = root / "v1"
        if not v1.is_dir():
            continue
        for ext in _MANIFEST_EXTS:
            for path in v1.glob(f"*.{ext}"):
                first = path.stem.split("_", 1)[0]
                if first.isdigit():
                    apps.add(int(first))
    return sorted(apps)


def locate_manifest_bins(app_id: int, *, cache_roots: list[Path]) -> list[Path]:
    """Newest manifest per depot for ``app_id`` across all roots (empty if none).

    Matches both .bin and .shas manifests. Roots are searched in order; the
    newest file per depot by mtime wins regardless of extension, so a fresher
    live-cache manifest supersedes an older archived one for the same depot (and
    a .bin and .shas for the same depot de-dupe to whichever is newer)."""
    newest_per_depot: dict[str, Path] = {}
    for root in cache_roots:
        v1 = root / "v1"
        if not v1.is_dir():
            continue
        for ext in _MANIFEST_EXTS:
            for path in v1.glob(f"{app_id}_{app_id}_*.{ext}"):
                parts = path.stem.split("_")
                if len(parts) != 4:
                    continue
                depot = parts[2]
                current = newest_per_depot.get(depot)
                if current is None or path.stat().st_mtime > current.stat().st_mtime:
                    newest_per_depot[depot] = path
    return list(newest_per_depot.values())
