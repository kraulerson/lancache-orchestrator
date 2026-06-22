"""SteamPrefillDriver — drives the host-installed SteamPrefill binary for Steam
prefill (modern persistent auth) and reads its state/auth files. Targets specific
apps by writing selectedAppsToPrefill.json (SteamPrefill has no --app flag), and
snapshots/restores the operator's selection so it is cron-safe. NEVER logs the
account.config bytes or any token/identifier."""

from __future__ import annotations

import asyncio
import json
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
    def __init__(self, *, binary: Path, config_dir: Path) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)

    @property
    def _selection_path(self) -> Path:
        return self._config_dir / "selectedAppsToPrefill.json"

    async def prefill_apps(self, app_ids: list[int], *, force: bool = False) -> PrefillResult:
        """Write our app selection, run SteamPrefill, then restore the operator's
        prior selection. Returns ok=True iff exit code 0."""
        prior = self._selection_path.read_text() if self._selection_path.exists() else None
        self._selection_path.write_text(json.dumps([int(a) for a in app_ids]))
        try:
            args = [str(self._binary), "prefill", "--no-ansi", *(["--force"] if force else [])]
            # SteamPrefill resolves its Config/ dir RELATIVE TO the working
            # directory (./Config), not the binary path. Run it from the parent
            # of our config_dir so ./Config maps to exactly config_dir —
            # otherwise it finds no account.config and login fails (the failure
            # is masked by a Spectre.Console crash; see the 2026-06-21 flip).
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self._config_dir.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            raw = out.decode("utf-8", "replace")
            return PrefillResult(ok=(proc.returncode == 0), raw=raw[-4000:])
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
