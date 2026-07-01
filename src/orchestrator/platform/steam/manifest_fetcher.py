"""DepotDownloaderManifestFetcher — fetch Steam manifests ONLY (no chunks) via
the DepotDownloader binary, writing {app}_{app}_{depot}_{gid}.shas sidecars into
the durable manifest archive so the F7 validator covers apps SteamPrefill skips
(already-up-to-date apps never (re)write a manifest). STDLIB + subprocess only;
MUST NOT import orchestrator.api.* / orchestrator.db.* (agent import-isolation,
tests/agent/test_import_isolation.py). NEVER logs/writes the Steam password,
2FA, or any token — only manifest .shas files."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from orchestrator.platform.steam.steamkit_manifest_parser import parse_steamkit_manifest

_log = structlog.get_logger(__name__)

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")  # COR-2: drop non-canonical chunk ids


class SteamAuthError(Exception):
    """No usable DepotDownloader/SteamPrefill session — operator must log in once."""


@dataclass(frozen=True)
class FetchResult:
    fetched: int
    skipped: int
    failed: int
    apps: int


# DepotDownloader persists its -remember-password session in .NET IsolatedStorage
# under $HOME (we run DD with HOME=config_dir), at a hash-named path like
# config_dir/.local/share/IsolatedStorage/<a>/<b>/<c>/AssemFiles/account.config
# (go-live finding 2026-07-01 — the earlier "config_dir/account.config" marker was
# wrong; DD never writes the account store to the working dir).
_SESSION_GLOB = ".local/share/IsolatedStorage/**/account.config"


class DepotDownloaderManifestFetcher:
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        steam_config_dir: Path,
        archive_dir: Path,
        delay_sec: float = 0.0,
        username: str = "",
    ) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)
        self._steam_config_dir = Path(steam_config_dir)
        self._archive_dir = Path(archive_dir)
        self._delay_sec = delay_sec
        self._username = username

    def login_from_session(self) -> None:
        """Verify a usable persisted DepotDownloader session exists (no password,
        no 2FA) — the .NET IsolatedStorage account.config under config_dir (see
        _SESSION_GLOB). Raises SteamAuthError when absent so the caller surfaces
        're-auth needed' instead of prompting in an unattended run."""
        if not any(self._config_dir.glob(_SESSION_GLOB)):
            raise SteamAuthError("no DepotDownloader session — run the one-time login")

    def _enumerate_app_ids(self) -> list[int]:
        """Store app_ids to fetch, read LIVE from SteamPrefill's SELECTION each run
        (auto-grows; nothing hardcoded). Uses selectedAppsToPrefill.json — the
        operator's clean store app_ids. NOT successfullyDownloadedDepots.json: its
        keys include content/depot ids DepotDownloader can't get an access token
        for ("Insufficient privileges", go-live finding 2026-07-01) and which the
        locator module already flags as an unreliable index."""
        p = self._steam_config_dir / "selectedAppsToPrefill.json"
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        apps: set[int] = set()
        for k in data:
            try:
                apps.add(int(k))
            except (TypeError, ValueError):
                continue
        return sorted(apps)

    def _write_shas(self, app_id: int, depot_id: int, gid: str, shas: set[str]) -> bool:
        """Write {app}_{app}_{depot}_{gid}.shas (one lowercase 40-hex SHA1/line).
        Idempotent: returns False if the file already exists OR if the filtered
        SHA set is empty (never persist an empty sidecar). Append-only archive."""
        v1 = self._archive_dir / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        out = v1 / f"{app_id}_{app_id}_{depot_id}_{gid}.shas"
        if out.exists():
            return False
        clean = sorted(s for s in shas if _SHA1_RE.match(s))
        if not clean:
            return False
        out.write_text("\n".join(clean) + "\n")
        return True

    def _run_manifest_only(self, app_id: int) -> list[tuple[int, str, set[str]]]:
        """Shell out to DepotDownloader with -manifest-only for one app.
        Returns [(depot_id, gid, chunk_shas)] for every depot manifest written.
        DD reads its remembered login key from the .NET IsolatedStorage under
        HOME, so the subprocess HOME is pinned to config_dir (the persistent
        mount) — the password is NEVER on argv and never persisted by us."""
        results: list[tuple[int, str, set[str]]] = []
        env = {**os.environ, "HOME": str(self._config_dir)}
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            argv = [
                str(self._binary),
                "-app",
                str(app_id),
                "-manifest-only",
                "-os",
                "windows",
                "-osarch",
                "64",
                "-remember-password",
                "-dir",
                scratch,
            ]
            if self._username:
                argv.extend(["-username", self._username])
            proc = subprocess.run(  # noqa: S603
                argv,
                cwd=str(self._config_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            manifest_paths = list(scratch_path.rglob("*.manifest"))
            if proc.returncode != 0 or not manifest_paths:
                _log.warning(
                    "manifest_fetch.dd_nonzero",
                    app_id=app_id,
                    returncode=proc.returncode,
                    stderr=proc.stderr[:500],
                )
                raise RuntimeError(
                    f"DepotDownloader produced no manifest for app {app_id} (rc={proc.returncode})"
                )
            # Discover all .manifest files written by DD under scratch/depots/
            for manifest_path in manifest_paths:
                stem = manifest_path.stem  # e.g. "441_777"
                parts = stem.split("_", 1)
                if len(parts) != 2:
                    continue
                try:
                    depot_id = int(parts[0])
                except ValueError:
                    continue
                gid = parts[1]
                shas = parse_steamkit_manifest(manifest_path.read_bytes())
                results.append((depot_id, gid, shas))
        return results

    def fetch_all(self) -> FetchResult:
        """One run: verify session, enumerate the cached app set, fetch each app's
        manifests (no chunks) and archive .shas sidecars. Per-app failures are
        isolated and counted; a hard BaseException boundary guarantees a
        timeout-style escape can never kill the agent (the ③ lesson)."""
        self.login_from_session()
        app_ids = self._enumerate_app_ids()
        fetched = skipped = failed = 0
        try:
            for i, app_id in enumerate(app_ids):
                try:
                    for depot_id, gid, shas in self._run_manifest_only(app_id):
                        if self._write_shas(app_id, depot_id, gid, shas):
                            fetched += 1
                        else:
                            skipped += 1
                except Exception as e:  # isolate one bad app, keep going
                    failed += 1
                    _log.warning(
                        "manifest_fetch.app_failed",
                        app_id=app_id,
                        reason=f"{type(e).__name__}: {e}"[:200],
                    )
                if self._delay_sec and i + 1 < len(app_ids):
                    time.sleep(self._delay_sec)  # throttle Steam logons (S1)
        except BaseException as e:  # ③: gevent.Timeout-style escape must not kill the agent
            _log.error("manifest_fetch.run_aborted", reason=f"{type(e).__name__}: {e}"[:200])
            raise
        _log.info(
            "manifest_fetch.done",
            apps=len(app_ids),
            fetched=fetched,
            skipped=skipped,
            failed=failed,
        )
        if fetched == 0 and skipped == 0 and failed > 0:
            raise RuntimeError(
                f"manifest fetch failed for all {failed} apps"
                " — DepotDownloader session likely expired"
            )
        return FetchResult(fetched=fetched, skipped=skipped, failed=failed, apps=len(app_ids))
