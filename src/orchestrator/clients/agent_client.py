"""Control-plane HTTP client for the data-plane agent.

POST-then-poll for async ops (pull, steam_prefill); single call for stat /
downloaded_state / auth_status. Raises AgentError on transport failure, non-2xx,
or a failed agent job — the handlers catch it to fail a job cleanly (never a
crash-loop)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)


class AgentError(RuntimeError):
    """The agent was unreachable, returned an error, or its job failed."""


class AgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_sec: float = 0.5,
        timeout_sec: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        self._poll = poll_interval_sec
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0)

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._headers,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            async with self._new_client() as client:
                resp = await client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise AgentError(f"agent unreachable: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise AgentError(f"agent returned {resp.status_code} for {path}")
        return resp

    async def _post_then_poll(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", path, json=payload)
        job_id = resp.json()["job_id"]
        poll_path = f"{path}/{job_id}"
        while True:
            snap = (await self._request("GET", poll_path)).json()
            state = snap.get("state")
            if state == "done":
                return snap.get("result") or {}
            if state == "failed":
                raise AgentError(f"agent job failed: {snap.get('error')}")
            await asyncio.sleep(self._poll)

    async def pull(
        self, chunks: list[dict[str, str]], *, user_agent: str, concurrency: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chunks": chunks, "user_agent": user_agent}
        if concurrency is not None:
            payload["concurrency"] = concurrency
        return await self._post_then_poll("/v1/pull", payload)

    async def steam_prefill(self, app_ids: list[int], *, force: bool = False) -> dict[str, Any]:
        return await self._post_then_poll("/v1/steam/prefill", {"app_ids": app_ids, "force": force})

    async def stat(self, hashes: list[str]) -> dict[str, int]:
        resp = await self._request("POST", "/v1/stat", json={"hashes": hashes})
        result: dict[str, int] = resp.json()
        return result

    async def downloaded_state(self) -> dict[str, list[int]]:
        resp = await self._request("GET", "/v1/steam/downloaded-state")
        result: dict[str, list[int]] = resp.json()
        return result

    async def auth_status(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v1/steam/auth-status")
        result: dict[str, Any] = resp.json()
        return result
