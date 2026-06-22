"""Tests for locating an app's current manifest .bin files (cache-based)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from orchestrator.agent.manifest_locator import locate_manifest_bins

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
    found = sorted(p.name for p in locate_manifest_bins(440, cache_root=tmp_path))
    assert found == ["440_440_440_111.bin", "440_440_441_333.bin"]


def test_app_with_no_bins_returns_empty(tmp_path):
    _write(tmp_path, "440_440_440_111.bin", 1000)
    assert locate_manifest_bins(999, cache_root=tmp_path) == []


def test_no_cache_dir_returns_empty(tmp_path):
    assert locate_manifest_bins(440, cache_root=tmp_path / "missing") == []


def test_single_depot_single_gid(tmp_path):
    _write(tmp_path, "1182900_1182900_1182901_3367036266289852265.bin", 1000)
    found = locate_manifest_bins(1182900, cache_root=tmp_path)
    assert [p.name for p in found] == ["1182900_1182900_1182901_3367036266289852265.bin"]


def test_list_prefilled_app_ids(tmp_path):
    from orchestrator.agent.manifest_locator import list_prefilled_app_ids

    _write(tmp_path, "440_440_440_111.bin", 1000)
    _write(tmp_path, "440_440_441_222.bin", 1000)  # same app, diff depot
    _write(tmp_path, "730_730_731_333.bin", 1000)
    assert list_prefilled_app_ids(cache_root=tmp_path) == [440, 730]


def test_list_prefilled_app_ids_no_cache(tmp_path):
    from orchestrator.agent.manifest_locator import list_prefilled_app_ids

    assert list_prefilled_app_ids(cache_root=tmp_path / "missing") == []
