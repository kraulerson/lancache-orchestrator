"""Tests for orchestrator.core.logging — structured logging framework (ID3).

Each test maps to a specific GitHub issue from the UAT-1 audit (2026-04-22).
Tests target the post-fix API, so they fail until BL2 fixes land. TDD per
CLAUDE.md Phase 2 Construction Rule.

Issue map:
  #9  SEV-2 CID context bleed               → test_request_context_*, test_cid_*
  #10 SEV-2 reserved-key clobber            → test_user_kwarg_*
  #14 SEV-3 no PII redaction                → test_redact_*, test_*_redacted
  #15 SEV-3 log_level silent fallback       → test_log_level_*, test_*_level_*
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
import structlog

from orchestrator.core import logging as log_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Each test starts with a clean structlog pipeline + empty contextvars."""
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _json_lines(captured_out: str) -> list[dict]:
    return [json.loads(line) for line in captured_out.strip().split("\n") if line.strip()]


def _last_json_line(captured_out: str) -> dict:
    lines = _json_lines(captured_out)
    assert lines, "no log output captured"
    return lines[-1]


# ---------------------------------------------------------------------------
# Baseline — correlation-ID generation
# ---------------------------------------------------------------------------


def test_new_correlation_id_is_uuid4_hex() -> None:
    cid = log_mod.new_correlation_id()
    assert re.match(r"^[0-9a-f]{32}$", cid)


def test_new_correlation_id_unique() -> None:
    assert log_mod.new_correlation_id() != log_mod.new_correlation_id()


# ---------------------------------------------------------------------------
# Issue #9 SEV-2 — request_context manager (scoped CID binding)
# ---------------------------------------------------------------------------


def test_request_context_binds_and_clears(capsys: pytest.CaptureFixture[str]) -> None:
    """Enter binds a CID; exit clears it. Log lines inside the block carry it,
    lines after do not."""
    log_mod.configure_logging()
    log = structlog.get_logger()

    with log_mod.request_context() as cid:
        assert re.match(r"^[0-9a-f]{32}$", cid)
        log.info("inside")
    log.info("outside")

    lines = _json_lines(capsys.readouterr().out)
    inside = next(r for r in lines if r.get("event") == "inside")
    outside = next(r for r in lines if r.get("event") == "outside")
    assert inside["correlation_id"] == cid
    assert "correlation_id" not in outside


def test_request_context_clears_on_exception(capsys: pytest.CaptureFixture[str]) -> None:
    """Exception inside the block must not leave a CID bound in contextvars."""
    log_mod.configure_logging()
    log = structlog.get_logger()

    with pytest.raises(RuntimeError), log_mod.request_context():
        log.info("inside")
        raise RuntimeError("boom")
    log.info("after_exception")

    lines = _json_lines(capsys.readouterr().out)
    after = next(r for r in lines if r.get("event") == "after_exception")
    assert "correlation_id" not in after


