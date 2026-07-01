import contextlib
import json
import shutil

import pytest

from orchestrator.platform.steam.manifest_fetcher import (
    DepotDownloaderManifestFetcher,
    FetchResult,
    SteamAuthError,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _make_session(config_dir):
    """Create the .NET IsolatedStorage account.config DepotDownloader persists
    under HOME (=config_dir), so login_from_session's glob finds it."""
    p = config_dir / ".local/share/IsolatedStorage/aa/bb/cc/AssemFiles/account.config"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00token")
    return p


def _fetcher(tmp_path, **kw):
    return DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=kw.get("config_dir", tmp_path / "dd-config"),
        steam_config_dir=kw.get("steam_config_dir", tmp_path / "Config"),
        archive_dir=kw.get("archive_dir", tmp_path / "archive"),
        delay_sec=0.0,
    )


def _fetcher_with_fake_dd(tmp_path, manifests):
    """manifests: {app_id: [(depot_id, gid, [shas])]} the fake DD 'returns'."""
    cfg = tmp_path / "dd-config"
    cfg.mkdir()
    _make_session(cfg)
    steam_cfg = tmp_path / "Config"
    steam_cfg.mkdir()
    # Enumeration reads selectedAppsToPrefill.json (a list of clean store app_ids).
    (steam_cfg / "selectedAppsToPrefill.json").write_text(json.dumps([int(a) for a in manifests]))
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=cfg,
        steam_config_dir=steam_cfg,
        archive_dir=tmp_path / "archive",
        delay_sec=0.0,
    )
    # Monkeypatch the per-app DD call to return the canned manifests (S1 locks the
    # real subprocess+parse; here we test enumerate/write/isolate/idempotency).
    f._run_manifest_only = lambda app_id: [  # type: ignore[method-assign]
        (d, g, set(s)) for (d, g, s) in manifests.get(app_id, [])
    ]
    return f


def test_login_from_session_raises_when_no_session(tmp_path):
    f = _fetcher(tmp_path)  # config_dir has no login key
    with pytest.raises(SteamAuthError):
        f.login_from_session()


def test_login_from_session_ok_when_isolated_storage_present(tmp_path):
    cfg = tmp_path / "dd-config"
    cfg.mkdir()
    _make_session(cfg)  # the .NET IsolatedStorage account.config DD persists
    _fetcher(tmp_path, config_dir=cfg).login_from_session()  # no raise


def test_fetch_result_fields():
    r = FetchResult(fetched=3, skipped=1, failed=0, apps=4)
    assert (r.fetched, r.skipped, r.failed, r.apps) == (3, 1, 0, 4)


def test_fetch_all_writes_shas_per_depot(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A, _SHA_B])]})
    r = f.fetch_all()
    out = tmp_path / "archive" / "v1" / "440_440_441_777.shas"
    assert out.exists()
    assert sorted(out.read_text().split()) == sorted([_SHA_A, _SHA_B])
    assert (r.fetched, r.apps) == (1, 1)


def test_fetch_all_idempotent_skip_existing(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A])]})
    f.fetch_all()
    r2 = f.fetch_all()  # second run: already archived
    assert r2.skipped == 1 and r2.fetched == 0


def test_fetch_all_isolates_per_app_failure(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A])], 730: []})

    def boom(app_id):
        if app_id == 730:
            raise RuntimeError("DD blew up on 730")
        return [(441, "777", {_SHA_A})]

    f._run_manifest_only = boom  # type: ignore[method-assign]
    r = f.fetch_all()
    assert r.failed == 1 and r.fetched == 1 and r.apps == 2  # 730 failed, 440 ok


def test_fetch_all_raises_auth_when_no_session(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: []})
    shutil.rmtree(f._config_dir / ".local")  # remove the IsolatedStorage session
    with pytest.raises(SteamAuthError):
        f.fetch_all()


def test_username_default_is_empty(tmp_path):
    """username= param defaults to '' — existing callers without it stay green."""
    f = _fetcher(tmp_path)
    assert f._username == ""


def test_username_stored_when_provided(tmp_path):
    """Explicit username= is stored on the fetcher."""
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=tmp_path / "dd-config",
        steam_config_dir=tmp_path / "Config",
        archive_dir=tmp_path / "archive",
        delay_sec=0.0,
        username="steamjoe",
    )
    assert f._username == "steamjoe"


