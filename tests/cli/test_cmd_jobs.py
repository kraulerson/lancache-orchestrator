"""F11: jobs list."""

from __future__ import annotations

import httpx

_JOBS = {
    "jobs": [
        {
            "id": 1,
            "kind": "prefill",
            "platform": "steam",
            "state": "running",
            "progress": 0.5,
            "error": None,
        }
    ],
    "meta": {"total": 1},
}


def test_jobs_list_table(mock):
    r = mock(["jobs"], lambda req: httpx.Response(200, json=_JOBS))
    assert r.exit_code == 0
    assert "prefill" in r.output and "RUNNING" in r.output.upper()


def test_jobs_sends_limit_not_per_page(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "limit" in dict(req.url.params)
        assert "per_page" not in dict(req.url.params)
        return httpx.Response(200, json=_JOBS)

    assert mock(["jobs", "--limit", "5"], handler).exit_code == 0
