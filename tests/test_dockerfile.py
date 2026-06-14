"""Regression tests for the runtime image's dual-venv build.

The shipped image must contain the Steam worker venv at the path
`Settings.steam_worker_python_path` points to — otherwise every
credentialed path (auth, library sync, manifest fetch, F7 validate)
fails at runtime because the worker subprocess binary is missing.

These are static checks over the Dockerfile (no docker build required),
so they run in CI without a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.core.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text()


def test_dockerfile_exists():
    assert _DOCKERFILE.is_file()


def test_builds_steam_worker_venv_from_pinned_requirements():
    """The worker venv must be built from the pinned, hash-checked
    requirements-steam-worker.txt (gevent + steam-next + zstandard)."""
    text = _dockerfile_text()
    assert "requirements-steam-worker.txt" in text
    assert ".venv-steam-worker" in text
    assert "--require-hashes" in text


def test_orchestrator_package_installed_into_worker_venv():
    """The worker subprocess imports orchestrator.platform.steam.worker, so
    the orchestrator package must be installed into the worker venv too."""
    text = _dockerfile_text()
    # The worker venv pip must install the local package (--no-deps).
    assert ".venv-steam-worker/bin/pip install" in text
    assert "--no-deps ." in text


def test_worker_venv_copied_to_settings_path():
    """The runtime stage must place the worker venv at exactly the directory
    Settings.steam_worker_python_path lives in — otherwise the worker can't
    be launched. Ties the Dockerfile to the setting so the two can't drift."""
    text = _dockerfile_text()
    worker_python = Settings(orchestrator_token="a" * 32).steam_worker_python_path
    # e.g. /opt/orchestrator/venv-steam-worker/bin/python -> venv dir
    venv_dir = worker_python.parent.parent  # .../venv-steam-worker
    assert str(venv_dir) in text, (
        f"Dockerfile must COPY the worker venv to {venv_dir} "
        f"(Settings.steam_worker_python_path={worker_python})"
    )
    assert "/opt/orchestrator/venv-steam-worker" in text


def test_entrypoint_defaults_to_loopback_not_hardcoded_0_0_0_0():
    """UAT-11 F-INT-3: the image must not hardcode --host 0.0.0.0 (which exposes
    the trigger endpoints to the LAN and fires the non-loopback warning every
    boot). It binds ORCH_API_HOST, defaulting to loopback; operators opt into
    0.0.0.0 explicitly."""
    text = _dockerfile_text()
    entry = next(line for line in text.splitlines() if "uvicorn" in line and "ENTRYPOINT" in line)
    assert '"--host", "0.0.0.0"' not in entry
    assert "ORCH_API_HOST" in entry
    assert "127.0.0.1" in entry  # the secure default


def test_entrypoint_uses_python_m_uvicorn_not_console_script():
    """The venv is copied build->runtime, so the `uvicorn` console-script shebang
    is broken; the entrypoint must invoke `python -m uvicorn` (shebang-independent)
    so the container actually starts (caught live, UAT-11)."""
    text = _dockerfile_text()
    entry = next(line for line in text.splitlines() if "uvicorn" in line and "ENTRYPOINT" in line)
    assert "python -m uvicorn" in entry


def test_venv_console_script_shebangs_rewritten_to_runtime_path():
    """The copied venv's console scripts (incl. the bundled `orchestrator-cli`)
    hardcode the build-stage `#!/build/.venv/bin/python` shebang, which doesn't
    exist in the runtime image. The Dockerfile must rewrite them to /app/.venv so
    `orchestrator-cli` works inside the container (caught live, UAT-11)."""
    text = _dockerfile_text()
    assert "/build/.venv/bin/python" in text  # the broken shebang it rewrites
    # rewritten to the runtime path via sed over the matching scripts
    assert "sed" in text and "/app/.venv/bin/python" in text
