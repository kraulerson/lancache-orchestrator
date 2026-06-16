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
        client._recent_stderr = __import__("collections").deque(maxlen=50)

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


class TestManifestFetch:
    """BL12: SteamWorkerClient.manifest_fetch() round-trips the IPC op."""

    async def test_returns_manifests_list(self):
        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._writer = _StubWriter()
        client._pending = {}
        client._timeout = 5.0
        client._op_timeout_overrides = {"manifest.fetch": 300.0}

        async def round_trip():
            msg_id = await client._send("manifest.fetch", {"app_id": 730})
            line = (
                b'{"msg_id":"'
                + msg_id.encode()
                + b'","ok":true,"result":{"manifests":['
                + b'{"depot_id":731,"manifest_gid":42,"name":"x",'
                + b'"total_bytes":100,"chunk_count":5,"raw_b64":"AAAA"}'
                + b"]}}\n"
            )
            await client._on_response_line(line)
            return await client._await_response(msg_id, timeout=300.0)

        result = await round_trip()
        assert "manifests" in result
        assert len(result["manifests"]) == 1
        assert result["manifests"][0]["depot_id"] == 731


class TestManifestExpand:
    """F7 / S2-2: manifest_expand() hands the BLOB off via a temp file path."""

    async def test_writes_blob_temp_file_and_returns_result(self):
        import asyncio
        import os

        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._manifest_expand_lock = asyncio.Lock()
        raw = b"\x28\xb5\x2f\xfd_stub_zstd_bytes"
        sha_a = "aa" * 20
        captured: dict = {}

        async def fake_send_and_await(op, params):
            captured["op"] = op
            captured["params"] = params
            # The client must have written the raw bytes to the temp path
            # BEFORE sending — read them back to verify.
            with open(params["raw_path"], "rb") as fh:
                captured["blob"] = fh.read()
            return {"depot_id": 731, "chunk_shas": [sha_a]}

        client._send_and_await = fake_send_and_await  # type: ignore[method-assign]

        result = await client.manifest_expand(raw)

        assert result == {"depot_id": 731, "chunk_shas": [sha_a]}
        assert captured["op"] == "manifest.expand"
        assert "raw_path" in captured["params"]
        assert "raw_b64" not in captured["params"]  # no bytes in the IPC line
        assert captured["blob"] == raw
        # Client cleans up the temp file after the call (finally unlink).
        assert not os.path.exists(captured["params"]["raw_path"])

    async def test_concurrent_manifest_expand_serialized(self):
        """The steam worker is strictly serial; manifest_expand single-flights so
        concurrent callers (the F13 sweep) don't queue head-of-line with their
        per-request timeout clock already running (F13 adversarial finding 2)."""
        import asyncio

        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._manifest_expand_lock = asyncio.Lock()
        in_flight = 0
        max_in_flight = 0

        async def fake_send_and_await(op, params):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"depot_id": 1, "chunk_shas": []}

        client._send_and_await = fake_send_and_await  # type: ignore[method-assign]
        await asyncio.gather(*(client.manifest_expand(b"zstdblob") for _ in range(5)))
        assert max_in_flight == 1  # serialized by the single-flight lock


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


