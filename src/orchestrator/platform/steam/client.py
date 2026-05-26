"""SteamWorkerClient — orchestrator-side bridge to the steam worker subprocess.

Manages the worker lifecycle, sends IPC requests, correlates responses by
msg_id, enforces per-request timeouts, and respects the restart-storm guard.

The worker subprocess runs gevent-patched steam-next code (see worker.py).
This file MUST NOT import `steam` or `gevent` directly — the whole point of
the subprocess split is to keep gevent's global monkey-patch out of the
orchestrator's asyncio process.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.platform.steam.protocol import (
    MAX_IPC_LINE_BYTES,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
)

# StreamReader limit for the subprocess's stdout. asyncio's default is
# 64 KiB (streams._DEFAULT_LIMIT) — far too small for `library.enumerate`
# responses, which exceed 64 KiB for any operator with more than ~600
# owned Steam apps. Set to MAX_IPC_LINE_BYTES + 1 KiB headroom so the
# 10 MiB protocol-level cap (protocol.py) is what enforces, not asyncio's
# arbitrary internal default. F-UAT6-1.
_STDOUT_READ_LIMIT = MAX_IPC_LINE_BYTES + 1024

_log = structlog.get_logger(__name__)


class SteamWorkerError(Exception):
    """Raised when the worker responds with ok=false. Carries the
    structured error kind + message from the worker."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message


class IPCTimeoutError(Exception):
    """Raised when a request doesn't get a response within
    Settings.steam_worker_ipc_timeout_sec."""


class WorkerDiedError(Exception):
    """Raised when the worker subprocess died mid-request."""


class WorkerDisabledError(Exception):
    """Raised when the restart-storm guard has fired and the worker
    will NOT be auto-respawned this orchestrator-process lifetime."""


