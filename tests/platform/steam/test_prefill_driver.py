import json
import stat
from pathlib import Path

import pytest

from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver


def _fake_binary(tmp_path, stdout="Done.", code=0):
    p = tmp_path / "FakeSteamPrefill"
    p.write_text(f"#!/bin/sh\ncat <<EOF\n{stdout}\nEOF\nexit {code}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.mark.asyncio
async def test_prefill_apps_writes_selection_and_runs(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path), config_dir=cfg)
    res = await d.prefill_apps([730, 440], force=True)
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [730, 440]
    assert res.ok is True


@pytest.mark.asyncio
async def test_prefill_apps_restores_prior_selection(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text("[111, 222]")
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path), config_dir=cfg)
    await d.prefill_apps([730], force=False)
    # the operator's prior selection is restored after the run
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [111, 222]


@pytest.mark.asyncio
async def test_prefill_apps_runs_from_config_parent_cwd(tmp_path):
    # SteamPrefill resolves its Config/ dir RELATIVE TO the working directory
    # (./Config), not the binary path, so the driver must run it from
    # config_dir.parent — otherwise it finds no account.config and login fails.
    cfg = tmp_path / "Config"
    cfg.mkdir()
    marker = tmp_path / "cwd.txt"
    bin_path = tmp_path / "FakeSteamPrefill"
    bin_path.write_text(f'#!/bin/sh\npwd > "{marker}"\nexit 0\n')
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    d = SteamPrefillDriver(binary=bin_path, config_dir=cfg)
    await d.prefill_apps([730])
    assert Path(marker.read_text().strip()).resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_prefill_apps_nonzero_exit_not_ok(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path, stdout="boom", code=3), config_dir=cfg)
    res = await d.prefill_apps([730])
    assert res.ok is False


def test_downloaded_state_parses(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text('{"730":[111,222],"440":[333]}')
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    assert d.downloaded_state() == {730: [111, 222], 440: [333]}


def test_downloaded_state_missing_returns_empty(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    assert d.downloaded_state() == {}


def test_auth_status_missing_config_needs_reauth(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    st = d.auth_status()
    assert st.ok is False and st.reason == "no_account_config"


def test_auth_status_present_ok(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "account.config").write_bytes(b"\x0a\x05hello")
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    assert d.auth_status().ok is True


def test_list_owned_from_downloaded_depots(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text('{"440":[1],"570":[2,3]}')
    d = SteamPrefillDriver(binary=tmp_path / "bin", config_dir=cfg)
    owned = d.list_owned()
    assert sorted(o.app_id for o in owned) == [440, 570]
    assert all(o.name == "" for o in owned)


def test_list_owned_missing_file_returns_empty(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=tmp_path / "bin", config_dir=cfg)
    assert d.list_owned() == []
