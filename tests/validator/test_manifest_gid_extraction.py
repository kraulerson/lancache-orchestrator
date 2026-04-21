"""Regression test for manifest GID extraction from Steam app info.

steam-next 1.4.4 returns manifest info as a dict {'gid': '...', 'size': '...'}
instead of a plain string GID. The workaround must handle both formats.
"""

from __future__ import annotations


def extract_manifest_gid(manifest_info: dict | str | int) -> int:
    """Extract integer GID from either dict or scalar manifest info."""
    if isinstance(manifest_info, dict):
        return int(manifest_info["gid"])
    return int(manifest_info)


def test_dict_format_gid() -> None:
    """New Steam API format: manifest info is a dict with 'gid' key."""
    info = {"gid": "7613356809904826842", "size": "5884085"}
    assert extract_manifest_gid(info) == 7613356809904826842


def test_string_format_gid() -> None:
    """Old Steam API format: manifest info is a plain string GID."""
    assert extract_manifest_gid("7613356809904826842") == 7613356809904826842


def test_int_format_gid() -> None:
    """Edge case: manifest info already an int."""
    assert extract_manifest_gid(7613356809904826842) == 7613356809904826842
