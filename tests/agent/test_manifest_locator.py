"""Tests for locating an app's current manifest .bin files (cache-based)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from orchestrator.agent.manifest_locator import list_prefilled_app_ids, locate_manifest_bins

if TYPE_CHECKING:
    from pathlib import Path


def _write(cache_root: Path, name: str, mtime: int) -> Path:
    v1 = cache_root / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    p = v1 / name
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    return p


def test_locates_newest_bin_per_depot(tmp_path):
    # app 440, depots 440 and 441. Depot 441 has two gids -> newest mtime wins.
    _write(tmp_path, "440_440_440_111.bin", 1000)
    _write(tmp_path, "440_440_441_222.bin", 1000)
    _write(tmp_path, "440_440_441_333.bin", 2000)  # newer for depot 441
    _write(tmp_path, "570_570_5701_999.bin", 1000)  # other app
    found = sorted(p.name for p in locate_manifest_bins(440, cache_roots=[tmp_path]))
    assert found == ["440_440_440_111.bin", "440_440_441_333.bin"]


def test_app_with_no_bins_returns_empty(tmp_path):
    _write(tmp_path, "440_440_440_111.bin", 1000)
    assert locate_manifest_bins(999, cache_roots=[tmp_path]) == []


def test_no_cache_dir_returns_empty(tmp_path):
    assert locate_manifest_bins(440, cache_roots=[tmp_path / "missing"]) == []


def test_single_depot_single_gid(tmp_path):
    _write(tmp_path, "1182900_1182900_1182901_3367036266289852265.bin", 1000)
    found = locate_manifest_bins(1182900, cache_roots=[tmp_path])
    assert [p.name for p in found] == ["1182900_1182900_1182901_3367036266289852265.bin"]


def test_list_prefilled_app_ids(tmp_path):
    _write(tmp_path, "440_440_440_111.bin", 1000)
    _write(tmp_path, "440_440_441_222.bin", 1000)  # same app, diff depot
    _write(tmp_path, "730_730_731_333.bin", 1000)
    assert list_prefilled_app_ids(cache_roots=[tmp_path]) == [440, 730]


def test_list_prefilled_app_ids_no_cache(tmp_path):
    assert list_prefilled_app_ids(cache_roots=[tmp_path / "missing"]) == []


# --- Union read across multiple cache roots (durable manifest archive) ---


def _write_bin(root: Path, app: int, depot: int, gid: int, mtime: float | None = None) -> Path:
    v1 = root / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    p = v1 / f"{app}_{app}_{depot}_{gid}.bin"
    p.write_bytes(b"x")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_union_live_only(tmp_path):
    live = tmp_path / "live"
    _write_bin(live, 440, 441, 111)
    assert locate_manifest_bins(440, cache_roots=[live, tmp_path / "absent"])


def test_union_archive_only(tmp_path):
    arch = tmp_path / "arch"
    _write_bin(arch, 730, 731, 222)
    found = locate_manifest_bins(730, cache_roots=[tmp_path / "absent", arch])
    assert len(found) == 1


def test_union_newest_per_depot_across_roots(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _write_bin(arch, 570, 571, 1, mtime=1000.0)  # older, archived
    newer = _write_bin(live, 570, 571, 2, mtime=2000.0)  # newer, live, same depot
    found = locate_manifest_bins(570, cache_roots=[live, arch])
    assert found == [newer]  # newest-per-depot wins regardless of root order


def test_union_both_absent_returns_empty(tmp_path):
    assert locate_manifest_bins(1, cache_roots=[tmp_path / "a", tmp_path / "b"]) == []


def test_list_prefilled_app_ids_union(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _write_bin(live, 440, 441, 1)
    _write_bin(arch, 730, 731, 1)
    assert list_prefilled_app_ids(cache_roots=[live, arch]) == [440, 730]
