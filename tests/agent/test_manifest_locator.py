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


# --- prefilled_gids: per-depot gid preference (validate against the gid
#     SteamPrefill actually prefilled, not just the newest file by mtime) ---


def test_prefers_prefilled_gid_over_newer_mtime(tmp_path):
    # Depot 441 has two manifests: gid 222 (the PREFILLED gid, OLDER mtime) and
    # gid 333 (newer mtime but a stale version). The prefilled gid must win.
    _write(tmp_path, "440_440_441_222.bin", 1000)  # prefilled, older
    _write(tmp_path, "440_440_441_333.bin", 2000)  # newer mtime, stale version
    found = locate_manifest_bins(440, cache_roots=[tmp_path], prefilled_gids={"222"})
    assert [p.name for p in found] == ["440_440_441_222.bin"]


def test_depot_not_in_prefilled_record_falls_back_to_mtime(tmp_path):
    # Depot 441 has a prefilled gid; depot 442 is NOT in the record (the record
    # lists only a subset of depots) -> 442 falls back to newest-by-mtime.
    _write(tmp_path, "440_440_441_222.bin", 1000)  # prefilled
    _write(tmp_path, "440_440_442_555.bin", 1000)
    _write(tmp_path, "440_440_442_666.bin", 2000)  # newer for depot 442 (not in record)
    found = sorted(
        p.name for p in locate_manifest_bins(440, cache_roots=[tmp_path], prefilled_gids={"222"})
    )
    assert found == ["440_440_441_222.bin", "440_440_442_666.bin"]


def test_shas_sidecar_with_no_recorded_gid_falls_back(tmp_path):
    # A .shas (fetcher sidecar) whose gid isn't in the prefilled record is kept
    # via the per-depot mtime fallback, not dropped.
    _write(tmp_path, "440_440_441_999.shas", 1000)
    found = locate_manifest_bins(440, cache_roots=[tmp_path], prefilled_gids={"222"})
    assert [p.name for p in found] == ["440_440_441_999.shas"]


def test_prefilled_gids_none_keeps_newest_mtime(tmp_path):
    # Backward-compatible: no record -> the original newest-by-mtime behavior.
    _write(tmp_path, "440_440_441_222.bin", 1000)
    _write(tmp_path, "440_440_441_333.bin", 2000)
    found = locate_manifest_bins(440, cache_roots=[tmp_path], prefilled_gids=None)
    assert [p.name for p in found] == ["440_440_441_333.bin"]


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


# --- .shas sidecar manifests (fetcher writes {app}_{app}_{depot}_{gid}.shas) ---


def test_locates_shas_only_app(tmp_path):
    # An app whose ONLY manifest is a .shas (SteamPrefill never cached it).
    _write(tmp_path, "900_900_901_777.shas", 1000)
    found = [p.name for p in locate_manifest_bins(900, cache_roots=[tmp_path])]
    assert found == ["900_900_901_777.shas"]


def test_bin_and_shas_same_depot_newest_mtime_wins(tmp_path):
    # A .bin and a .shas for the SAME app+depot de-dupe to the newer mtime.
    _write(tmp_path, "440_440_440_111.bin", 1000)
    newer = _write(tmp_path, "440_440_440_222.shas", 2000)
    found = locate_manifest_bins(440, cache_roots=[tmp_path])
    assert [p.name for p in found] == [newer.name]


def test_bin_wins_when_bin_is_newer_than_shas(tmp_path):
    _write(tmp_path, "440_440_440_111.shas", 1000)
    newer = _write(tmp_path, "440_440_440_222.bin", 2000)
    found = locate_manifest_bins(440, cache_roots=[tmp_path])
    assert [p.name for p in found] == [newer.name]


def test_shas_only_app_in_list_prefilled_app_ids(tmp_path):
    _write(tmp_path, "440_440_440_111.bin", 1000)  # .bin app
    _write(tmp_path, "900_900_901_777.shas", 1000)  # .shas-only app
    assert list_prefilled_app_ids(cache_roots=[tmp_path]) == [440, 900]


