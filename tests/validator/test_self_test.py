"""Tests for orchestrator.validator.self_test (F7)."""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.validator.self_test import validator_self_test

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


async def test_true_when_cache_dir_ok(tmp_path):
    # A real cache has content; an empty non-mount dir is now treated as the
    # unmounted-misconfiguration case (see test_false_when_empty_and_not_a_mountpoint).
    (tmp_path / "ab").mkdir()
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


async def test_false_when_empty_and_not_a_mountpoint(tmp_path):
    """An unmounted Docker bind-mount/volume silently becomes an empty, non-mount
    directory — the self-test must catch it rather than report healthy and then
    flag every cached game as missing (audit 2026-06-09)."""
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)
    assert await validator_self_test(s) is False  # empty + not a mountpoint


async def test_true_when_empty_but_is_a_mountpoint(tmp_path, monkeypatch):
    """A correctly-mounted but freshly-empty cache (a real mountpoint) is fine."""
    import orchestrator.validator.self_test as st

    monkeypatch.setattr(st.os.path, "ismount", lambda _p: True)
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)
    assert await validator_self_test(s) is True


async def test_true_when_non_empty_cache(tmp_path):
    """A non-empty cache directory passes regardless of mount detection."""
    (tmp_path / "ab").mkdir()
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)
    assert await validator_self_test(s) is True


# --- re-arch ④: agent-sourced validator health (control plane has no cache mount) ---


class _StubAgent:
    """Minimal AgentClient stand-in: agent_health() returns a canned body or
    raises, so we can exercise validator_self_test's agent branch in isolation."""

    def __init__(self, *, healthy: bool | None = None, raises: bool = False) -> None:
        self._healthy = healthy
        self._raises = raises

    async def agent_health(self) -> dict[str, object]:
        if self._raises:
            raise RuntimeError("agent unreachable")
        return {"ok": True, "validator_healthy": self._healthy}


async def test_agent_enabled_sources_health_from_agent_true(tmp_path):
    """With agent_enabled and an agent client, health comes from the agent, not
    the (absent on the LXC) local cache path."""
    s = Settings(
        orchestrator_token=VALID_TOKEN,
        agent_enabled=True,
        lancache_nginx_cache_path=tmp_path / "does-not-exist",
    )
    assert await validator_self_test(s, agent_client=_StubAgent(healthy=True)) is True


async def test_agent_enabled_sources_health_from_agent_false(tmp_path):
    s = Settings(
        orchestrator_token=VALID_TOKEN,
        agent_enabled=True,
        lancache_nginx_cache_path=tmp_path,
    )
    assert await validator_self_test(s, agent_client=_StubAgent(healthy=False)) is False


async def test_agent_enabled_agent_raises_returns_false(tmp_path):
    s = Settings(
        orchestrator_token=VALID_TOKEN,
        agent_enabled=True,
        lancache_nginx_cache_path=tmp_path,
    )
    assert await validator_self_test(s, agent_client=_StubAgent(raises=True)) is False


async def test_flag_off_still_uses_local_cache_path(tmp_path):
    """Flag-off (default) is byte-identical: the local cache path is the source of
    truth and the agent_client argument is ignored entirely."""
    (tmp_path / "ab").mkdir()
    s = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)
    # Even passing an agent that would say False, flag-off must read local → True.
    assert await validator_self_test(s, agent_client=_StubAgent(healthy=False)) is True


async def test_agent_health_missing_key_defaults_false(tmp_path):
    """An agent body that omits `validator_healthy` defaults to False — locks the
    `.get(..., False)` contract against an older/partial agent response."""

    class _NoKeyAgent:
        async def agent_health(self) -> dict[str, object]:
            return {"ok": True}  # no validator_healthy key

    s = Settings(
        orchestrator_token=VALID_TOKEN,
        agent_enabled=True,
        lancache_nginx_cache_path=tmp_path,
    )
    assert await validator_self_test(s, agent_client=_NoKeyAgent()) is False
