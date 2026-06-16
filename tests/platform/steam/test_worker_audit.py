"""Unit tests for the Steam worker subprocess (audit 2026-06-09).

worker.py is gevent-monkey-patched and imports steam-next at module load, so it
is normally only exercised live (UAT-9). These tests stub the `steam` modules in
``sys.modules`` so the worker imports cleanly, giving its handlers their first
real unit coverage. They pin four audit findings:

- SEV-3: cleartext password retained for abandoned 2FA flows (no TTL sweep).
- SEV-3: credential directory created world-traversable (no 0700).
- SEV-3: library.enumerate returns a false empty library when licenses lag.
- SEV-4: manifest.fetch leaks already-written BLOB temp files on a mid-loop raise.
"""

from __future__ import annotations

import importlib
import stat
import sys
import time
import types
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from pathlib import Path


class _EResult:
    OK = "OK"
    Fail = "Fail"
    Timeout = "Timeout"
    AccountLoginDeniedNeedTwoFactor = "NEED_2FA"
    AccountLogonDenied = "EMAIL_CODE"
    # Auth-loss results (#122) — must mirror the real steam.enums.EResult member
    # names the worker references.
    AccessDenied = "AccessDenied"
    Expired = "Expired"
    NotLoggedOn = "NotLoggedOn"
    LoggedInElsewhere = "LoggedInElsewhere"
    InvalidPassword = "InvalidPassword"
    Revoked = "Revoked"


class _SteamError(Exception):
    """Mirror of steam.exceptions.SteamError: carries an `.eresult`."""

    def __init__(self, message, eresult=_EResult.Fail):
        super().__init__(message)
        self.eresult = eresult


class _FakeSteamClient:
    def __init__(self) -> None:
        self.licenses: dict = {}
        self.steam_id = 0
        self.connected = True
        self.logged_on = True
        self.credential_location: str | None = None
        self.login_result = _EResult.OK

    def set_credential_location(self, path: str) -> None:
        self.credential_location = path

    def login(self, *args, **kwargs):
        return self.login_result


