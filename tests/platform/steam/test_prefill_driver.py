import asyncio
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


def _hanging_binary(tmp_path):
    """Mimics the 2026-08-12 incident: prints success, then never exits."""
    p = tmp_path / "HangingSteamPrefill"
    p.write_text("#!/bin/sh\necho 'Prefill complete!'\nsleep 300\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _group_spawning_binary(tmp_path, marker):
    """Spawns a background child that outlives its parent unless the whole
    process GROUP is killed. The child writes `marker` after a delay."""
    p = tmp_path / "GroupSteamPrefill"
    p.write_text(f"#!/bin/sh\n(sleep 5; echo pwned > {marker}) &\nsleep 300\n")
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
async def test_prefill_apps_pins_subprocess_home(tmp_path):
    # SteamPrefill writes its manifest cache to $HOME/.cache/SteamPrefill; the
    # driver must pin the subprocess HOME so manifests land where the capture
    # reads, regardless of the container's inherited HOME (UAT-13 F2 / #211).
    cfg = tmp_path / "Config"
    cfg.mkdir()
    home = tmp_path / "pinned_home"
    marker = tmp_path / "home.txt"
    bin_path = tmp_path / "FakeSteamPrefill"
    bin_path.write_text(f'#!/bin/sh\nprintf %s "$HOME" > "{marker}"\nexit 0\n')
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    d = SteamPrefillDriver(binary=bin_path, config_dir=cfg, home=home)
    await d.prefill_apps([730])
    assert marker.read_text().strip() == str(home)


@pytest.mark.asyncio
async def test_prefill_apps_home_none_inherits_env(tmp_path, monkeypatch):
    # Default home=None preserves prior behavior: the subprocess inherits the
    # process environment's HOME (no env override).
    cfg = tmp_path / "Config"
    cfg.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "inherited"))
    marker = tmp_path / "home.txt"
    bin_path = tmp_path / "FakeSteamPrefill"
    bin_path.write_text(f'#!/bin/sh\nprintf %s "$HOME" > "{marker}"\nexit 0\n')
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    d = SteamPrefillDriver(binary=bin_path, config_dir=cfg)
    await d.prefill_apps([730])
    assert marker.read_text().strip() == str(tmp_path / "inherited")


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


@pytest.mark.asyncio
async def test_prefill_apps_times_out_instead_of_hanging(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(binary=_hanging_binary(tmp_path), config_dir=cfg, timeout_sec=1.0)
    res = await d.prefill_apps([730])
    assert res.ok is False
    assert "timeout" in res.raw.lower()


@pytest.mark.asyncio
async def test_prefill_apps_restores_selection_even_on_timeout(tmp_path):
    """The operator's selection must survive the timeout path, or a timed-out
    run leaves the orchestrator's temporary app list as the cron's input."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text("[111, 222]")
    d = SteamPrefillDriver(binary=_hanging_binary(tmp_path), config_dir=cfg, timeout_sec=1.0)
    await d.prefill_apps([730])
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [111, 222]


@pytest.mark.asyncio
async def test_timeout_kills_the_whole_process_group(tmp_path):
    """Killing only the direct child leaves grandchildren alive — exactly the
    cron's known weakness, where `timeout` kills the docker exec client while
    the in-container SteamPrefill runs on."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    marker = tmp_path / "child-survived.txt"
    d = SteamPrefillDriver(
        binary=_group_spawning_binary(tmp_path, marker), config_dir=cfg, timeout_sec=1.0
    )
    await d.prefill_apps([730])
    await asyncio.sleep(7)  # past the child's 5s delay
    assert not marker.exists(), "background child survived the group kill"
