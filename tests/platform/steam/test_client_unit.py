"""Unit tests for SteamWorkerClient (asyncio side; BL10 / F1).

Subprocess isolated via dependency injection — these tests do NOT spawn
a real subprocess. End-to-end IPC plumbing is tested separately in
tests/integration/test_steam_client_subprocess.py.
"""

from __future__ import annotations

import pytest


class _StubWriter:
    """Stand-in for asyncio.StreamWriter."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


class _StubReader:
    """Stand-in for asyncio.StreamReader; feeds canned response lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class TestSteamWorkerClientRequestResponseCorrelation:
    async def test_request_returns_correlated_response(self):
        from orchestrator.platform.steam.client import SteamWorkerClient

        # The client doesn't know msg_ids in advance; we have to grab
        # the one it generated and craft a matching response.
        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._reader_task = None
        client._timeout = 5.0

        async def fake_send_and_wait(op, params):
            # Simulate the round trip: invoke the write path, then
            # synthesize the matching response and resolve the future.
            msg_id = await client._send(op, params)
            response_line = (
                b'{"msg_id":"' + msg_id.encode() + b'","ok":true,"result":{"echo":"hi"}}\n'
            )
            await client._on_response_line(response_line)
            return await client._await_response(msg_id)

        result = await fake_send_and_wait("auth.status", {})
        assert result == {"echo": "hi"}

    async def test_unknown_msg_id_response_ignored(self):
        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 5.0

        # Unknown msg_id; client should log and drop, not crash.
        await client._on_response_line(b'{"msg_id":"unknown","ok":true,"result":{}}\n')
        assert client._pending == {}


