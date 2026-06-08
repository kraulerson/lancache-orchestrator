"""F11: library sync."""

from __future__ import annotations

import httpx


def test_library_sync_default_steam(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/platforms/steam/library/sync"
        return httpx.Response(202, json={"job_id": 42})

    r = mock(["library", "sync"], handler)
    assert r.exit_code == 0
    assert "42" in r.output


def test_library_sync_epic(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/platforms/epic/library/sync"
        return httpx.Response(202, json={"job_id": 7})

    r = mock(["library", "sync", "--platform", "epic"], handler)
    assert r.exit_code == 0
    assert "7" in r.output
