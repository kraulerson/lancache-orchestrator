"""resolve-tools.sh must not hang on a tool whose own commands block.

UAT-14 (#295). The resolver `eval`s every matrix tool's check and version command
with no time bound, so one wedged third-party binary hangs the whole run. In practice
`colima version` against a stuck lima ssh did exactly that, and because
`check-phase-gate.sh` skips tool resolution when `CI` is set, CI never sees it — only
developers do, and the documented `pytest` invocation is what wedges.

The bound here is deliberately generous. The point is not to assert a particular
timeout value, it is to assert that SOME bound exists: a resolver that returns in a
few seconds passes, one that waits on `sleep 120` does not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "resolve-tools.sh"

# Longer than any per-command bound the script could sensibly use, and far longer
# than a healthy run needs.
BLOCKING_SECONDS = 120

# If the script is bounded at all, it finishes well inside this. If it is not, it
# waits the full BLOCKING_SECONDS and this expires first.
RUN_DEADLINE_SECONDS = 45


def _matrix_with_blocking_tool(tmp_path: Path, *, block_check: bool) -> Path:
    """A one-tool matrix whose check or version command blocks."""
    blocking = f"sleep {BLOCKING_SECONDS}"
    tool = {
        "category": "testing",
        "name": "WedgedTool",
        "description": "A tool whose command never returns",
        "required": False,
        "phase": 0,
        "tracks": ["light", "standard", "full"],
        "dev_os": ["darwin", "linux"],
        "platforms": ["all"],
        "languages": ["all"],
        # A blocking check must be bounded too: `docker info` against a dead daemon
        # is the same shape as the version command that actually bit us.
        "check_command": blocking if block_check else "true",
        "version_command": "echo 1.0" if block_check else blocking,
        "min_version": None,
        "latest_check": None,
        "install": {"manual": "n/a"},
    }
    matrix_dir = tmp_path / "matrix"
    matrix_dir.mkdir()
    (matrix_dir / "common.json").write_text(json.dumps({"tools": [tool]}))
    return matrix_dir


def _run(matrix_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "bash",
            str(SCRIPT),
            "--dev-os",
            "linux",
            "--platform",
            "other",
            "--language",
            "other",
            "--track",
            "light",
            "--phase",
            "0",
            "--matrix-dir",
            str(matrix_dir),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=RUN_DEADLINE_SECONDS,
    )


@pytest.mark.parametrize("block_check", [False, True], ids=["version_command", "check_command"])
def test_resolver_does_not_hang_on_a_blocking_tool_command(
    tmp_path: Path, block_check: bool
) -> None:
    matrix_dir = _matrix_with_blocking_tool(tmp_path, block_check=block_check)

    try:
        result = _run(matrix_dir)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"resolve-tools.sh did not return within {RUN_DEADLINE_SECONDS}s while a tool's "
            f"{'check' if block_check else 'version'} command slept for {BLOCKING_SECONDS}s. "
            "Every eval of a third-party command needs a time bound, or one wedged binary "
            "hangs the developer's whole test run (#295)."
        )

    # Returning is the assertion. A non-zero exit would also be a hang-free outcome,
    # but the resolver is expected to treat an unresponsive tool as simply unresolved.
    assert result.returncode == 0, (
        f"resolver exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
