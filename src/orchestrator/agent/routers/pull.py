"""Agent /v1/pull — platform-agnostic chunk puller (async job + poll)."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from orchestrator.agent.puller import ChunkSpec, pull_chunks

_log = structlog.get_logger(__name__)

router = APIRouter()


class _ChunkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    host: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunks: list[_ChunkIn]
    user_agent: str
    concurrency: int | None = None


def _validate_pull_url(url: str) -> None:
    """Anti-SSRF: relative-path-only. The agent joins this to its fixed
    lancache_base_url; the Host header is the only routing input."""
    if (
        not url
        or not url.startswith("/")
        or url.startswith("//")
        or "://" in url
        or ".." in url
        or "@" in url
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid chunk url")


@router.post("/v1/pull", status_code=status.HTTP_202_ACCEPTED)
async def start_pull(body: PullRequest, request: Request) -> dict[str, str]:
    for c in body.chunks:
        _validate_pull_url(c.url)
    specs = [ChunkSpec(url=c.url, host=c.host) for c in body.chunks]
    store = request.app.state.agent_jobs
    settings = request.app.state.settings
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await pull_chunks(
                specs,
                user_agent=body.user_agent,
                settings=settings,
                concurrency=body.concurrency,
                on_progress=lambda d, t: store.set_progress(job_id, d, t),
            )
            store.set_done(
                job_id,
                {
                    "chunks_total": result.chunks_total,
                    "chunks_ok": result.chunks_ok,
                    "chunks_failed": result.chunks_failed,
                    "failures": result.failures,
                },
            )
        except Exception as e:  # record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    # Hold a strong reference so the fire-and-forget task is not GC'd mid-flight
    # (mirrors db/pool.py's background-task set + discard-on-done pattern).
    bg_tasks = request.app.state.agent_bg_tasks
    task = asyncio.create_task(_run())
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return {"job_id": job_id}


@router.get("/v1/pull/{job_id}")
async def get_pull(job_id: str, request: Request) -> dict[str, Any]:
    snap: dict[str, Any] | None = request.app.state.agent_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap
