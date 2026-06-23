"""Agent liveness endpoint (auth-exempt).

The agent owns the lancache cache mount, so the liveness probe also reports the
local F7 validator self-test result. Liveness itself is always 200; the
`validator_healthy` field lets the control plane (re-arch ④: an LXC with no
local cache mount) source its validator health from the agent that does own it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from orchestrator.validator.self_test import validator_self_test

router = APIRouter()


@router.get("/v1/health")
async def health(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    healthy = await validator_self_test(settings)
    return {"ok": True, "validator_healthy": healthy}