class SteamWorkerClient:
    """Async client for the steam worker subprocess.

    Usage:
        client = SteamWorkerClient()
        await client.start()
        try:
            result = await client.auth_begin(username, password)
        finally:
            await client.stop()

    Lifecycle:
        - start() spawns the subprocess and starts the reader task.
        - Each public method (auth_begin, auth_complete, ...) sends a
          request and awaits the correlated response.
        - stop() sends `shutdown` IPC, waits up to 5s, then SIGTERM,
          waits 5s, then SIGKILL.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._python_path = settings.steam_worker_python_path
        self._worker_module = "orchestrator.platform.steam.worker"
        self._timeout = float(settings.steam_worker_ipc_timeout_sec)
        self._max_restart_attempts = settings.steam_worker_max_restart_attempts
        self._restart_attempts = 0
        self._disabled = False

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        """Spawn the worker subprocess. Idempotent (no-op if already running)."""
        if self._process is not None and self._process.returncode is None:
            return
        if self._disabled:
            raise WorkerDisabledError(
                f"worker restart-storm guard fired (max={self._max_restart_attempts})"
            )

        # Filter env: only PATH + LANG (no creds; worker reads stdin only).
        # F-UAT6-2: also forward ORCH_STEAM_SESSION_DIR so the worker writes
        # steam-next's credential dir to the operator-configured path
        # instead of the hardcoded default. The worker is a separate venv
        # and can't import orchestrator's Settings, so env-pass is the
        # contract.
        settings = get_settings()
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        env["ORCH_STEAM_SESSION_DIR"] = str(settings.steam_session_dir)
        cmd = [str(self._python_path), "-u", "-m", self._worker_module]

        try:
            # limit= sizes the StreamReader's internal buffer so large
            # `library.enumerate` responses don't trip asyncio's default
            # 64 KiB safeguard (F-UAT6-1).
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STDOUT_READ_LIMIT,
            )
        except FileNotFoundError as e:
            self._on_worker_died(reason="worker_binary_missing")
            raise WorkerDiedError(f"worker python not found: {self._python_path}") from e

        self._writer = self._process.stdin
        # asyncio.subprocess returns Process whose stdin is StreamWriter
        self._reader_task = asyncio.create_task(self._read_loop())
        _log.info("steam_worker.spawned", pid=self._process.pid)

    async def stop(self) -> None:
        """Graceful shutdown: send `shutdown` IPC, then SIGTERM, then SIGKILL.

        Issue #95 item 4: also suppresses `RuntimeError` (closed transport)
        and `OSError` (broken pipe). If the worker crashed mid-write, the
        underlying `_writer.write()` raises one of these — production
        shutdown should never fail because the process is already dead.
        """
        if self._process is None or self._process.returncode is not None:
            return
        with contextlib.suppress(
            TimeoutError, IPCTimeoutError, WorkerDiedError, RuntimeError, OSError
        ):
            await asyncio.wait_for(self._send_and_await("shutdown", {}), timeout=5.0)
        # SIGTERM, wait 5s, SIGKILL
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()

    async def auth_begin(self, username: str, password: str) -> dict[str, Any]:
        return await self._send_and_await(
            "auth.begin", {"username": username, "password": password}
        )

    async def auth_complete(self, challenge_id: str, code: str) -> dict[str, Any]:
        return await self._send_and_await(
            "auth.complete", {"challenge_id": challenge_id, "code": code}
        )

    async def auth_status(self) -> dict[str, Any]:
        return await self._send_and_await("auth.status", {})

    async def library_enumerate(self) -> dict[str, Any]:
        """Ask the worker to enumerate the operator's owned Steam apps (BL11).

        Returns `{"apps": [{"app_id": int, "name": str, "depots": [int, ...]}, ...]}`.
        Raises `SteamWorkerError(kind='NotAuthenticated')` if no Steam session.
        """
        return await self._send_and_await("library.enumerate", {})

    # --- internals -----------------------------------------------------

    async def _send(self, op: str, params: dict[str, Any]) -> str:
        msg_id = str(uuid.uuid4())
        req = RequestEnvelope(msg_id=msg_id, op=op, params=params)
        loop = asyncio.get_running_loop()
        self._pending[msg_id] = loop.create_future()
        if self._writer is None:
            raise WorkerDiedError("writer is None — worker not started or already stopped")
        self._writer.write(req.to_line().encode("utf-8"))
        await self._writer.drain()
        return msg_id

    async def _await_response(self, msg_id: str) -> dict[str, Any]:
        fut = self._pending[msg_id]
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except TimeoutError as e:
            raise IPCTimeoutError(f"no response for {msg_id} within {self._timeout}s") from e
        finally:
            self._pending.pop(msg_id, None)

    async def _send_and_await(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        msg_id = await self._send(op, params)
        return await self._await_response(msg_id)

    async def _read_loop(self) -> None:
        if self._process is None:
            raise WorkerDiedError("_read_loop called with no process")
        if self._process.stdout is None:
            raise WorkerDiedError("_read_loop called with no stdout")
        try:
            while True:
                try:
                    line = await self._process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    # F-UAT6-1: response line exceeded the configured
                    # StreamReader limit. Without this catch the reader
                    # task would die silently and the subprocess would
                    # leak — the restart-storm guard would never trip.
                    _log.error(
                        "steam_worker.ipc_response_overflow",
                        reason=str(e)[:200],
                        limit=_STDOUT_READ_LIMIT,
                    )
                    self._on_worker_died(reason="response_too_large")
                    return
                if not line:
                    self._on_worker_died(reason="stdout_closed")
                    return
                await self._on_response_line(line)
        except asyncio.CancelledError:
            return

    async def _on_response_line(self, line: bytes) -> None:
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as e:
            _log.error("steam_worker.ipc_decode_error", reason=str(e))
            return
        try:
            resp = ResponseEnvelope.from_line(text)
        except ProtocolError as e:
            _log.error("steam_worker.ipc_protocol_error", reason=str(e))
            return
        fut = self._pending.get(resp.msg_id)
        if fut is None or fut.done():
            _log.debug("steam_worker.ipc_orphan_response", msg_id=resp.msg_id)
            return
        if resp.ok:
            fut.set_result(resp.result or {})
        else:
            err = resp.error or {"kind": "Unknown", "message": ""}
            fut.set_exception(SteamWorkerError(err["kind"], err.get("message", "")))

    def _on_worker_died(self, *, reason: str) -> None:
        """Issue #95 item 2: `_max_restart_attempts` is the **budget**, not
        a deaths-allowed count. The guard fires when `_restart_attempts`
        EXCEEDS the budget — i.e. with default budget=3, deaths 1/2/3
        produce warnings + allow respawn, death 4 fires the guard.

        This off-by-one vs. the plain reading "max 3 attempts" is
        intentional and tested (see test_max_restart_attempts_exhausted).
        Operator-facing setting `steam_worker_max_restart_attempts` is
        documented in Settings + ADR-0013 with this semantics.
        """
        self._restart_attempts += 1
        if self._restart_attempts > self._max_restart_attempts:
            self._disabled = True
            _log.error(
                "steam_worker.restart_storm_guard_fired",
                attempts=self._restart_attempts,
                max=self._max_restart_attempts,
            )
        else:
            _log.warning("steam_worker.died", reason=reason, attempts=self._restart_attempts)
        # Fail all pending futures so awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WorkerDiedError(f"worker died: {reason}"))
        self._pending.clear()
        self._process = None
        self._writer = None
