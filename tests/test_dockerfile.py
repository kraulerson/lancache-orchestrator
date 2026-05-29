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