def test_request_context_accepts_explicit_cid(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    log = structlog.get_logger()

    with log_mod.request_context("fixed-abc") as cid:
        assert cid == "fixed-abc"
        log.info("inside")

    record = _last_json_line(capsys.readouterr().out)
    assert record["correlation_id"] == "fixed-abc"


async def test_cid_isolation_across_asyncio_tasks(capsys: pytest.CaptureFixture[str]) -> None:
    """Two concurrent asyncio tasks must each see only their own CID. This is
    Python contextvars-native behavior; we assert our code doesn't break it."""
    log_mod.configure_logging()
    log = structlog.get_logger()

    async def worker(name: str) -> None:
        with log_mod.request_context(f"cid-{name}"):
            await asyncio.sleep(0.01)
            log.info("worker_done", worker=name)

    await asyncio.gather(worker("a"), worker("b"))

    lines = _json_lines(capsys.readouterr().out)
    by_worker = {r["worker"]: r["correlation_id"] for r in lines if r.get("event") == "worker_done"}
    assert by_worker["a"] == "cid-a"
    assert by_worker["b"] == "cid-b"


# ---------------------------------------------------------------------------
# Issue #10 SEV-2 — reserved-key clobber
# ---------------------------------------------------------------------------


def test_user_kwarg_correlation_id_does_not_clobber(capsys: pytest.CaptureFixture[str]) -> None:
    """log.info(..., correlation_id='spoofed') must NOT overwrite the real CID.
    Spoofed value is preserved as user_correlation_id."""
    log_mod.configure_logging()
    log = structlog.get_logger()

    with log_mod.request_context("real"):
        log.info("evt", correlation_id="spoofed")

    record = _last_json_line(capsys.readouterr().out)
    assert record["correlation_id"] == "real"
    assert record["user_correlation_id"] == "spoofed"


def test_user_kwarg_event_blocked_at_python_level() -> None:
    """log.info('real_event', event='spoofed') is a TypeError at the call site —
    structlog's method signature (`meth(event, **kwargs)`) makes `event` an
    exclusive positional-or-kwarg slot. Documenting this so callers know the
    'event' key is unclobberable by design."""
    log_mod.configure_logging()
    log = structlog.get_logger()

    with pytest.raises(TypeError, match="event"):
        log.info("real_event", event="spoofed")


def test_non_reserved_user_kwarg_passes_through(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("evt", my_field="value", count=42)

    record = _last_json_line(capsys.readouterr().out)
    assert record["my_field"] == "value"
    assert record["count"] == 42


def test_reserved_keys_constant_exported() -> None:
    """The module exports a RESERVED_KEYS constant for callers that want to
    validate their own kwargs without importing from private members."""
    assert hasattr(log_mod, "RESERVED_KEYS")
    assert "correlation_id" in log_mod.RESERVED_KEYS


# ---------------------------------------------------------------------------
# Issue #14 SEV-3 — PII / secret redaction
# ---------------------------------------------------------------------------


def test_password_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("auth", password="hunter2")

    out = capsys.readouterr().out
    assert "hunter2" not in out
    record = _last_json_line(out)
    assert record["password"] == "<redacted>"


def test_access_token_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("auth", access_token="abc123")
    record = _last_json_line(capsys.readouterr().out)
    assert record["access_token"] == "<redacted>"


def test_bearer_token_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("auth", bearer="xyz")
    record = _last_json_line(capsys.readouterr().out)
    assert record["bearer"] == "<redacted>"


def test_authorization_header_in_nested_dict_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info(
        "req",
        headers={"Authorization": "Bearer xyz", "Content-Type": "application/json"},
    )

    out = capsys.readouterr().out
    assert "xyz" not in out
    record = _last_json_line(out)
    assert record["headers"]["Authorization"] == "<redacted>"
    # Non-sensitive siblings untouched
    assert record["headers"]["Content-Type"] == "application/json"


def test_api_key_name_variants_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """Cover api_key, api-key, APIKey, ApiKey — case-insensitive + separator-insensitive."""
    log_mod.configure_logging()
    log = structlog.get_logger()
    log.info("e1", api_key="a")
    log.info("e2", **{"api-key": "b"})
    log.info("e3", APIKey="c")

    lines = _json_lines(capsys.readouterr().out)
    by_event = {r["event"]: r for r in lines}
    assert by_event["e1"]["api_key"] == "<redacted>"
    assert by_event["e2"]["api-key"] == "<redacted>"
    assert by_event["e3"]["APIKey"] == "<redacted>"


def test_secret_key_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("cfg", secret="s3cr3t", client_secret="cs3cr3t")
    out = capsys.readouterr().out
    assert "s3cr3t" not in out
    assert "cs3cr3t" not in out


def test_session_and_cookie_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("req", session_id="sid", cookie="c=v")
    out = capsys.readouterr().out
    assert "sid" not in out
    assert "c=v" not in out


def test_list_of_dicts_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info(
        "multi_creds",
        items=[
            {"user": "alice", "password": "p1"},
            {"user": "bob", "password": "p2"},
        ],
    )

    out = capsys.readouterr().out
    assert "p1" not in out and "p2" not in out
    record = _last_json_line(out)
    assert record["items"][0]["password"] == "<redacted>"
    assert record["items"][1]["password"] == "<redacted>"
    assert record["items"][0]["user"] == "alice"


def test_non_sensitive_keys_untouched(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging()
    structlog.get_logger().info("evt", user="alice", count=5, when="now")

    record = _last_json_line(capsys.readouterr().out)
    assert record["user"] == "alice"
    assert record["count"] == 5
    assert record["when"] == "now"


# ---------------------------------------------------------------------------
# Issue #15 SEV-3 — log_level validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_valid_log_level_accepted(level: str) -> None:
    log_mod.configure_logging(log_level=level)


@pytest.mark.parametrize("level", ["debug", "Info", "wArNiNg"])
def test_log_level_is_case_insensitive(level: str) -> None:
    log_mod.configure_logging(log_level=level)


@pytest.mark.parametrize(
    "level",
    ["WARN", "VERBOSE", "TRACE", "", "info-ish", "critical_", "FATAL"],
)
def test_invalid_log_level_raises(level: str) -> None:
    with pytest.raises(ValueError):
        log_mod.configure_logging(log_level=level)


def test_default_log_level_is_info() -> None:
    log_mod.configure_logging()
