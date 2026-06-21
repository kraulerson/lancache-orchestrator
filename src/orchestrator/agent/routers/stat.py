"""Agent /v1/stat — cache disk-stat over control-supplied cache-key hashes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from orchestrator.validator.cache_key import cache_path
from orchestrator.validator.disk_stat import validate_chunks

router = APIRouter()

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class StatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hashes: list[str]


@router.post("/v1/stat")
async def stat(body: StatRequest, request: Request) -> dict[str, int]:
    for h in body.hashes:
        if not _HEX32.match(h):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cache-key hash"
            )
    settings = request.app.state.settings
    cache_root = Path(settings.lancache_nginx_cache_path)
    levels = settings.cache_levels
    paths = [cache_path(cache_root, h, levels) for h in body.hashes]
    cached, missing = await validate_chunks(paths)
    return {"cached": cached, "missing": missing}