class TestWorkerStderrDrain:
    """The worker subprocess is opaque: its stderr (gevent/steam-next tracebacks,
    native crash messages like 'Segmentation fault') is the ONLY diagnostic when
    it dies. It must be drained continuously — both so the pipe never fills and
    stalls the worker (64 KiB OS buffer), and so a crash leaves a breadcrumb.
    The last lines must surface in the steam_worker.died log (UAT-11: a worker
    crashed mid manifest.fetch with reason=stdout_closed and zero diagnostics)."""

    async def test_drain_stderr_captures_worker_lines_into_ring(self):
        import asyncio
        import collections

        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._recent_stderr = collections.deque(maxlen=50)

        loop = asyncio.get_event_loop()
        stderr = asyncio.StreamReader(loop=loop)
        stderr.feed_data(b"Traceback (most recent call last):\n")
        stderr.feed_data(b"RuntimeError: greenlet boom\n")
        stderr.feed_eof()

        class _FakeProcess:
            returncode = None

        proc = _FakeProcess()
        proc.stderr = stderr
        client._process = proc

        await client._drain_stderr()

        captured = list(client._recent_stderr)
        assert "RuntimeError: greenlet boom" in captured
        assert "Traceback (most recent call last):" in captured

    async def test_worker_death_log_includes_recent_stderr(self):
        import collections
        from unittest.mock import MagicMock, patch

        from orchestrator.platform.steam.client import SteamWorkerClient

        client = SteamWorkerClient.__new__(SteamWorkerClient)
        client._max_restart_attempts = 3
        client._restart_attempts = 0
        client._disabled = False
        client._writer = None
        client._reader_task = None
        client._pending = {}
        client._recent_stderr = collections.deque(["Segmentation fault (core dumped)"], maxlen=50)

        # Patch the module logger directly: structlog.capture_logs is defeated by
        # the project's cache_logger_on_first_use config once the logger is cached
        # earlier in the suite, so assert on the call kwargs instead.
        fake_log = MagicMock()
        with patch("orchestrator.platform.steam.client._log", fake_log):
            client._on_worker_died(reason="stdout_closed")

        fake_log.warning.assert_called_once()
        _, kwargs = fake_log.warning.call_args
        # The crash breadcrumb must travel with the death event.
        assert "Segmentation fault (core dumped)" in str(kwargs.get("recent_stderr"))

    async def test_start_creates_stderr_drain_task(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from orchestrator.core.settings import get_settings
        from orchestrator.platform.steam.client import SteamWorkerClient

        monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
        get_settings.cache_clear()

        client = SteamWorkerClient()

        eof_stderr = asyncio.StreamReader()
        eof_stderr.feed_eof()
        eof_stdout = asyncio.StreamReader()
        eof_stdout.feed_eof()

        class _FakeProcess:
            pid = 4321
            returncode = None
            stdin = None

        async def _fake_create(*_args, **_kwargs):
            proc = _FakeProcess()
            proc.stdout = eof_stdout
            proc.stderr = eof_stderr
            return proc

        with patch(
            "orchestrator.platform.steam.client.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_create),
        ):
            await client.start()

        try:
            assert isinstance(client._stderr_task, asyncio.Task), (
                "start() must drain the worker's stderr so it can't fill the pipe "
                "and so crashes leave a diagnostic breadcrumb"
            )
        finally:
            if client._reader_task is not None:
                client._reader_task.cancel()
            if client._stderr_task is not None:
                client._stderr_task.cancel()


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
        client._recent_stderr = __import__("collections").deque(maxlen=50)

        for _ in range(3):
            client._on_worker_died(reason="crash")
        # 4th death triggers the guard
        client._on_worker_died(reason="crash")
        assert client._disabled is True


class TestWorkerRestartOnWedge:
    """#153: when a steam op times out (IPC) or is cancelled (job-runtime
    timeout), the worker subprocess is wedged on the abandoned op and the next
    job would queue behind it. The next op must restart the worker first so it
    starts clean — accepting session loss (the worker doesn't auto-relogin), an
    operator-approved tradeoff."""

    def _bare_client(self):
        import asyncio

        from orchestrator.platform.steam.client import SteamWorkerClient

        c = SteamWorkerClient.__new__(SteamWorkerClient)
        c._writer = _StubWriter()
        c._pending = {}
        c._timeout = 0.02
        c._op_timeout_overrides = {}
        c._needs_restart = False
        c._intentional_restart = False
        c._ipc_lock = asyncio.Lock()
        c._process = None
        c._reader_task = None
        c._stderr_task = None
        return c

    async def test_ipc_timeout_flags_worker_for_restart(self):
        from orchestrator.platform.steam.client import IPCTimeoutError

        c = self._bare_client()
        # No response is ever fed → the await times out.
        with pytest.raises(IPCTimeoutError):
            await c._send_and_await("auth.status", {})
        assert c._needs_restart is True

    async def test_cancelled_op_flags_worker_for_restart(self):
        import asyncio

        c = self._bare_client()

        async def fake_send(op, params):
            return "mid"

        async def fake_await(msg_id, timeout=None):
            raise asyncio.CancelledError()

        c._send = fake_send  # type: ignore[method-assign]
        c._await_response = fake_await  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await c._send_and_await("manifest.fetch", {})
        assert c._needs_restart is True

    async def test_next_op_restarts_wedged_worker_before_sending(self):
        c = self._bare_client()
        c._needs_restart = True
        order: list[str] = []

        async def fake_restart():
            order.append("restart")
            c._needs_restart = False

        async def fake_send(op, params):
            order.append("send")
            return "mid"

        async def fake_await(msg_id, timeout=None):
            order.append("await")
            return {"ok": True}

        c._restart_after_wedge = fake_restart  # type: ignore[method-assign]
        c._send = fake_send  # type: ignore[method-assign]
        c._await_response = fake_await  # type: ignore[method-assign]

        await c._send_and_await("auth.status", {})
        assert order == ["restart", "send", "await"]

    async def test_shutdown_op_never_triggers_restart(self):
        # stop() sends the 'shutdown' op through _send_and_await; a wedged worker
        # must not try to restart itself during shutdown.
        c = self._bare_client()
        c._needs_restart = True
        restarted: list[bool] = []

        async def fake_restart():
            restarted.append(True)

        async def fake_send(op, params):
            return "mid"

        async def fake_await(msg_id, timeout=None):
            return {}

        c._restart_after_wedge = fake_restart  # type: ignore[method-assign]
        c._send = fake_send  # type: ignore[method-assign]
        c._await_response = fake_await  # type: ignore[method-assign]

        await c._send_and_await("shutdown", {})
        assert restarted == []

    async def test_intentional_restart_does_not_trip_storm_guard(self):
        import asyncio
        import collections

        from orchestrator.platform.steam.client import SteamWorkerClient

        c = SteamWorkerClient.__new__(SteamWorkerClient)
        c._restart_attempts = 0
        c._max_restart_attempts = 3
        c._disabled = False
        c._pending = {}
        c._reader_task = None
        c._recent_stderr = collections.deque(maxlen=50)
        c._intentional_restart = True  # a deliberate restart is in progress

        reader = asyncio.StreamReader()
        reader.feed_eof()

        class _P:
            stdout = reader
            returncode = None

        c._process = _P()

        # The reader sees EOF from the intentional SIGKILL — it must NOT count as
        # a crash (else repeated wedges would trip the restart-storm guard).
        await c._read_loop()
        assert c._restart_attempts == 0

    async def test_concurrent_wedged_ops_restart_worker_only_once(self):
        """#153 (adversarial-review fix): the client is shared between the jobs
        loop and API auth handlers, so two ops can hit the wedge restart
        concurrently. The `_ipc_lock` + clear-flag-after-respawn must serialize
        them so the worker is respawned EXACTLY once, not double-killed."""
        import asyncio

        c = self._bare_client()
        c._needs_restart = True

        starts: list[bool] = []

        async def fake_start():
            # Real _restart_after_wedge clears _needs_restart only AFTER start().
            starts.append(True)

        sent: list[str] = []

        async def fake_send(op, params):
            sent.append(op)
            return op

        async def fake_await(msg_id, timeout=None):
            return {"ok": True}

        c.start = fake_start  # type: ignore[method-assign]
        c._send = fake_send  # type: ignore[method-assign]
        c._await_response = fake_await  # type: ignore[method-assign]

        await asyncio.gather(
            c._send_and_await("auth.status", {}),
            c._send_and_await("library.enumerate", {}),
        )

        assert len(starts) == 1, "worker respawned more than once under concurrency"
        assert len(sent) == 2  # both ops still sent
        assert c._needs_restart is False
