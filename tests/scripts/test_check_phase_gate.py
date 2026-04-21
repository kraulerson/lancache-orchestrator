from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check-phase-gate.sh"


@pytest.fixture
def ci_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CI"] = "true"
    return env


def test_check_phase_gate_exits_zero(ci_env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=ci_env,
    )
    assert result.returncode == 0, (
        f"check-phase-gate.sh exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_no_sigpipe_from_head_under_pipefail(ci_env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=ci_env,
    )
    assert "Broken pipe" not in result.stderr, (
        f"SIGPIPE regression: grep/head pipeline failed under set -o pipefail.\n"
        f"stderr:\n{result.stderr}"
    )


def test_no_local_outside_function_error(ci_env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=ci_env,
    )
    assert "can only be used in a function" not in result.stderr, (
        f"`local` outside function regression detected.\nstderr:\n{result.stderr}"
    )


def test_ci_guard_skips_interactive_tool_resolution(ci_env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=ci_env,
    )
    assert "Install now? [Y/n]" not in result.stdout
    assert "Tools needed for Phase" not in result.stdout, (
        f"Interactive tool-resolution block ran despite CI=true.\nstdout:\n{result.stdout}"
    )
