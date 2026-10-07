"""SteamManifestFetcher: fetch Steam manifests ONLY (no chunks) through the
one-login SteamManifestHelper (#361), writing {app}_{app}_{depot}_{gid}.shas
sidecars into the durable manifest archive so the F7 validator covers apps
SteamPrefill skips. STDLIB + subprocess only; MUST NOT import orchestrator.api.* /
orchestrator.db.* (agent import-isolation, tests/agent/test_import_isolation.py).
NEVER sees, logs or writes the Steam token. Only the helper reads it."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from orchestrator.platform.steam.steamkit_manifest_parser import parse_steamkit_manifest

_log = structlog.get_logger(__name__)

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")  # COR-2: drop non-canonical chunk ids


class SteamAuthError(Exception):
    """No usable DepotDownloader/SteamPrefill session — operator must log in once."""


class HelperSessionError(RuntimeError):
    """The helper's Steam session stopped early: login refused (exit 2), session
    import failed (exit 3), or the connection dropped twice (exit 4). Raised AFTER
    the manifests it did fetch are archived, so partial progress is kept."""


_HELPER_STOPS = {2: "login failed", 3: "session import failed", 4: "connection lost"}
_MANIFEST_NAME_RE = re.compile(r"(?P<depot>\d+)_(?P<gid>\d+)\.manifest")


@dataclass(frozen=True)
class _HelperRun:
    returncode: int
    apps: dict[int, dict[str, object]]
    summary: dict[str, object] | None
    stderr_tail: str


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


class SteamManifestFetcher:
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        steam_config_dir: Path,
        archive_dir: Path,
        username: str = "",
        timeout_sec: float = 7200.0,
        manifest_cache_dir: Path | None = None,
    ) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)
        self._steam_config_dir = Path(steam_config_dir)
        self._archive_dir = Path(archive_dir)
        self._username = username
        self._timeout_sec = timeout_sec
        # Live SteamPrefill .bin manifest cache (e.g. /steamprefill-cache). When
        # set, enumeration also covers apps prefilled outside the selection.
        self._manifest_cache_dir = Path(manifest_cache_dir) if manifest_cache_dir else None
        # The helper keeps its own session here, imported once from DepotDownloader's.
        self._session_dir = self._config_dir / "steam-manifest-helper"

    def login_from_session(self) -> None:
        """A run can proceed if the helper already holds a session, or if
        DepotDownloader left one it can import. Neither: SteamAuthError, so the
        caller surfaces 're-auth needed' instead of prompting in an unattended run."""
        if (self._session_dir / "session.json").exists():
            return
        if any(self._config_dir.glob(_SESSION_GLOB)):
            return
        raise SteamAuthError(
            "no Steam session: run SteamManifestHelper login --username <user> "
            f"--session-dir {self._session_dir}"
        )

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
        # Durability (#213 follow-up): also cover apps prefilled OUTSIDE the
        # selection — a `.bin` with no `.shas` yet (e.g. a `--recently-purchased`
        # game). Bounded to that delta so the first run stays small: it asks the
        # helper for the new apps only, not the whole library (#228). Since #361
        # the run is one Steam login whatever its size, so this bounds run size,
        # not logins.
        #
        # Look for the `.bin` in BOTH roots. It was previously read only from the
        # manifest cache dir, but the agent's archive-sync loop writes
        # newly-prefilled manifests to the ARCHIVE — so the net that exists
        # precisely for recently-purchased games never saw them. Confirmed live
        # 2026-08-17: 11 newly-purchased games had their `.bin` in the archive and
        # zero in the manifest cache.
        have_bin = self._app_ids_with_ext(self._manifest_cache_dir, "bin") | self._app_ids_with_ext(
            self._archive_dir, "bin"
        )
        have_shas = self._app_ids_with_ext(self._archive_dir, "shas")
        return sorted(apps | (have_bin - have_shas))

    @staticmethod
    def _app_ids_with_ext(directory: Path | None, ext: str) -> set[int]:
        """app_ids that have a ``<dir>/v1/{app}_*.{ext}`` manifest. Inlined (no
        ``manifest_locator`` import) to preserve agent import-isolation."""
        apps: set[int] = set()
        if directory is None:
            return apps
        v1 = directory / "v1"
        if not v1.is_dir():
            return apps
        for path in v1.glob(f"*.{ext}"):
            first = path.stem.split("_", 1)[0]
            if first.isdigit():
                apps.add(int(first))
        return apps

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

    def _run_helper(self, app_ids: list[int], scratch: Path) -> _HelperRun:
        apps_file = scratch / "apps.txt"
        apps_file.write_text("".join(f"{a}\n" for a in app_ids))
        argv = [
            str(self._binary),
            "fetch",
            "--apps",
            str(apps_file),
            "--out",
            str(scratch / "manifests"),
            "--session-dir",
            str(self._session_dir),
            "--username",
            self._username,
            "--import-from",
            str(self._config_dir),
        ]
        try:
            proc = subprocess.run(  # noqa: S603  argv list, no shell
                argv, capture_output=True, text=True, timeout=self._timeout_sec
            )
        except subprocess.TimeoutExpired as e:
            # TimeoutExpired carries bytes even with text=True.
            err = (
                e.stderr.decode("utf-8", "replace")
                if isinstance(e.stderr, bytes)
                else (e.stderr or "")
            )
            _log.error(
                "manifest_fetch.helper_timed_out",
                timeout_sec=self._timeout_sec,
                stderr_tail=err[-500:],
            )
            raise RuntimeError(
                f"steam manifest helper timed out after {self._timeout_sec:.0f}s;"
                f" stderr tail: {err[-300:]}"
            ) from e
        requested = set(app_ids)
        apps: dict[int, dict[str, object]] = {}  # last line per app wins
        summary: dict[str, object] | None = None
        for line in proc.stdout.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # a stray non-JSON line is not data
            if not isinstance(obj, dict):
                continue
            if obj.get("summary") is True:
                summary = obj
            elif "app" in obj and "status" in obj:
                app = obj["app"]
                if not isinstance(app, int) or isinstance(app, bool) or app not in requested:
                    _log.warning(
                        "manifest_fetch.app_unrequested",
                        app_id=app if isinstance(app, int) else str(app)[:40],
                    )
                    continue
                apps[app] = obj
        return _HelperRun(proc.returncode, apps, summary, (proc.stderr or "")[-500:])

    def _archive_app(self, app_id: int, app_dir: Path, names: object) -> tuple[int, int] | None:
        """Archive one ok app's manifests. Returns (fetched, skipped), or None when
        the helper listed no readable manifest for it. Names must match
        <depot>_<gid>.manifest exactly, so a listed name can never escape app_dir."""
        if not isinstance(names, list):
            return None
        fetched = skipped = 0
        seen = False
        for name in names:
            match = _MANIFEST_NAME_RE.fullmatch(name) if isinstance(name, str) else None
            # `name` is only joined onto app_dir after the pattern check passed.
            path = app_dir / name if match is not None else None
            if match is None or path is None or not path.is_file():
                _log.warning(
                    "manifest_fetch.manifest_name_skipped",
                    app_id=app_id,
                    name=str(name)[:200],
                    pattern_ok=match is not None,
                )
                continue
            seen = True
            shas = parse_steamkit_manifest(path.read_bytes())
            if self._write_shas(app_id, int(match["depot"]), match["gid"], shas):
                fetched += 1
            else:
                skipped += 1
        return (fetched, skipped) if seen else None

    @staticmethod
    def _log_helper_failed(run: _HelperRun, session: str, reason: str) -> None:
        """The helper's stderr is its human log. Log it whole-ish BEFORE raising:
        the router truncates the exception text, cutting the final error line."""
        _log.error(
            "manifest_fetch.helper_failed",
            helper_exit=run.returncode,
            session=session,
            reason=reason,
            stderr_tail=run.stderr_tail,
        )

    def fetch_all(self) -> FetchResult:
        """One run: verify a session exists, enumerate the cached app set, run the
        helper ONCE for all of it, and archive .shas sidecars. Per-app failures are
        counted; a session that stopped early raises AFTER archiving what it got."""
        self.login_from_session()
        app_ids = self._enumerate_app_ids()
        fetched = skipped = failed = not_attempted = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                scratch = Path(tmp)
                run = self._run_helper(app_ids, scratch)
                for app_id, line in run.apps.items():
                    status = line.get("status")
                    if status == "not_attempted":
                        not_attempted += 1
                        continue
                    written = (
                        self._archive_app(
                            app_id, scratch / "manifests" / str(app_id), line.get("manifests")
                        )
                        if status == "ok"
                        else None
                    )
                    if written is None:
                        failed += 1
                        reason = line.get("reason") or "helper reported ok but produced no manifest"
                        _log.warning(
                            "manifest_fetch.app_failed",
                            app_id=app_id,
                            status=status,
                            reason=str(reason)[:200],
                        )
                        continue
                    fetched += written[0]
                    skipped += written[1]
                if run.returncode == 0:
                    # A clean exit that never mentioned an app is a failure for it.
                    # After a session stop (exit 2/3/4) the raise below reports it.
                    for app_id in app_ids:
                        if app_id not in run.apps:
                            failed += 1
                            _log.warning("manifest_fetch.app_unreported", app_id=app_id)
        except BaseException as e:  # ③: a timeout-style escape must not kill the agent silently
            _log.error("manifest_fetch.run_aborted", reason=f"{type(e).__name__}: {e}"[:200])
            raise
        summary = run.summary or {}
        session = str(summary.get("session") or "")
        summary_reason = str(summary.get("reason") or "")[:300]
        _log.info(
            "manifest_fetch.done",
            apps=len(app_ids),
            fetched=fetched,
            skipped=skipped,
            failed=failed,
            not_attempted=not_attempted,
            helper_exit=run.returncode,
            session=session,
            reason=summary_reason,
            stderr_tail=run.stderr_tail,
        )
        if run.returncode in _HELPER_STOPS:
            reason = summary_reason or _HELPER_STOPS[run.returncode]
            self._log_helper_failed(run, session, summary_reason)
            raise HelperSessionError(
                f"steam manifest helper stopped ({_HELPER_STOPS[run.returncode]}): {reason}"
                f" | fetched={fetched} skipped={skipped} failed={failed}"
                f" not_attempted={not_attempted}"
            )
        if run.returncode != 0 or run.summary is None:
            self._log_helper_failed(run, session, summary_reason)
            raise RuntimeError(
                f"steam manifest helper failed (exit {run.returncode});"
                f" stderr tail: {run.stderr_tail[-300:]}"
            )
        if fetched == 0 and skipped == 0 and failed > 0:
            raise RuntimeError(f"manifest fetch failed for all {failed} apps")
        return FetchResult(fetched=fetched, skipped=skipped, failed=failed, apps=len(app_ids))