def test_run_manifest_only_includes_username_in_argv(tmp_path, monkeypatch):
    """-username <user> appears in the subprocess argv when username is non-empty."""
    captured: list[list[str]] = []

    def _fake_run(argv, **kw):
        captured.append(argv)

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr("subprocess.run", _fake_run)
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=tmp_path / "dd-config",
        steam_config_dir=tmp_path / "Config",
        archive_dir=tmp_path / "archive",
        delay_sec=0.0,
        username="steamjoe",
    )
    # After I2: _run_manifest_only raises RuntimeError when returncode==0 but no
    # .manifest files were found (the fake subprocess writes no files). Swallow it —
    # the test cares only that the argv was constructed correctly, not the result.
    with contextlib.suppress(RuntimeError):
        f._run_manifest_only(440)
    assert captured, "subprocess.run was not called"
    argv = captured[0]
    assert "-username" in argv
    assert argv[argv.index("-username") + 1] == "steamjoe"


def test_run_manifest_only_pins_home_to_config_dir(tmp_path, monkeypatch):
    """DD's -remember-password session lives in .NET IsolatedStorage under HOME,
    so the subprocess HOME is pinned to config_dir (the persistent mount)."""
    captured: dict = {}

    def _fake_run(argv, **kw):
        captured["env"] = kw.get("env")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr("subprocess.run", _fake_run)
    cfg = tmp_path / "dd-config"
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=cfg,
        steam_config_dir=tmp_path / "Config",
        archive_dir=tmp_path / "archive",
        delay_sec=0.0,
        username="steamjoe",
    )
    with contextlib.suppress(RuntimeError):
        f._run_manifest_only(440)
    assert captured["env"]["HOME"] == str(cfg)


def test_write_shas_empty_returns_false_no_file(tmp_path):
    """_write_shas returns False and writes NO file when the SHA set has no valid SHAs."""
    f = _fetcher(tmp_path)
    result = f._write_shas(440, 441, "777", set())
    assert result is False
    out = tmp_path / "archive" / "v1" / "440_440_441_777.shas"
    assert not out.exists()


def test_fetch_all_raises_on_total_failure(tmp_path):
    """fetch_all raises RuntimeError when _run_manifest_only raises for every app."""
    f = _fetcher_with_fake_dd(
        tmp_path, {440: [(441, "777", [_SHA_A])], 730: [(731, "888", [_SHA_B])]}
    )

    def always_boom(app_id: int):  # type: ignore[return]
        raise RuntimeError("DD completely blew up")

    f._run_manifest_only = always_boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="manifest fetch failed for all"):
        f.fetch_all()


def test_fetch_all_skipped_all_archived_no_raise(tmp_path):
    """fetch_all does NOT raise (and returns skipped==N) when every app is already archived.

    The total-failure raise condition requires failed > 0 AND fetched == skipped == 0.
    When every depot is skipped (shas file existed), failed==0 so no raise fires.
    """
    f = _fetcher_with_fake_dd(
        tmp_path, {440: [(441, "777", [_SHA_A])], 730: [(731, "888", [_SHA_B])]}
    )
    f.fetch_all()  # first run archives both
    r = f.fetch_all()  # second run: all skipped, no raise
    assert r.skipped == 2 and r.fetched == 0 and r.failed == 0


def test_enumerate_app_ids_skips_scalar_json(tmp_path):
    """A selectedAppsToPrefill.json that contains bare null/42 (scalar) must not
    raise TypeError — the file is silently treated as empty."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text("null")
    f = _fetcher(tmp_path, steam_config_dir=cfg)
    assert f._enumerate_app_ids() == []


def test_enumerate_reads_selected_not_downloaded_depots(tmp_path):
    """Go-live fix: enumerate the clean store app_ids in selectedAppsToPrefill.json
    and IGNORE successfullyDownloadedDepots.json (whose keys are content/depot ids
    DepotDownloader can't token)."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text(json.dumps([258090, 207650, 92300]))
    # A successfullyDownloadedDepots.json with different (content/depot) keys must
    # NOT be picked up.
    (cfg / "successfullyDownloadedDepots.json").write_text(
        json.dumps({"1968731": [1], "2900140": [2]})
    )
    f = _fetcher(tmp_path, steam_config_dir=cfg)
    assert f._enumerate_app_ids() == [92300, 207650, 258090]


def test_enumerate_missing_selection_returns_empty(tmp_path):
    """No selectedAppsToPrefill.json -> empty (no raise)."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    assert _fetcher(tmp_path, steam_config_dir=cfg)._enumerate_app_ids() == []
