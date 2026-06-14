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


def test_invalid_state_rejected_with_choices(cli_invoke):
    """An invalid --state typo must be rejected client-side listing valid values,
    not silently return an empty table (UAT-11 S11-E-04)."""
    r = cli_invoke(["jobs", "--state", "success"])  # valid is 'succeeded'
    assert r.exit_code == 2
    assert "succeeded" in (r.output + (r.stderr or ""))


def test_invalid_kind_rejected_with_choices(cli_invoke):
    r = cli_invoke(["jobs", "--kind", "sync"])  # valid is 'library_sync'
    assert r.exit_code == 2
    assert "library_sync" in (r.output + (r.stderr or ""))
