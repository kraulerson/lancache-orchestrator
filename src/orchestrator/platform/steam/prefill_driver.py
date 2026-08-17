"""SteamPrefillDriver — drives the host-installed SteamPrefill binary for Steam
prefill (modern persistent auth) and reads its state/auth files. Targets specific
apps by writing selectedAppsToPrefill.json (SteamPrefill has no --app flag), and
snapshots/restores the operator's selection so it is cron-safe. NEVER logs the
account.config bytes or any token/identifier."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

# Upper bound on the retained stdout tail. A full-library prefill prints for
# hours; only the end has diagnostic value, and an unbounded buffer would grow
# with the run.
_TAIL_BYTES = 65536


@dataclass(frozen=True)
class PrefillResult:
    ok: bool
    raw: str


@dataclass(frozen=True)
class SteamAuthStatus:
    ok: bool
    reason: str = ""


class SteamPrefillDriver:
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        home: Path | None = None,
        timeout_sec: float = 36000.0,
    ) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)
        # SteamPrefill writes its manifest cache to ``$HOME/.cache/SteamPrefill``.
        # When ``home`` is set, the subprocess HOME is pinned so manifests land
        # where the durable-archive capture reads (steam_prefill_live_cache_dir),
        # regardless of the container's inherited HOME — otherwise a deploy that
        # omits ``-e HOME=/tmp`` silently strands manifests and re-introduces the
        # false-Partial bug (UAT-13 F2 / #211). None preserves prior env-inherit.
        self._home = None if home is None else Path(home)
        self._timeout_sec = float(timeout_sec)

    @property
    def _selection_path(self) -> Path:
        return self._config_dir / "selectedAppsToPrefill.json"

    def _kill_process_group_now(self, proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the process GROUP synchronously — the cancellation path.

        Deliberately not the graceful SIGTERM-then-wait of
        ``_kill_process_group``: on cancellation the task is already being torn
        down, so any ``await`` here is unreliable (it can be interrupted before
        the kill lands). A missed kill is not a cosmetic problem — the child is
        detached into its own session by ``start_new_session=True``, so a
        group-wide kill of the agent no longer reaches it, and the router's
        ``finally`` has already cleared the single-flight gate. The result is an
        orphan nothing tracks, plus a restarted agent free to launch a rival
        SteamPrefill against the same auth/cache.

        SIGKILL rather than SIGTERM is the right trade here: a half-written
        download is recoverable (lancache tolerates partials and the next
        prefill resumes), a multi-day orphan is not.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(pgid, signal.SIGKILL)

    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the process GROUP, escalating to SIGKILL after 60s.

        The group, not the process: SteamPrefill may have spawned children, and
        killing only the direct child is the host cron's known weakness — its
        `timeout` kills the docker exec client while the in-container process
        runs on (its own alert text admits this).
        """
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        _log.warning("steam_prefill.killing_process_group", pid=proc.pid, pgid=pgid, signal="TERM")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except TimeoutError:
            # Escalation is worth its own line: a group that ignores SIGTERM for
            # a full minute is the shape of the hang this driver exists to catch.
            _log.warning(
                "steam_prefill.process_group_escalated_to_sigkill", pid=proc.pid, pgid=pgid
            )
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _run(self, *extra_args: str) -> PrefillResult:
        """Run the SteamPrefill binary with the shared timeout + kill semantics.

        Shared by every prefill mode (selection, force, recently-purchased) so
        the timeout and process-group kill can never drift between them.
        """
        args = [str(self._binary), "prefill", "--no-ansi", *extra_args]
        # SteamPrefill resolves its Config/ dir RELATIVE TO the working
        # directory (./Config), not the binary path. Run it from the parent
        # of our config_dir so ./Config maps to exactly config_dir —
        # otherwise it finds no account.config and login fails (the failure
        # is masked by a Spectre.Console crash; see the 2026-06-21 flip).
        env = None if self._home is None else {**os.environ, "HOME": str(self._home)}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self._config_dir.parent),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        # Stream stdout into a bounded tail buffer rather than using
        # communicate(). On timeout, wait_for cancels communicate() and its
        # output is never bound — discarding exactly the diagnostic that matters,
        # since the motivating incident printed "Prefill complete!" and THEN hung.
        # Reading as we go means the tail survives the kill.
        tail = bytearray()
        drain = asyncio.create_task(self._drain(proc, tail))
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._timeout_sec)
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so `except Exception` never sees
            # it — the same shape as the UAT-11 gevent.Timeout escape. The agent
            # lifespan cancels every task in agent_bg_tasks on shutdown/redeploy,
            # so without this the subprocess is simply orphaned.
            drain.cancel()
            self._kill_process_group_now(proc)
            raise
        except TimeoutError:
            drain.cancel()
            await self._kill_process_group(proc)
            _log.warning(
                "steam_prefill.timeout_killed",
                timeout_sec=self._timeout_sec,
                pid=proc.pid,
                args=" ".join(extra_args) or "(selection)",
                output_tail=self._decode_tail(tail)[-500:],
            )
            return PrefillResult(
                ok=False,
                raw=(
                    f"timeout: SteamPrefill exceeded {self._timeout_sec:.0f}s and was killed\n"
                    f"--- last output before the hang ---\n{self._decode_tail(tail)}"
                ),
            )
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        return PrefillResult(ok=(proc.returncode == 0), raw=self._decode_tail(tail))

    @staticmethod
    async def _drain(proc: asyncio.subprocess.Process, tail: bytearray) -> None:
        """Accumulate the subprocess's output, keeping only a bounded tail.

        Bounded so a chatty multi-hour prefill cannot grow this without limit;
        the tail is the part with the diagnostic value.
        """
        if proc.stdout is None:  # pragma: no cover - PIPE is always requested
            return
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            tail.extend(chunk)
            if len(tail) > _TAIL_BYTES:
                del tail[:-_TAIL_BYTES]

    @staticmethod
    def _decode_tail(tail: bytearray) -> str:
        return bytes(tail).decode("utf-8", "replace")[-4000:]

    async def prefill_apps(self, app_ids: list[int], *, force: bool = False) -> PrefillResult:
        """Write our app selection, run SteamPrefill, then restore the operator's
        prior selection. Returns ok=True iff exit code 0."""
        prior = self._selection_path.read_text() if self._selection_path.exists() else None
        self._selection_path.write_text(json.dumps([int(a) for a in app_ids]))
        try:
            return await self._run(*(["--force"] if force else []))
        finally:
            if prior is not None:
                self._selection_path.write_text(prior)

    def downloaded_state(self) -> dict[int, list[int]]:
        """{app_id: [prefilled manifest GIDs]} from SteamPrefill's own record."""
        p = self._config_dir / "successfullyDownloadedDepots.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return {int(k): [int(g) for g in v] for k, v in data.items()}

    def auth_status(self) -> SteamAuthStatus:
        """account.config present => SteamPrefill is/was authed (its ~6-month token;
        SteamPrefill itself re-auths when it lapses). Precise JWT-exp parse of the
        ProtoBuf blob is a follow-up refinement."""
        if not (self._config_dir / "account.config").exists():
            return SteamAuthStatus(ok=False, reason="no_account_config")
        return SteamAuthStatus(ok=True)
