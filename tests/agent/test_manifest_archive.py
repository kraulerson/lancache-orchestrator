"""Tests for the durable manifest archive — append-only sync of SteamPrefill's
transient .bin manifests into a permanent store."""

from __future__ import annotations

import os
import time
from pathlib import Path

import orchestrator.agent.manifest_archive as mod
from orchestrator.agent.manifest_archive import sync_manifests_to_archive


def _bin(root: Path, name: str, age_seconds: float = 100.0) -> Path:
    v1 = root / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    p = v1 / name
    p.write_bytes(b"data")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def test_copies_new_bin(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "440_440_441_1.bin")
    assert sync_manifests_to_archive(live, arch) == 1
    assert (arch / "v1" / "440_440_441_1.bin").is_file()


def test_skips_already_archived(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "440_440_441_1.bin")
    _bin(arch, "440_440_441_1.bin")
    assert sync_manifests_to_archive(live, arch) == 0


def test_preserves_mtime(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    src = _bin(live, "1_1_2_3.bin", age_seconds=5000.0)
    sync_manifests_to_archive(live, arch)
    assert (arch / "v1" / "1_1_2_3.bin").stat().st_mtime == src.stat().st_mtime


def test_settle_guard_skips_too_fresh(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "9_9_9_9.bin", age_seconds=0.0)  # written "now"
    assert sync_manifests_to_archive(live, arch, settle_seconds=10.0) == 0


def test_tolerates_unreadable_file(tmp_path, monkeypatch):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "1_1_2_3.bin")
    _bin(live, "4_4_5_6.bin")
    real = mod.shutil.copy2

    def flaky(src, dst, *a, **k):
        if Path(src).name == "1_1_2_3.bin":
            raise OSError("boom")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(mod.shutil, "copy2", flaky)
    assert sync_manifests_to_archive(live, arch) == 1  # the good one still copied


def test_no_op_when_live_absent(tmp_path):
    assert sync_manifests_to_archive(tmp_path / "nope", tmp_path / "arch") == 0


def test_never_deletes_archive(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    keep = _bin(arch, "stale_only_in_archive.bin")
    _bin(live, "1_1_2_3.bin")
    sync_manifests_to_archive(live, arch)
    assert keep.is_file()
