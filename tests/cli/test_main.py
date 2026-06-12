"""F11: root cli group, exit-code mapping."""

from __future__ import annotations

import httpx


def test_help_lists_subcommands(cli_invoke):
    r = cli_invoke(["--help"])
    assert r.exit_code == 0
    for grp in ("auth", "library", "status", "game", "jobs", "db", "config"):
        assert grp in r.output


def test_api_unreachable_exits_2(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    r = mock(["jobs"], handler)
    assert r.exit_code == 2


def test_401_exits_3(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    r = mock(["jobs"], handler)
    assert r.exit_code == 3


def test_server_error_exits_1(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "database unavailable"})

    r = mock(["jobs"], handler)
    assert r.exit_code == 1


def test_malformed_2xx_response_exits_1_cleanly(mock):
    """A 2xx response missing an expected key (e.g. job_id) must surface as a
    clean exit 1, not an unhandled KeyError traceback to the operator."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={})  # no job_id

    r = mock(["library", "sync"], handler)
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)  # backstop caught it, not a raw KeyError
    assert "✗" in r.stderr
