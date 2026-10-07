import json
import shutil
import struct
import sys

import pytest
from structlog.testing import capture_logs

from orchestrator.platform.steam.manifest_fetcher import (
    FetchResult,
    HelperSessionError,
    SteamAuthError,
    SteamManifestFetcher,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _make_session(config_dir):
    """Create the .NET IsolatedStorage account.config DepotDownloader persists
    under HOME (=config_dir), which the helper imports from."""
    p = config_dir / ".local/share/IsolatedStorage/aa/bb/cc/AssemFiles/account.config"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00token")
    return p


def _manifest_bytes(shas):
    """A minimal SteamKit2 manifest payload holding these chunk SHAs, built the
    same way as tests/platform/steam/test_steamkit_manifest_parser.py."""

    def ld(field, payload):
        return bytes([(field << 3) | 2, len(payload)]) + payload

    filemap = b"".join(ld(6, ld(1, bytes.fromhex(s))) for s in shas)
    payload = ld(1, filemap)
    return struct.pack("<II", 0x71F617D0, len(payload)) + payload


def _fetcher(tmp_path, **kw):
    return SteamManifestFetcher(
        binary=kw.get("binary", tmp_path / "SteamManifestHelper"),
        config_dir=kw.get("config_dir", tmp_path / "dd-config"),
        steam_config_dir=kw.get("steam_config_dir", tmp_path / "Config"),
        archive_dir=kw.get("archive_dir", tmp_path / "archive"),
        username="kraulerson",
        timeout_sec=kw.get("timeout_sec", 30.0),
        manifest_cache_dir=kw.get("manifest_cache_dir"),
    )


def _fake_helper(tmp_path, *, lines, exit_code=0, manifests=None, extra_stdout="", sleep=0.0):
    """Write an executable stand-in for SteamManifestHelper. It records its argv and
    the app list it was given, writes `manifests` ({app: {name: [shas]}}) under
    --out, prints `lines` as JSON (plus `extra_stdout`), and exits `exit_code`."""
    spec = {
        "lines": lines,
        "exit": exit_code,
        "manifests": {
            str(app): {n: _manifest_bytes(s).hex() for n, s in files.items()}
            for app, files in (manifests or {}).items()
        },
        "extra": extra_stdout,
        "sleep": sleep,
        "record": str(tmp_path),
    }
    # A shebang cannot carry a path with a space (this repo lives under "Claude
    # Projects"), so the executable is a /bin/sh launcher that execs the interpreter.
    impl = tmp_path / "helper_impl.py"
    script = tmp_path / "SteamManifestHelper"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
    impl.write_text(
        "import json, pathlib, sys, time\n"
        f"spec = json.loads({json.dumps(json.dumps(spec))})\n"
        "rec = pathlib.Path(spec['record'])\n"
        "with open(rec / 'calls.log', 'a') as f: f.write('call\\n')\n"
        "(rec / 'argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "opts = dict(zip(sys.argv[2::2], sys.argv[3::2]))\n"
        "(rec / 'apps-seen.txt').write_text(pathlib.Path(opts['--apps']).read_text())\n"
        "for app, files in spec['manifests'].items():\n"
        "    d = pathlib.Path(opts['--out']) / app\n"
        "    d.mkdir(parents=True, exist_ok=True)\n"
        "    for name, hexbytes in files.items(): (d / name).write_bytes(bytes.fromhex(hexbytes))\n"
        "time.sleep(spec['sleep'])\n"
        "if spec['extra']: print(spec['extra'])\n"
        "for line in spec['lines']: print(json.dumps(line))\n"
        "sys.stderr.write('helper log line\\n')\n"
        "sys.exit(spec['exit'])\n"
    )
    script.chmod(0o755)
    return script


def _setup(tmp_path, apps):
    cfg = tmp_path / "dd-config"
    cfg.mkdir()
    _make_session(cfg)
    steam_cfg = tmp_path / "Config"
    steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text(json.dumps(apps))


_DONE = {"summary": True, "session": "completed", "reason": ""}


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


def test_fetch_all_raises_auth_when_no_session(tmp_path):
    _setup(tmp_path, [440])
    shutil.rmtree(tmp_path / "dd-config" / ".local")  # remove the IsolatedStorage session
    with pytest.raises(SteamAuthError):
        _fetcher(tmp_path).fetch_all()


def test_write_shas_empty_returns_false_no_file(tmp_path):
    """_write_shas returns False and writes NO file when the SHA set has no valid SHAs."""
    f = _fetcher(tmp_path)
    result = f._write_shas(440, 441, "777", set())
    assert result is False
    out = tmp_path / "archive" / "v1" / "440_440_441_777.shas"
    assert not out.exists()


def test_fetch_all_skipped_all_archived_no_raise(tmp_path):
    """fetch_all does NOT raise (and returns skipped==N) when every manifest is already archived.

    The total-failure raise condition requires failed > 0 AND fetched == skipped == 0.
    When every depot is skipped (shas file existed), failed==0 so no raise fires.
    """
    _setup(tmp_path, [10])
    v1 = tmp_path / "archive" / "v1"
    v1.mkdir(parents=True)
    (v1 / "10_10_100_1.shas").write_text(f"{_SHA_A}\n")
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_1.manifest"]}, _DONE],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    r = _fetcher(tmp_path).fetch_all()
    assert r.skipped == 1 and r.fetched == 0 and r.failed == 0


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


def test_enumerate_unions_uncovered_cache_apps(tmp_path):
    """Durability: an app with a live `.bin` but no `.shas` (a --recently-purchased
    game outside the selection) is enumerated; one already covered by a `.shas` is
    NOT; selection apps are always included."""
    steam_cfg = tmp_path / "Config"
    steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text(json.dumps([111]))
    cache = tmp_path / "cache"
    (cache / "v1").mkdir(parents=True)
    archive = tmp_path / "archive"
    (archive / "v1").mkdir(parents=True)
    (cache / "v1" / "222_222_2221_gidA.bin").write_bytes(b"x")  # .bin, no .shas -> enumerate
    (cache / "v1" / "333_333_3331_gidB.bin").write_bytes(b"x")  # .bin + .shas -> skip
    (archive / "v1" / "333_333_3331_gidB.shas").write_text("")
    f = _fetcher(
        tmp_path, steam_config_dir=steam_cfg, archive_dir=archive, manifest_cache_dir=cache
    )
    assert f._enumerate_app_ids() == [111, 222]


def test_enumerate_selection_only_when_no_cache_dir(tmp_path):
    steam_cfg = tmp_path / "Config"
    steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text(json.dumps([111]))
    f = _fetcher(tmp_path, steam_config_dir=steam_cfg, manifest_cache_dir=None)
    assert f._enumerate_app_ids() == [111]


def test_enumerate_covers_a_prefilled_app_whose_bin_is_in_the_archive(tmp_path):
    """The durability net must see manifests wherever prefill actually puts them.

    Its stated purpose (#213 follow-up) is to cover apps prefilled OUTSIDE the
    selection — e.g. a `--recently-purchased` game — by taking (has .bin) minus
    (has .shas). But it read `.bin` only from steam_manifest_cache_dir, while the
    agent's archive-sync loop writes newly-prefilled manifests to
    steam_manifest_archive_dir. Confirmed live 2026-08-17: 11 newly-purchased
    games had their `.bin` in the archive and ZERO in the manifest cache, so the
    net that exists precisely for them never saw them.
    """
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text(json.dumps([111]))

    archive = tmp_path / "archive"
    (archive / "v1").mkdir(parents=True)
    # A recently-purchased game: prefilled (its .bin was archived), no .shas yet.
    (archive / "v1" / "2282790_2282790_2282791_777.bin").write_bytes(b"m")

    f = _fetcher(tmp_path, steam_config_dir=cfg, archive_dir=archive)
    assert f._enumerate_app_ids() == [111, 2282790]


def test_enumerate_does_not_refetch_an_app_that_already_has_shas(tmp_path):
    """The net is (has .bin) MINUS (has .shas) — an app already covered must not
    be re-fetched, or every run would trigger a needless DepotDownloader logon
    burst (#228)."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text(json.dumps([]))

    archive = tmp_path / "archive"
    (archive / "v1").mkdir(parents=True)
    (archive / "v1" / "2282790_2282790_2282791_777.bin").write_bytes(b"m")
    (archive / "v1" / "2282790_2282790_2282791_777.shas").write_text("a" * 40 + "\n")

    f = _fetcher(tmp_path, steam_config_dir=cfg, archive_dir=archive)
    assert f._enumerate_app_ids() == []


def test_one_helper_call_carries_every_selected_app(tmp_path):
    _setup(tmp_path, [10, 20, 30])
    _fake_helper(tmp_path, lines=[_DONE])
    _fetcher(tmp_path).fetch_all()
    assert (tmp_path / "calls.log").read_text().count("call") == 1
    assert (tmp_path / "apps-seen.txt").read_text().split() == ["10", "20", "30"]
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0] == "fetch"
    opts = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert opts["--username"] == "kraulerson"
    assert opts["--session-dir"] == str(tmp_path / "dd-config" / "steam-manifest-helper")
    assert opts["--import-from"] == str(tmp_path / "dd-config")


def test_ok_manifests_become_shas_sidecars(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_555.manifest"]}, _DONE],
        manifests={10: {"100_555.manifest": [_SHA_B, _SHA_A]}},
    )
    result = _fetcher(tmp_path).fetch_all()
    assert result == FetchResult(fetched=1, skipped=0, failed=0, apps=1)
    assert (tmp_path / "archive/v1/10_10_100_555.shas").read_text() == f"{_SHA_A}\n{_SHA_B}\n"


def test_an_already_archived_manifest_is_skipped(tmp_path):
    _setup(tmp_path, [10])
    v1 = tmp_path / "archive/v1"
    v1.mkdir(parents=True)
    (v1 / "10_10_100_555.shas").write_text(f"{_SHA_A}\n")
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_555.manifest"]}, _DONE],
        manifests={10: {"100_555.manifest": [_SHA_B]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=0, skipped=1, failed=0, apps=1)
    assert (v1 / "10_10_100_555.shas").read_text() == f"{_SHA_A}\n"


def test_per_app_failures_are_counted_with_their_reason(tmp_path):
    _setup(tmp_path, [10, 20, 30, 40])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "not_owned", "reason": "no access to 1 depot(s)"},
            {"app": 30, "status": "no_depots", "reason": "no Windows 64-bit English depots"},
            {"app": 40, "status": "error", "reason": "depot 41: CDN returned 404"},
            _DONE,
        ],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    with capture_logs() as logs:
        result = _fetcher(tmp_path).fetch_all()
    assert result == FetchResult(fetched=1, skipped=0, failed=3, apps=4)
    reasons = {
        e.get("app_id"): e.get("reason") for e in logs if e["event"] == "manifest_fetch.app_failed"
    }
    assert reasons[40] == "depot 41: CDN returned 404"
    assert reasons[20] == "no access to 1 depot(s)"


def test_a_refused_login_raises_with_steams_reason(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[
            {
                "summary": True,
                "session": "login_refused",
                "reason": "Steam refused the login: RateLimitExceeded",
            }
        ],
        exit_code=2,
    )
    with pytest.raises(HelperSessionError, match="RateLimitExceeded"):
        _fetcher(tmp_path).fetch_all()


def test_a_stopped_session_archives_what_it_fetched_before_raising(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "not_attempted", "reason": "connection lost twice"},
            {
                "summary": True,
                "session": "disconnected",
                "reason": "connection lost twice: NoConnection",
            },
        ],
        exit_code=4,
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    with pytest.raises(HelperSessionError, match="not_attempted=1"):
        _fetcher(tmp_path).fetch_all()
    assert (tmp_path / "archive/v1/10_10_100_1.shas").exists()


def test_a_failed_session_import_raises_naming_the_login_command(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[
            {
                "summary": True,
                "session": "import_failed",
                "reason": "no saved login. Run: SteamManifestHelper login"
                " --username kraulerson --session-dir /x",
            }
        ],
        exit_code=3,
    )
    with pytest.raises(HelperSessionError, match="SteamManifestHelper login"):
        _fetcher(tmp_path).fetch_all()


def test_a_hung_helper_is_killed(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(tmp_path, lines=[_DONE], sleep=10.0)
    with pytest.raises(RuntimeError, match="timed out"):
        _fetcher(tmp_path, timeout_sec=1.0).fetch_all()


def test_a_stray_non_json_stdout_line_is_ignored(tmp_path):
    # Review Focus 4.
    _setup(tmp_path, [10])
    _fake_helper(
        tmp_path,
        lines=[{"app": 10, "status": "ok", "manifests": ["100_1.manifest"]}, _DONE],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
        extra_stdout="SteamKit2 debug: something happened",
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=0, apps=1)


def test_an_unexpected_exit_without_a_summary_raises_with_the_stderr_tail(tmp_path):
    _setup(tmp_path, [10])
    _fake_helper(tmp_path, lines=[], exit_code=1)
    with pytest.raises(RuntimeError, match="helper log line"):
        _fetcher(tmp_path).fetch_all()


def test_ok_with_no_readable_manifest_counts_as_failed(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["100_1.manifest"]},
            {"app": 20, "status": "ok", "manifests": ["200_2.manifest"]},
            _DONE,
        ],
        manifests={10: {"100_1.manifest": [_SHA_A]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=1, apps=2)


def test_a_manifest_name_outside_the_pattern_is_never_read(tmp_path):
    # App 10 lists a traversal name; app 20 is good, so the run doesn't hit the
    # all-failed rule and the assertion is about the name alone.
    _setup(tmp_path, [10, 20])
    (tmp_path / "secret.manifest").write_bytes(_manifest_bytes([_SHA_B]))
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "ok", "manifests": ["../../../secret.manifest"]},
            {"app": 20, "status": "ok", "manifests": ["200_2.manifest"]},
            _DONE,
        ],
        manifests={20: {"200_2.manifest": [_SHA_A]}},
    )
    assert _fetcher(tmp_path).fetch_all() == FetchResult(fetched=1, skipped=0, failed=1, apps=2)
    assert [p.name for p in (tmp_path / "archive" / "v1").glob("*.shas")] == ["20_20_200_2.shas"]


def test_total_failure_still_raises(tmp_path):
    _setup(tmp_path, [10, 20])
    _fake_helper(
        tmp_path,
        lines=[
            {"app": 10, "status": "error", "reason": "x"},
            {"app": 20, "status": "error", "reason": "y"},
            _DONE,
        ],
    )
    with pytest.raises(RuntimeError, match="all 2 apps"):
        _fetcher(tmp_path).fetch_all()


def test_a_helper_session_counts_as_a_session(tmp_path):
    cfg = tmp_path / "dd-config"
    (cfg / "steam-manifest-helper").mkdir(parents=True)
    (cfg / "steam-manifest-helper" / "session.json").write_text("{}")
    _fetcher(tmp_path).login_from_session()
