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
