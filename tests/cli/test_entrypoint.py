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