class TestSteamWorkerClientErrorPath:
    async def test_error_response_raises_steam_worker_error(self):
        from orchestrator.platform.steam.client import (
            SteamWorkerClient,
            SteamWorkerError,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 5.0

        async def fake_call():
            msg_id = await client._send("auth.begin", {"username": "u", "password": "p"})
            response_line = (
                b'{"msg_id":"'
                + msg_id.encode()
                + b'","ok":false,"error":{"kind":"InvalidCredentials","message":"bad"}}\n'
            )
            await client._on_response_line(response_line)
            with pytest.raises(SteamWorkerError) as exc_info:
                await client._await_response(msg_id)
            assert exc_info.value.kind == "InvalidCredentials"

        await fake_call()

    async def test_timeout_raises_ipc_timeout(self):
        from orchestrator.platform.steam.client import (
            IPCTimeoutError,
            SteamWorkerClient,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 0.05  # 50ms

        msg_id = await client._send("auth.status", {})
        with pytest.raises(IPCTimeoutError):
            await client._await_response(msg_id)


class TestLibraryEnumerate:
    """BL11: SteamWorkerClient.library_enumerate() round-trips the IPC op."""

    async def test_returns_apps_list(self):
        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 5.0

        async def round_trip():
            # Spy on `_send` so we can synthesize the response with the
            # auto-generated msg_id.
            msg_id = await client._send("library.enumerate", {})
            line = (
                b'{"msg_id":"'
                + msg_id.encode()
                + b'","ok":true,"result":{"apps":['
                + b'{"app_id":730,"name":"CS2","depots":[731]}'
                + b"]}}\n"
            )
            await client._on_response_line(line)
            return await client._await_response(msg_id)

        result = await round_trip()
        assert result == {"apps": [{"app_id": 730, "name": "CS2", "depots": [731]}]}

    async def test_not_authenticated_raises_steam_worker_error(self):
        from orchestrator.platform.steam.client import (
            SteamWorkerClient,
            SteamWorkerError,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 5.0

        msg_id = await client._send("library.enumerate", {})
        line = (
            b'{"msg_id":"'
            + msg_id.encode()
            + b'","ok":false,"error":{"kind":"NotAuthenticated","message":"no session"}}\n'
        )
        await client._on_response_line(line)
        with pytest.raises(SteamWorkerError) as exc_info:
            await client._await_response(msg_id)
        assert exc_info.value.kind == "NotAuthenticated"


class TestReadLoopLargeResponse:
    """F-UAT6-1: the asyncio StreamReader default limit is 64 KiB. A real
    Steam library_enumerate response exceeds this for any operator with
    more than ~600 owned apps. The orchestrator MUST configure the
    subprocess streams with a limit at least as large as the 10 MiB
    MAX_IPC_LINE_BYTES protocol cap, and MUST handle the overflow case
    gracefully (worker dies / restart-storm guard fires) rather than the
    reader task crashing on a raw ValueError.
    """

    async def test_create_subprocess_exec_uses_protocol_cap_as_limit(self, monkeypatch):
        """SteamWorkerClient.start() must pass limit= to create_subprocess_exec
        sized at >= MAX_IPC_LINE_BYTES, otherwise asyncio's default 64 KiB
        limit silently breaks any meaningful library_enumerate response."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.platform.steam.client import SteamWorkerClient
        from orchestrator.platform.steam.protocol import MAX_IPC_LINE_BYTES

        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        client = SteamWorkerClient()
        # We don't actually want to spawn — capture the call args.
        captured_kwargs: dict = {}

        async def _fake_create(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            raise FileNotFoundError("intercepted")  # short-circuit start()

        with patch(
            "orchestrator.platform.steam.client.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_create),
        ):
            from orchestrator.platform.steam.client import WorkerDiedError

            with pytest.raises(WorkerDiedError):
                await client.start()

        assert "limit" in captured_kwargs, (
            "start() must pass limit= so asyncio.StreamReader can handle large lines"
        )
        assert captured_kwargs["limit"] >= MAX_IPC_LINE_BYTES, (
            f"limit must be >= MAX_IPC_LINE_BYTES ({MAX_IPC_LINE_BYTES}); "
            f"got {captured_kwargs['limit']}"
        )

    async def test_read_loop_handles_limit_overrun_gracefully(self):
        """If a response line somehow exceeds the configured limit, the
        reader must mark the worker dead (not crash silently)."""
        from orchestrator.platform.steam.client import (
            SteamWorkerClient,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {"test-id": __import__("asyncio").get_event_loop().create_future()}
        client._restart_attempts = 0
        client._max_restart_attempts = 3
        client._disabled = False
        client._reader_task = None

        # Simulate a process whose stdout is a real StreamReader with tiny
        # limit; feed it a line that overflows.
        loop = __import__("asyncio").get_event_loop()
        small_reader = __import__("asyncio").StreamReader(limit=128, loop=loop)
        small_reader.feed_data(b"x" * 500 + b"\n")
        small_reader.feed_eof()

        class _FakeProcess:
            stdout = small_reader
            returncode = None

        client._process = _FakeProcess()

        # Run the read loop — it should set _disabled=False after one
        # death + the pending future should be failed with WorkerDiedError.
        await client._read_loop()

        # The pending future must be resolved (with WorkerDiedError) and
        # _restart_attempts incremented so awaiters don't hang silently —
        # this is the contract that F-UAT6-1 was missing.
        assert client._restart_attempts >= 1, "read loop must signal worker-died on overflow"


class TestWorkerEnvForSessionDir:
    """F-UAT6-2: the steam-next library writes refresh tokens to its
    credential_location directory; the worker MUST read the path from
    ORCH_STEAM_SESSION_DIR (env-passed because the worker is a separate
    venv and can't import orchestrator's Settings). The orchestrator
    client.start() must include this env var in the subprocess env.
    """

    async def test_start_passes_steam_session_dir_to_subprocess_env(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        from orchestrator.platform.steam.client import (
            SteamWorkerClient,
            WorkerDiedError,
        )

        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        # Use a non-default path so we can verify it actually got passed.
        monkeypatch.setenv("ORCH_STEAM_SESSION_DIR", "/data/orchestrator/steam_session_custom")
        # Need to clear the settings cache so the new env var is picked up.
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        client = SteamWorkerClient()
        captured_kwargs: dict = {}

        async def _fake_create(*_args, **kwargs):
            captured_kwargs.update(kwargs)
            raise FileNotFoundError("intercepted")

        with (
            patch(
                "orchestrator.platform.steam.client.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=_fake_create),
            ),
            pytest.raises(WorkerDiedError),
        ):
            await client.start()

        env = captured_kwargs.get("env") or {}
        assert "ORCH_STEAM_SESSION_DIR" in env, (
            "worker subprocess env must include ORCH_STEAM_SESSION_DIR"
        )
        assert env["ORCH_STEAM_SESSION_DIR"] == "/data/orchestrator/steam_session_custom"


class TestPerOpTimeoutOverride:
    """Issue #109: library.enumerate must use the long-running timeout
    (steam_worker_library_enumerate_timeout_sec) instead of the default
    30 s IPC budget. Other ops still use the default."""

    async def test_library_enumerate_uses_long_timeout(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        monkeypatch.setenv("ORCH_STEAM_WORKER_IPC_TIMEOUT_SEC", "5")
        monkeypatch.setenv("ORCH_STEAM_WORKER_LIBRARY_ENUMERATE_TIMEOUT_SEC", "120")
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient()
        assert client._timeout == 5.0
        assert client._op_timeout_overrides.get("library.enumerate") == 120.0
        # Non-overridden op stays on the default
        assert client._op_timeout_overrides.get("auth.begin") is None

    async def test_default_timeout_used_for_non_overridden_op(self):
        from orchestrator.platform.steam.client import (
            IPCTimeoutError,
            SteamWorkerClient,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 0.05  # 50 ms default
        client._op_timeout_overrides = {"library.enumerate": 1.0}

        # auth.status has no override → 50 ms default fires fast
        msg_id = await client._send("auth.status", {})
        with pytest.raises(IPCTimeoutError) as exc:
            await client._await_response(
                msg_id, timeout=client._op_timeout_overrides.get("auth.status")
            )
        assert "0.05" in str(exc.value)

    async def test_override_timeout_governs_long_op(self):
        from orchestrator.platform.steam.client import (
            IPCTimeoutError,
            SteamWorkerClient,
        )

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 30.0  # would-be default
        client._op_timeout_overrides = {"library.enumerate": 0.05}

        msg_id = await client._send("library.enumerate", {})
        with pytest.raises(IPCTimeoutError) as exc:
            await client._await_response(msg_id, timeout=0.05)
        # The error message reflects the override, not the default
        assert "0.05" in str(exc.value)


class TestRestartStormGuard:
    async def test_max_restart_attempts_exhausted_marks_disabled(self):
        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._max_restart_attempts = 3
        client._restart_attempts = 0
        client._disabled = False
        client._writer = None
        client._reader_task = None
        client._pending = {}

        for _ in range(3):
            client._on_worker_died(reason="crash")
        # 4th death triggers the guard
        client._on_worker_died(reason="crash")
        assert client._disabled is True
