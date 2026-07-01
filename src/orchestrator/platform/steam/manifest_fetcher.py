"""DepotDownloaderManifestFetcher — fetch Steam manifests ONLY (no chunks) via
the DepotDownloader binary, writing {app}_{app}_{depot}_{gid}.shas sidecars into
the durable manifest archive so the F7 validator covers apps SteamPrefill skips
(already-up-to-date apps never (re)write a manifest). STDLIB + subprocess only;
MUST NOT import orchestrator.api.* / orchestrator.db.* (agent import-isolation,
tests/agent/test_import_isolation.py). NEVER logs/writes the Steam password,
2FA, or any token — only manifest .shas files."""

from __future__ import annotations

import json
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


# S2: the persisted login-key filename DepotDownloader writes under config_dir
# (own-session path) — or SteamPrefill's account.config (reuse path). Confirm in S2.
_SESSION_MARKER = "account.config"


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
        """Verify a usable persisted session exists (no password, no 2FA).
        Raises SteamAuthError when absent so the caller surfaces 're-auth needed'
        instead of prompting in an unattended run."""
        if not (self._config_dir / _SESSION_MARKER).exists():
            raise SteamAuthError("no DepotDownloader session — run the one-time login")

    def _enumerate_app_ids(self) -> list[int]:
        """The cached app set, read LIVE each run (auto-grows; nothing hardcoded).
        Union of successfullyDownloadedDepots.json keys (what's cached) and
        selectedAppsToPrefill.json (selected) — S3 confirms completeness."""
        apps: set[int] = set()
        for name in ("successfullyDownloadedDepots.json", "selectedAppsToPrefill.json"):
            p = self._steam_config_dir / name
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, (dict, list)):
                continue  # scalar JSON (bare null/42) — skip, not a TypeError
            keys = data.keys() if isinstance(data, dict) else data
            for k in keys:
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
        Username is taken from _SESSION_MARKER's sibling config; DD reads the
        remembered login key from config_dir — the password is NEVER on argv."""
        results: list[tuple[int, str, set[str]]] = []
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
