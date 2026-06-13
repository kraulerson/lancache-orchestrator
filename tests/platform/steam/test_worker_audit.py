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
    AccountLoginDeniedNeedTwoFactor = "NEED_2FA"
    AccountLogonDenied = "EMAIL_CODE"


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

    for name, mod in [
        ("steam", steam),
        ("steam.monkey", monkey),
        ("steam.client", client_mod),
        ("steam.enums", enums_mod),
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

        def serialize(self):
            return b"good-bytes"

    class _BadManifest:
        depot_id = 2
        gid = 22

        def serialize(self):
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
