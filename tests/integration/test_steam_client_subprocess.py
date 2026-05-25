"""Integration tests for SteamWorkerClient ↔ mock steam worker.

Spawns the mock worker as a real subprocess and exercises the full IPC
plumbing (stdin pipe + stdout pipe + JSON line framing + msg_id
correlation + timeouts).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_WORKER = REPO_ROOT / "tests" / "integration" / "mock_steam_worker.py"


@pytest.fixture
def mock_worker_settings(monkeypatch):
    """Set env vars so SteamWorkerClient construction doesn't fail.

    The actual subprocess invocation is overridden in _make_client_pointing_at_mock
    to launch the mock script directly — these settings are only needed so
    SteamWorkerClient.__init__ can call get_settings() without errors.
    """
    from orchestrator.core.settings import get_settings

    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    monkeypatch.setenv("ORCH_STEAM_WORKER_PYTHON_PATH", sys.executable)
    monkeypatch.setenv("ORCH_STEAM_WORKER_IPC_TIMEOUT_SEC", "5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _make_client_pointing_at_mock(scenario: str):
    """Construct a SteamWorkerClient that spawns the mock script instead
    of the real worker module."""
    from orchestrator.platform.steam.client import SteamWorkerClient

    client = SteamWorkerClient()

    env = dict(os.environ)
    env["MOCK_SCENARIO"] = scenario
    client._process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(MOCK_WORKER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    client._writer = client._process.stdin  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._read_loop())
    return client


async def test_auth_begin_no_2fa_returns_authenticated(mock_worker_settings):
    client = await _make_client_pointing_at_mock(scenario="no_2fa")
    try:
        result = await client.auth_begin("alice", "secret")
        assert result["authenticated"] is True
        assert result["steam_id"] == 76561198000000000
    finally:
        await client.stop()


async def test_auth_begin_needs_2fa_returns_challenge(mock_worker_settings):
    client = await _make_client_pointing_at_mock(scenario="needs_mobile_auth")
    try:
        result = await client.auth_begin("alice", "secret")
        assert result["authenticated"] is False
        assert "challenge_id" in result
        assert result["challenge_type"] == "mobile_authenticator"
    finally:
        await client.stop()


async def test_auth_complete_bad_code_raises_steam_worker_error(mock_worker_settings):
    from orchestrator.platform.steam.client import SteamWorkerError

    client = await _make_client_pointing_at_mock(scenario="bad_code")
    try:
        with pytest.raises(SteamWorkerError) as exc_info:
            await client.auth_complete("any-challenge-id", "wrong-code")
        assert exc_info.value.kind == "TwoFactorCodeMismatch"
    finally:
        await client.stop()


async def test_ipc_timeout_raises_after_threshold(mock_worker_settings, monkeypatch):
    monkeypatch.setenv("ORCH_STEAM_WORKER_IPC_TIMEOUT_SEC", "1")
    from orchestrator.core.settings import get_settings
    from orchestrator.platform.steam.client import IPCTimeoutError

    get_settings.cache_clear()
    client = await _make_client_pointing_at_mock(scenario="ipc_silence")
    try:
        with pytest.raises(IPCTimeoutError):
            await client.auth_status()
    finally:
        await client.stop()


async def test_clean_shutdown_via_stop(mock_worker_settings):
    client = await _make_client_pointing_at_mock(scenario="no_2fa")
    pid = client._process.pid  # type: ignore[union-attr]
    assert pid is not None
    await client.stop()
    # Process should be terminated
    assert client._process is None or client._process.returncode is not None
