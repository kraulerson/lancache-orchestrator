"""Tests for locating an app's current manifest .bin files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from orchestrator.agent.manifest_locator import locate_manifest_bins

if TYPE_CHECKING:
    from pathlib import Path


def _setup(tmp_path: Path, downloaded: dict, bin_names: list[str]) -> tuple[Path, Path]:
    cache = tmp_path / "cache" / "v1"
    cache.mkdir(parents=True)
    for name in bin_names:
        (cache / name).write_bytes(b"x")
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps(downloaded))
    return cache.parent, cfg


def test_locates_bins_for_app(tmp_path):
    cache_root, cfg = _setup(
        tmp_path,
        {"440": [111, 222]},
        ["440_440_4401_111.bin", "440_440_4402_222.bin", "570_570_5701_999.bin"],
    )
    found = locate_manifest_bins(440, cache_root=cache_root, config_dir=cfg)
    names = sorted(p.name for p in found)
    assert names == ["440_440_4401_111.bin", "440_440_4402_222.bin"]


def test_app_not_prefilled_returns_empty(tmp_path):
    cache_root, cfg = _setup(tmp_path, {"440": [111]}, ["440_440_4401_111.bin"])
    assert locate_manifest_bins(999, cache_root=cache_root, config_dir=cfg) == []


def test_missing_bin_for_gid_skipped(tmp_path):
    cache_root, cfg = _setup(tmp_path, {"440": [111, 222]}, ["440_440_4401_111.bin"])
    found = locate_manifest_bins(440, cache_root=cache_root, config_dir=cfg)
    assert [p.name for p in found] == ["440_440_4401_111.bin"]


def test_no_downloaded_file_returns_empty(tmp_path):
    cache = tmp_path / "cache" / "v1"
    cache.mkdir(parents=True)
    cfg = tmp_path / "Config"
    cfg.mkdir()
    assert locate_manifest_bins(440, cache_root=cache.parent, config_dir=cfg) == []