@pytest.fixture
def worker(monkeypatch):
    """Import the gevent worker with steam-next stubbed out."""
    steam = types.ModuleType("steam")
    monkey = types.ModuleType("steam.monkey")
    monkey.patch_minimal = lambda: None  # type: ignore[attr-defined]
    steam.monkey = monkey  # type: ignore[attr-defined]
    client_mod = types.ModuleType("steam.client")
    client_mod.SteamClient = _FakeSteamClient  # type: ignore[attr-defined]
    enums_mod = types.ModuleType("steam.enums")
    enums_mod.EResult = _EResult  # type: ignore[attr-defined]
    # steam.exceptions stub (#122): SteamError carries an `.eresult`; the worker
    # inspects it to map genuine auth-loss results to kind=NotAuthenticated.
    exceptions_mod = types.ModuleType("steam.exceptions")
    exceptions_mod.SteamError = _SteamError  # type: ignore[attr-defined]
    # gevent stub. gevent.Timeout subclasses BaseException (NOT Exception) by
    # design, so a bare `except Exception` can't swallow it — faithfully
    # reproduce that so tests prove the worker catches it explicitly.
    gevent_mod = types.ModuleType("gevent")

    class _Timeout(BaseException):
        pass

    gevent_mod.Timeout = _Timeout  # type: ignore[attr-defined]

    for name, mod in [
        ("steam", steam),
        ("steam.monkey", monkey),
        ("steam.client", client_mod),
        ("steam.enums", enums_mod),
        ("steam.exceptions", exceptions_mod),
        ("gevent", gevent_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("orchestrator.platform.steam.worker", None)
    mod = importlib.import_module("orchestrator.platform.steam.worker")
    # Collect IPC responses instead of writing to stdout.
    sent: list[dict] = []
    monkeypatch.setattr(mod, "_send", lambda payload: sent.append(payload))
    mod._sent = sent  # type: ignore[attr-defined]
    yield mod
    sys.modules.pop("orchestrator.platform.steam.worker", None)


def test_ensure_client_creates_credential_dir_0700(worker, tmp_path: Path) -> None:
    """The steam-next credential dir holds the long-lived refresh token — it
    must be 0700, not world-traversable."""
    cred = tmp_path / "steam_session"
    worker._client = None
    worker._ensure_client(str(cred))
    mode = stat.S_IMODE(cred.stat().st_mode)
    assert mode == 0o700, oct(mode)


def test_auth_begin_sweeps_expired_challenges(worker) -> None:
    """An abandoned 2FA flow's cleartext password must not live forever — a new
    auth.begin sweeps expired entries from the worker's _challenges dict."""
    worker._challenges.clear()
    worker._challenges["stale"] = {
        "username": "old",
        "password": "OLD_SECRET_PW",
        "expires_at": time.time() - 1,  # already expired
    }
    worker._client = _FakeSteamClient()
    worker._client.login_result = _EResult.OK  # no new challenge created

    worker._handle_auth_begin("m1", {"username": "u", "password": "p"})

    assert "stale" not in worker._challenges, "expired challenge (with password) was not swept"


def test_library_enumerate_signals_timeout_not_false_empty(worker, monkeypatch) -> None:
    """When the license list never populates, the worker must signal a timeout
    error — not reply ok{apps: []}, which the orchestrator records as a green
    empty sync."""
    worker._client = _FakeSteamClient()
    worker._client.licenses = {}  # never populates

    # Make the (now configurable) license wait return 0 immediately.
    import orchestrator.platform.steam.enumerate as enum_mod

    monkeypatch.setattr(enum_mod, "wait_for_licenses", lambda *a, **k: 0)

    worker._handle_library_enumerate("m2", {})

    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "LicenseListTimeout"


def test_manifest_fetch_cleans_temp_blobs_on_failure(worker, monkeypatch, tmp_path: Path) -> None:
    """If a depot raises after earlier depots already wrote BLOB temp files, the
    worker must delete those files before sending the error (the orchestrator
    never learns their paths on failure, so it can't clean them)."""
    # Stub the local imports the handler performs.
    zstd_mod = types.ModuleType("zstandard")

    class _Compressor:
        def compress(self, data):
            return data

    zstd_mod.ZstdCompressor = lambda level=3: _Compressor()  # type: ignore[attr-defined]
    cdn_mod = types.ModuleType("steam.client.cdn")
    cdn_mod.CDNClient = lambda client: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zstandard", zstd_mod)
    monkeypatch.setitem(sys.modules, "steam.client.cdn", cdn_mod)

    worker._client = _FakeSteamClient()

    class _Mapping:
        chunks: ClassVar[list] = []

    class _Payload:
        mappings: ClassVar[list] = [_Mapping()]

    class _Meta:
        cb_disk_original = 10

    class _GoodManifest:
        depot_id = 1
        gid = 11
        name = "ok"
        metadata = _Meta()
        payload = _Payload()

        def serialize(self, compress=True):
            return b"good-bytes"

    class _BadManifest:
        depot_id = 2
        gid = 22

        def serialize(self, compress=True):
            raise RuntimeError("malformed manifest")  # post-fetch failure

    manifests = {1: _GoodManifest(), 2: _BadManifest()}

    class _StubCdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            return 0

        def get_manifest(self, app_id, depot_id, gid, decrypt=True, manifest_request_code=0):
            return manifests[depot_id]

    worker._cdn_client = _StubCdn()
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11), (2, 22)]
    )
    # Route temp blobs into tmp_path so we can observe them.
    monkeypatch.setattr(worker, "_blob_temp_path", lambda prefix: tmp_path / f"blob-{prefix}.zst")

    worker._handle_manifest_fetch("m3", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is False  # the run failed
    leaked = list(tmp_path.glob("blob-*.zst"))
    assert leaked == [], f"leaked temp BLOB files on failure: {leaked}"


def test_manifest_fetch_gevent_timeout_does_not_crash_worker(
    worker, monkeypatch, tmp_path: Path
) -> None:
    """UAT-11 live: a slow CDN depot raised gevent.Timeout (a BaseException), which
    escaped the handler's `except Exception` and KILLED the worker process
    (steam_worker.died reason=stdout_closed). The handler must catch it and report
    a clean, retryable SteamCDNTimeout — never let it propagate."""
    zstd_mod = types.ModuleType("zstandard")
    zstd_mod.ZstdCompressor = lambda level=3: None  # type: ignore[attr-defined]
    cdn_mod = types.ModuleType("steam.client.cdn")
    cdn_mod.CDNClient = lambda client: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zstandard", zstd_mod)
    monkeypatch.setitem(sys.modules, "steam.client.cdn", cdn_mod)

    worker._client = _FakeSteamClient()

    class _TimingOutCdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            # steam-next's internal AsyncResult.get() 15s budget elapsing.
            raise worker.GeventTimeout(15)

        def get_manifest(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("should not be called")

    worker._cdn_client = _TimingOutCdn()
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )
    monkeypatch.setattr(worker, "_blob_temp_path", lambda prefix: tmp_path / f"blob-{prefix}.zst")

    # Must NOT raise — that is the crash this test pins.
    worker._handle_manifest_fetch("m4", {"app_id": "340"})

    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "SteamCDNTimeout", resp


def test_dispatch_loop_survives_handler_gevent_timeout(worker, monkeypatch) -> None:
    """Defense in depth: even if some handler lets a gevent.Timeout escape, the
    worker's main() dispatch loop must convert it to an IPC error and keep
    serving — a single slow op must never take down the worker for every
    subsequent job until a restart."""
    import io
    import json

    def _boom(msg_id, _params):
        raise worker.GeventTimeout(15)

    monkeypatch.setitem(worker._HANDLERS, "boom.timeout", _boom)
    line = json.dumps({"msg_id": "m5", "op": "boom.timeout", "params": {}}) + "\n"
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(line))

    rc = worker.main()  # must return cleanly, not propagate the BaseException

    assert rc == 0
    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "SteamCDNTimeout", resp


