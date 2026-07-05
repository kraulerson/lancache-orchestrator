"""Tests for the agent path-safety guard (F18 Task 2b, under_cache_root)."""

from __future__ import annotations

from orchestrator.agent._paths import under_cache_root


def test_keeps_inside_paths(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    inside = root / "3b" / "3b" / "deadbeefdeadbeefdeadbeefdeadbeef"
    assert under_cache_root(root, [inside]) == [inside]


def test_drops_dotdot_traversal(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    escape = root / ".." / ".." / "etc" / "passwd"
    assert under_cache_root(root, [escape]) == []


def test_drops_root_itself(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    assert under_cache_root(root, [root]) == []


def test_drops_symlink_escape(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_bytes(b"x")
    link = root / "link"
    link.symlink_to(outside)  # resolves OUTSIDE the cache root
    assert under_cache_root(root, [link]) == []


def test_mixed_keeps_only_inside(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    good = root / "aa" / "bb" / "0123456789abcdef0123456789abcdef"
    bad = root / ".." / "etc"
    assert under_cache_root(root, [good, bad]) == [good]
