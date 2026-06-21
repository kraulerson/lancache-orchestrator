"""Agent liveness endpoint (auth-exempt)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
