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


def locate_manifest_bins(
    app_id: int, *, cache_roots: list[Path], prefilled_gids: set[str] | None = None
) -> list[Path]:
    """One manifest per depot for ``app_id`` across all roots (empty if none).

    Matches both .bin and .shas. By default the newest file per depot by mtime
    wins (a fresher live/archived manifest supersedes an older one; a .bin and a
    .shas for the same depot de-dupe to whichever is newer).

    ``prefilled_gids`` (the set of gids SteamPrefill's own record says it
    prefilled for this app, supplied by the caller — the locator never reads
    that file itself, see the module docstring) is a per-depot SELECTION
    *preference*, not an enumeration index: for any depot that has a candidate
    whose gid is in the set, that gid is chosen (newest among the matching) even
    if a different gid is newer by mtime — so validation pins to the version that
    was actually prefilled rather than the latest manifest on disk. A depot with
    no matching candidate (not in the record, or a .shas sidecar) falls back to
    newest-by-mtime. None/empty preserves the pure newest-by-mtime behavior."""
    candidates: dict[str, list[Path]] = {}
    for root in cache_roots:
        v1 = root / "v1"
        if not v1.is_dir():
            continue
        for ext in _MANIFEST_EXTS:
            for path in v1.glob(f"{app_id}_{app_id}_*.{ext}"):
                parts = path.stem.split("_")
                if len(parts) != 4:
                    continue
                candidates.setdefault(parts[2], []).append(path)

    result: list[Path] = []
    for paths in candidates.values():
        pool = paths
        if prefilled_gids:
            matching = [p for p in paths if p.stem.split("_")[3] in prefilled_gids]
            if matching:
                pool = matching
        result.append(max(pool, key=lambda p: p.stat().st_mtime))
    return result
