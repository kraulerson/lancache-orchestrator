from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check-phase-gate.sh"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def test_no_sigpipe_under_pipefail() -> None:
    result = _run()
    assert "Broken pipe" not in result.stderr, (
        f"SIGPIPE regression in a grep|head pipeline.\nstderr:\n{result.stderr}"
    )


def test_no_local_outside_function() -> None:
    result = _run()
    assert "can only be used in a function" not in result.stderr, (
        f"`local`-outside-function regression.\nstderr:\n{result.stderr}"
    )
