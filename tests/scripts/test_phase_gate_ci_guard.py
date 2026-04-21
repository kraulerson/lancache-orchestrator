from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check-phase-gate.sh"


def _run_with_ci() -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CI"] = "true"
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_phase_gate_exits_zero_under_ci() -> None:
    result = _run_with_ci()
    assert result.returncode == 0, (
        f"phase-gate exited {result.returncode} with CI=true\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_tool_resolution_block_skipped_under_ci() -> None:
    result = _run_with_ci()
    assert "Tools needed for Phase" not in result.stdout, (
        f"Interactive tool-resolution ran despite CI=true.\nstdout:\n{result.stdout}"
    )
    assert "Install now? [Y/n]" not in result.stdout
