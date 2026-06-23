from __future__ import annotations

from orchestrator.core.net import detect_non_loopback_bind


def test_loopback_returns_none(monkeypatch):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    monkeypatch.setattr("sys.argv", ["x"])
    assert detect_non_loopback_bind("127.0.0.1") is None


def test_non_loopback_setting_returned(monkeypatch):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    monkeypatch.setattr("sys.argv", ["x"])
    assert detect_non_loopback_bind("0.0.0.0") == "0.0.0.0"  # noqa: S104


def test_uvicorn_host_env_detected(monkeypatch):
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.setattr("sys.argv", ["x"])
    assert detect_non_loopback_bind("127.0.0.1") == "0.0.0.0"  # noqa: S104


def test_host_argv_flag_detected(monkeypatch):
    monkeypatch.delenv("UVICORN_HOST", raising=False)
    monkeypatch.setattr("sys.argv", ["x", "--host", "0.0.0.0"])  # noqa: S104
    assert detect_non_loopback_bind("127.0.0.1") == "0.0.0.0"  # noqa: S104
