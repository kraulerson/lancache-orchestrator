"""F11: status summary (aggregates /health + /platforms)."""

from __future__ import annotations

import httpx


def test_status_renders_health_and_platforms(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "scheduler_running": True,
                    "lancache_reachable": True,
                    "cache_volume_mounted": True,
                    "validator_healthy": True,
                    "git_sha": "abc",
                    "version": "0.1.0",
                    "uptime_sec": 5,
                },
            )
        return httpx.Response(
            200,
            json={
                "platforms": [
                    {
                        "name": "steam",
                        "auth_status": "ok",
                        "auth_method": "steam_cm",
                        "auth_expires_at": None,
                        "last_sync_at": "x",
                        "last_error": None,
                    }
                ],
                "meta": {"total": 1},
            },
        )

    r = mock(["status"], handler)
    assert r.exit_code == 0
    assert "SCHEDULER" in r.output.upper()
    assert "VALIDATOR" in r.output.upper()
    assert "STEAM" in r.output.upper()


def test_status_renders_degraded_health_503(mock):
    """`/health` returns 503-with-body when degraded; `status` must render the
    summary (exit 0), not crash with an ApiError/exit 1."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/health":
            return httpx.Response(
                503,
                json={
                    "status": "degraded",
                    "scheduler_running": True,
                    "lancache_reachable": False,
                    "cache_volume_mounted": True,
                    "validator_healthy": False,
                    "git_sha": "abc",
                    "version": "0.1.0",
                    "uptime_sec": 5,
                },
            )
        return httpx.Response(200, json={"platforms": [], "meta": {"total": 0}})

    r = mock(["status"], handler)
    assert r.exit_code == 0, r.output
    assert "VALIDATOR" in r.output.upper()
    assert "LANCACHE" in r.output.upper()
