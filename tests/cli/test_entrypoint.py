"""F11: the console entry resolves and exposes every group."""

from __future__ import annotations

import pathlib
import tomllib

from click.testing import CliRunner

from orchestrator.cli.main import cli, main


def test_main_callable():
    assert callable(main)


def test_pyproject_entry_points_to_main():
    pp = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    assert pp["project"]["scripts"]["orchestrator-cli"] == "orchestrator.cli.main:main"


def test_all_groups_registered():
    r = CliRunner().invoke(cli, ["--help"])
    assert r.exit_code == 0
    for g in ("auth", "library", "status", "game", "jobs", "db", "config"):
        assert g in r.output


def test_python_m_invocation_works():
    """`python -m orchestrator.cli.main` must work (the __main__ guard), not be a
    silent no-op (UAT-11 S11-E-09)."""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "orchestrator.cli.main", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0
    assert "Usage" in r.stdout


def test_get_settings_suppresses_missing_secrets_dir_warning(monkeypatch, recwarn):
    """get_settings() must not emit the noisy '/run/secrets does not exist'
    UserWarning on the operator path (UAT-11 S11-E-07)."""
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    get_settings()
    get_settings.cache_clear()
    assert not any("does not exist" in str(w.message) for w in recwarn.list)