def _stub_cdn_modules(worker, monkeypatch, tmp_path, cdn) -> None:
    """Stub the lazy imports `_handle_manifest_fetch` performs + the CDN client
    + the blob temp path, routing blobs into tmp_path."""
    zstd_mod = types.ModuleType("zstandard")

    class _Compressor:
        def compress(self, data):
            return data

    zstd_mod.ZstdCompressor = lambda level=3: _Compressor()  # type: ignore[attr-defined]
    cdn_mod = types.ModuleType("steam.client.cdn")
    cdn_mod.CDNClient = lambda client: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zstandard", zstd_mod)
    monkeypatch.setitem(sys.modules, "steam.client.cdn", cdn_mod)
    worker._cdn_client = cdn
    monkeypatch.setattr(worker, "_blob_temp_path", lambda prefix: tmp_path / f"blob-{prefix}.zst")


def test_manifest_fetch_chunk_count_is_unique_not_summed(worker, monkeypatch, tmp_path) -> None:
    """#121 + #123.2: chunk_count must be the manifest's unique_chunks (what F7's
    SHA-deduped validate counts), NOT the sum of per-file mapping refs (which
    double-counts content-deduped chunks). And the dead `name` IPC field — which
    the orchestrator handler never consumes — must not be sent."""

    class _Meta:
        cb_disk_original = 9999
        unique_chunks = 2  # the true unique count (protobuf field 7)

    class _Mapping:
        def __init__(self, n):
            self.chunks = [object()] * n

    class _Manifest:
        depot_id = 1
        gid = 11
        metadata = _Meta()
        # 2 + 1 = 3 mapping refs, but only 2 UNIQUE chunks (one is shared).
        payload = type("P", (), {"mappings": [_Mapping(2), _Mapping(1)]})()

        def serialize(self, compress=True):
            return b"protobuf-bytes"

    class _Cdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            return 0

        def get_manifest(self, app_id, depot_id, gid, decrypt=True, manifest_request_code=0):
            return _Manifest()

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )

    worker._handle_manifest_fetch("m6", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is True, resp
    entry = resp["result"]["manifests"][0]
    assert entry["chunk_count"] == 2, "chunk_count must be unique_chunks, not the summed refs (3)"
    assert "name" not in entry, "the dead `name` field must not be sent (#123.2)"


def test_manifest_fetch_chunk_count_falls_back_when_field_absent(
    worker, monkeypatch, tmp_path
) -> None:
    """#121 robustness: if a manifest's metadata has no `unique_chunks` attribute
    (steam-next rename / older protobuf), chunk_count falls back to the summed
    mapping refs rather than dropping to 0 — so the count is never silently lost.
    (A silent revert to the double-count is surfaced via a stderr warning, which
    the orchestrator drains.)"""

    class _Meta:
        cb_disk_original = 9999
        # NO unique_chunks attribute at all.

    class _Mapping:
        def __init__(self, n):
            self.chunks = [object()] * n

    class _Manifest:
        depot_id = 1
        gid = 11
        metadata = _Meta()
        payload = type("P", (), {"mappings": [_Mapping(2), _Mapping(1)]})()

        def serialize(self, compress=True):
            return b"protobuf-bytes"

    class _Cdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            return 0

        def get_manifest(self, app_id, depot_id, gid, decrypt=True, manifest_request_code=0):
            return _Manifest()

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )

    worker._handle_manifest_fetch("m7", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is True, resp
    # No unique_chunks → fall back to the summed refs (2 + 1 = 3), not 0.
    assert resp["result"]["manifests"][0]["chunk_count"] == 3


# --- #122: EResult-based mid-fetch auth-loss detection ------------------------


def test_manifest_fetch_auth_loss_eresult_maps_to_not_authenticated(
    worker, monkeypatch, tmp_path
) -> None:
    """#122: a genuine auth-loss SteamError (e.g. NotLoggedOn) from the initial
    enumeration must surface kind=NotAuthenticated so the orchestrator's auth-flip
    (platforms.auth_status='expired') fires — not be masked as SteamAPIError."""

    class _Cdn:
        def get_app_depot_info(self, app_id):
            raise worker.SteamError("session lost", worker.EResult.NotLoggedOn)

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())

    worker._handle_manifest_fetch("m_a1", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "NotAuthenticated", resp


def test_manifest_fetch_transient_timeout_eresult_is_not_auth_loss(
    worker, monkeypatch, tmp_path
) -> None:
    """#122 (the adversarial-review SEV-2 it replaces): a slow/dropped CM gives
    resp=None -> EResult.Timeout in steam-next. That is transient/retryable and
    must NOT flip auth — otherwise a network blip forces a needless 2FA re-auth.
    kind stays SteamAPIError."""

    class _Cdn:
        def get_app_depot_info(self, app_id):
            raise worker.SteamError("timed out", worker.EResult.Timeout)

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())

    worker._handle_manifest_fetch("m_a2", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "SteamAPIError", resp


def test_manifest_fetch_per_depot_session_loss_is_not_authenticated(
    worker, monkeypatch, tmp_path
) -> None:
    """#122: a session-wide auth loss surfacing on a per-depot call (unambiguous
    EResult like NotLoggedOn) must FAIL the whole fetch as NotAuthenticated — not
    be swallowed into `skipped` as a false-partial success (the #109 lesson)."""

    class _Cdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            raise worker.SteamError("not logged on", worker.EResult.NotLoggedOn)

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )

    worker._handle_manifest_fetch("m_a3", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "NotAuthenticated", resp


def test_manifest_fetch_per_depot_access_denied_is_skipped(worker, monkeypatch, tmp_path) -> None:
    """#122: AccessDenied on a per-depot call is ambiguous — the common
    'depot not owned' case is indistinguishable from auth loss — so it stays a
    per-depot skip. The fetch succeeds with that depot reported in `skipped`,
    NOT a failed job."""

    class _Cdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            raise worker.SteamError("no access", worker.EResult.AccessDenied)

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )

    worker._handle_manifest_fetch("m_a4", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is True, resp
    assert resp["result"]["manifests"] == []
    assert len(resp["result"]["skipped"]) == 1


# --- #123.1: avoid double compression ----------------------------------------


def test_manifest_fetch_serializes_uncompressed_to_avoid_double_zstd(
    worker, monkeypatch, tmp_path
) -> None:
    """#123.1: serialize() defaults to ZIP-compressing, then we zstd it — storing
    zstd(ZIP(protobuf)). Pass compress=False so we store zstd(protobuf): smaller,
    and DepotManifest.deserialize auto-detects (so the F7 expand round-trip and
    already-stored blobs still parse)."""
    seen: dict = {}

    class _Meta:
        cb_disk_original = 10
        unique_chunks = 1

    class _Mapping:
        chunks: ClassVar[list] = [object()]

    class _Manifest:
        depot_id = 1
        gid = 11
        metadata = _Meta()
        payload = type("P", (), {"mappings": [_Mapping()]})()

        def serialize(self, compress=True):
            seen["compress"] = compress
            return b"pb"

    class _Cdn:
        def get_app_depot_info(self, app_id):
            return {}

        def get_manifest_request_code(self, app_id, depot_id, gid):
            return 0

        def get_manifest(self, app_id, depot_id, gid, decrypt=True, manifest_request_code=0):
            return _Manifest()

    worker._client = _FakeSteamClient()
    _stub_cdn_modules(worker, monkeypatch, tmp_path, _Cdn())
    monkeypatch.setattr(
        worker.enumerate_module, "manifest_gids_for_app", lambda depots, branch: [(1, 11)]
    )

    worker._handle_manifest_fetch("m_c1", {"app_id": "440"})

    resp = worker._sent[-1]
    assert resp["ok"] is True, resp
    assert seen.get("compress") is False, "serialize must be called with compress=False (#123.1)"