# --- Differing-group-id .bin naming (real SteamPrefill secondary-depot shape) ---
#
# SteamPrefill names a game's PRIMARY depot {app}_{app}_{depot}_{gid}.bin, but a
# secondary depot is {app}_{depotGroupId}_{depot}_{gid}.bin where depotGroupId is
# frequently a DIFFERENT number from app_id (a Steam-internal grouping, not the
# app id). The old glob only matched the first shape.


def test_locates_bin_with_differing_group_id_segment(tmp_path):
    # depot 229003's "group id" (228980) differs from the app id (219990) --
    # this exact shape was found live on Grim Dawn.
    _write(tmp_path, "219990_228980_229003_8740933542064151477.bin", 1000)
    found = [p.name for p in locate_manifest_bins(219990, cache_roots=[tmp_path])]
    assert found == ["219990_228980_229003_8740933542064151477.bin"]


def test_mix_of_matching_and_differing_group_id_depots(tmp_path):
    # One depot matches the old app_id-repeated pattern, one doesn't -- both
    # must be found, matching the real Grim Dawn shape (1 of 9 depots matched
    # the old glob; the other 8 didn't).
    _write(tmp_path, "219990_219990_219991_111.bin", 1000)  # matches old pattern
    _write(tmp_path, "219990_483840_483840_222.bin", 1000)  # differing group id
    found = sorted(p.name for p in locate_manifest_bins(219990, cache_roots=[tmp_path]))
    assert found == [
        "219990_219990_219991_111.bin",
        "219990_483840_483840_222.bin",
    ]


def test_realistic_nine_depot_shape(tmp_path):
    # Reproduces Grim Dawn's real depot layout: 9 depots, only 1 whose group id
    # equals the app id. A minimal 2-depot test could miss an ordering or
    # dedup bug that only shows up at real scale.
    depots = [
        ("219990", "219991", "1"),  # matches old pattern (group id == app id)
        ("228980", "229003", "2"),  # differs
        ("483840", "483840", "3"),
        ("642280", "642280", "4"),
        ("642280", "642281", "5"),
        ("897670", "897670", "6"),
        ("897670", "897671", "7"),
        ("2699230", "2699230", "8"),
        ("2699230", "2699231", "9"),
    ]
    expected_depots = set()
    for group, depot, gid in depots:
        _write(tmp_path, f"219990_{group}_{depot}_{gid}.bin", 1000)
        expected_depots.add(depot)
    found = locate_manifest_bins(219990, cache_roots=[tmp_path])
    found_depots = {p.stem.split("_")[2] for p in found}
    assert found_depots == expected_depots
    assert len(found_depots) == 9


def test_shas_glob_stays_narrow_for_differing_group_id_name(tmp_path):
    # The .shas extension must NOT widen -- the fetcher never actually writes
    # this shape (it always repeats app_id), but if a file like this existed
    # it must still be excluded, proving the two extensions are scoped
    # independently rather than sharing one widened pattern.
    _write(tmp_path, "219990_228980_229003_111.shas", 1000)
    assert locate_manifest_bins(219990, cache_roots=[tmp_path]) == []


# --- Cross-app-ID collision safety (the load-bearing safety argument for the
#     whole fix gets its own explicit regression test, not just informal
#     verification in the design doc) ---


def test_widened_glob_does_not_collide_short_app_id_is_prefix_of_long(tmp_path):
    # App 44's widened glob ("44_*") must not match app 440's files -- the
    # character immediately after "44" in "440_..." is "0", not "_".
    _write(tmp_path, "44_44_45_111.bin", 1000)
    _write(tmp_path, "440_440_441_222.bin", 1000)
    found = [p.name for p in locate_manifest_bins(44, cache_roots=[tmp_path])]
    assert found == ["44_44_45_111.bin"]


def test_widened_glob_does_not_collide_long_app_id_is_prefix_of_short(tmp_path):
    # The reverse direction: app 4400's glob ("4400_*") must not match a file
    # for app 44 whose second segment happens to start with "400".
    _write(tmp_path, "44_400_401_111.bin", 1000)
    _write(tmp_path, "4400_4400_4401_222.bin", 1000)
    found = [p.name for p in locate_manifest_bins(4400, cache_roots=[tmp_path])]
    assert found == ["4400_4400_4401_222.bin"]
