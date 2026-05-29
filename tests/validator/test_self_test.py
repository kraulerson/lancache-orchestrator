"""Tests for orchestrator.validator.self_test (F7)."""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.validator.self_test import validator_self_test

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


async def test_true_when_cache_dir_ok(tmp_path):
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)
    assert await validator_self_test(s) is True


async def test_false_when_cache_dir_missing(tmp_path):
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path / "nope")
    assert await validator_self_test(s) is False


async def test_false_when_cache_path_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_bytes(b"x")
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=f)
    assert await validator_self_test(s) is False
