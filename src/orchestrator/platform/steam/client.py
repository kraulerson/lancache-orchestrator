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
import collections
import contextlib
import os
import tempfile
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

# How many of the worker's most-recent stderr lines to retain for the
# steam_worker.died breadcrumb. The worker subprocess is opaque (separate
# gevent venv); its stderr — Python tracebacks, native "Segmentation fault"
# messages — is the only diagnostic when it dies. Bounded so a chatty worker
# can't grow this without limit.
_STDERR_RING_SIZE = 50

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
        # Per-op long-running timeout (issue #109): library_enumerate +
        # manifest_fetch traffic Steam's CM for hundreds of round-trips
        # via batched get_product_info; the default 30 s IPC budget is
        # too small for real libraries. Operations not in this map fall
        # back to `self._timeout`.
        self._op_timeout_overrides: dict[str, float] = {
            "library.enumerate": float(settings.steam_worker_library_enumerate_timeout_sec),
            "manifest.fetch": float(settings.steam_worker_manifest_fetch_timeout_sec),
            "manifest.expand": float(settings.steam_worker_manifest_expand_timeout_sec),
        }
        self._max_restart_attempts = settings.steam_worker_max_restart_attempts
        self._restart_attempts = 0
        self._disabled = False

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Ring of the worker's most-recent stderr lines, so a crash (stdout EOF)
        # carries the traceback/native-fault message that caused it.
        self._recent_stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_RING_SIZE)
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # The worker handles IPC strictly serially. Single-flight manifest_expand
        # so concurrent callers (the F13 sweep) serialize here instead of queuing
        # head-of-line at the worker with their per-request timeout clock already
        # running — which spuriously timed out trailing requests. Uncontended for
        # the single-caller paths (F7 validate, F5 prefill).
        self._manifest_expand_lock = asyncio.Lock()

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
        # Continuously drain stderr: (1) so the OS pipe buffer (~64 KiB) never
        # fills and blocks the worker mid-fetch, and (2) so worker tracebacks and
        # native crash messages are logged and retained for the death breadcrumb.
        self._stderr_task = asyncio.create_task(self._drain_stderr())
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
        if self._stderr_task is not None:
            self._stderr_task.cancel()

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

    async def manifest_fetch(self, app_id: int) -> dict[str, Any]:
        """Ask the worker to fetch all depot manifests for a Steam app (BL12).

        Returns `{"manifests": [{depot_id, manifest_gid, name, total_bytes,
        chunk_count, raw_b64}, ...]}`. Raises `SteamWorkerError(kind=
        'NotAuthenticated')` if no Steam session.
        """
        return await self._send_and_await("manifest.fetch", {"app_id": app_id})

    async def manifest_expand(self, raw: bytes) -> dict[str, Any]:
        """Deserialize a stored manifest BLOB in the worker venv (F7).

        Offline — no Steam session required. `raw` is the `zstd(protobuf)`
        bytes stored in `manifests.raw`. Returns `{"depot_id": int,
        "chunk_shas": [hex, ...]}` (deduped).

        S2-2: the BLOB is handed to the worker via a temp file on the shared
        container FS (its path travels in the IPC line, not ~170 MB of
        base64). The worker deletes it after reading; we clean up on the
        error path as a fallback.
        """
        # Serialize the worker-bound expand (see _manifest_expand_lock). Holding
        # the lock across the temp-file write also bounds on-disk blobs to one at
        # a time under a concurrent sweep.
        async with self._manifest_expand_lock:
            blob_path = os.path.join(tempfile.gettempdir(), f"orch-expand-{uuid.uuid4().hex}.zst")
            with open(blob_path, "wb") as fh:
                fh.write(raw)
            try:
                return await self._send_and_await("manifest.expand", {"raw_path": blob_path})
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(blob_path)

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

    async def _await_response(self, msg_id: str, timeout: float | None = None) -> dict[str, Any]:
        fut = self._pending[msg_id]
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            return await asyncio.wait_for(fut, timeout=effective_timeout)
        except TimeoutError as e:
            raise IPCTimeoutError(f"no response for {msg_id} within {effective_timeout}s") from e
        finally:
            self._pending.pop(msg_id, None)

    async def _send_and_await(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        msg_id = await self._send(op, params)
        timeout = self._op_timeout_overrides.get(op)
        return await self._await_response(msg_id, timeout=timeout)

    async def _drain_stderr(self) -> None:
        """Read the worker's stderr line-by-line until EOF.

        Without this the stderr PIPE is never consumed: a chatty worker would
        fill the ~64 KiB OS buffer and block on its next write (stalling the
        whole gevent hub), and — worse — when the worker dies we'd have no record
        of the traceback or native fault that killed it. Each line is logged and
        kept in `_recent_stderr` so `_on_worker_died` can attach the tail.
        """
        if self._process is None or self._process.stderr is None:
            return
        stderr = self._process.stderr
        try:
            while True:
                try:
                    raw = await stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # An over-long stderr line (no newline within the buffer
                    # limit). Drop it rather than letting the drain task die,
                    # which would re-expose the pipe-fill stall.
                    continue
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                self._recent_stderr.append(line)
                _log.warning("steam_worker.stderr", line=line[:500])
        except asyncio.CancelledError:
            return

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
                recent_stderr=list(self._recent_stderr)[-10:],
            )
        else:
            _log.warning(
                "steam_worker.died",
                reason=reason,
                attempts=self._restart_attempts,
                recent_stderr=list(self._recent_stderr)[-10:],
            )
        # Fail all pending futures so awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WorkerDiedError(f"worker died: {reason}"))
        self._pending.clear()
        self._process = None
        self._writer = None
