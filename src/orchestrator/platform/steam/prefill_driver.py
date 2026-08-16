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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except TimeoutError:
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
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_sec)
        except TimeoutError:
            await self._kill_process_group(proc)
            return PrefillResult(
                ok=False,
                raw=f"timeout: SteamPrefill exceeded {self._timeout_sec:.0f}s and was killed",
            )
        raw = out.decode("utf-8", "replace")
        return PrefillResult(ok=(proc.returncode == 0), raw=raw[-4000:])

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

    async def prefill_recent(self) -> PrefillResult:
        """Prefill newly-purchased apps using SteamPrefill's own discovery.

        Deliberately does NOT touch selectedAppsToPrefill.json: SteamPrefill
        derives the recent set itself, so there is no selection to save or
        restore, and no window in which a concurrent reader would see our
        temporary list. Never passes --force (spec OQ4: force is operator-only).
        """
        return await self._run("--recently-purchased")

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
