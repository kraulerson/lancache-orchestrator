"""Control-plane HTTP client for the data-plane agent.

POST-then-poll for async ops (pull, steam_prefill); single call for stat /
downloaded_state / auth_status. Raises AgentError on transport failure, non-2xx,
or a failed agent job — the handlers catch it to fail a job cleanly (never a
crash-loop)."""

from __future__ import annotations

import asyncio
import time
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
        poll_interval_sec: float = 3.0,
        timeout_sec: float = 30.0,
        poll_timeout_sec: float = 7200.0,
        connect_retries: int = 2,
        connect_retry_backoff_sec: float = 0.5,
    ) -> None:
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport
        # UAT-12: poll at 3s (was 0.5s) — a multi-hour job needs far fewer
        # connects, cutting the chance of landing on a connect blip ~6x.
        self._poll = poll_interval_sec
        # UAT-12: connect timeout 15s (was 10s) absorbs brief accept-lag in one
        # attempt; the retry below is the backstop for a harder blip.
        self._timeout = httpx.Timeout(timeout_sec, connect=15.0)
        # UAT-12: bounded retry on connect-phase failures (see _request). The
        # agent's single uvicorn listener can be briefly CPU-starved by a heavy
        # SteamPrefill --force on the steal-bound VM, lagging accept() past the
        # connect timeout; one such blip must not kill a multi-hour prefill.
        self._connect_retries = connect_retries
        self._connect_backoff = connect_retry_backoff_sec
        # Overall ceiling for a post-then-poll op (prefill/pull). Bounds the poll
        # loop so a job stuck 'running' can't poll forever (MEM-2). Default safely
        # above any real prefill; the orchestrator job's own timeout is the
        # primary guard, this is the client-side backstop.
        self._poll_timeout = poll_timeout_sec
        # Re-arch ④ §3b-1: hold ONE persistent AsyncClient, built lazily and
        # reused across calls (and across the many GET polls in
        # _post_then_poll). On loopback rebuilding per request was harmless;
        # once the control plane moves to an LXC every call is a cross-host LAN
        # round-trip, so connection reuse (keep-alive) matters. Closed on the
        # API lifespan shutdown via aclose().
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "headers": self._headers,
                "timeout": self._timeout,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        """Close the persistent client. Idempotent — safe if never built."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = await self._get_client().request(method, path, **kw)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                # UAT-12: a connect-phase failure means no connection was
                # established, so the request never reached the agent — safe to
                # retry for ANY method (no duplicate side effects). Bounded by
                # _connect_retries; a transient blip mid-prefill must not fail the
                # job, but a genuinely-down agent must still surface promptly.
                if attempt >= self._connect_retries:
                    raise AgentError(f"agent unreachable: {type(e).__name__}") from e
                attempt += 1
                _log.warning(
                    "agent.connect_retry", path=path, attempt=attempt, error=type(e).__name__
                )
                await asyncio.sleep(self._connect_backoff * attempt)
            except httpx.HTTPError as e:
                # Non-connect transport error (e.g. ReadTimeout after the request
                # was sent): not safe to blind-retry a POST, so surface it.
                raise AgentError(f"agent unreachable: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise AgentError(f"agent returned {resp.status_code} for {path}")
        return resp

    async def _post_then_poll(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        poll_timeout: float | None = None,
    ) -> dict[str, Any]:
        resp = await self._request("POST", path, json=payload)
        # COR-7: tolerate a malformed 202 body (no job_id) with a clean error.
        job_id = resp.json().get("job_id")
        if not job_id:
            raise AgentError(f"agent POST {path} returned no job_id")
        poll_path = f"{path}/{job_id}"
        effective_timeout = poll_timeout if poll_timeout is not None else self._poll_timeout
        deadline = time.monotonic() + effective_timeout
        while True:
            snap = (await self._request("GET", poll_path)).json()
            state = snap.get("state")
            if state == "done":
                return snap.get("result") or {}
            if state == "failed":
                raise AgentError(f"agent job failed: {snap.get('error')}")
            # MEM-2: bound the poll loop — a job stuck 'running' must not spin
            # forever. Checked AFTER terminal states so a job that finishes right
            # at the deadline still returns its result.
            if time.monotonic() >= deadline:
                raise AgentError(f"agent job {job_id} did not finish within {effective_timeout}s")
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

    async def fetch_manifests(self) -> dict[str, Any]:
        """Trigger a manifest-only fetch run on the agent (it self-enumerates the
        cached app set; no app-id list crosses the wire). POST + poll to done.
        Uses a 6-hour poll ceiling (fetch_manifests visits every cached app via
        DepotDownloader; a full library can take hours — the default 2h ceiling
        would time out mid-run on a large library)."""
        return await self._post_then_poll("/v1/steam/fetch-manifests", {}, poll_timeout=21600.0)

    async def stat(self, hashes: list[str]) -> dict[str, int]:
        resp = await self._request("POST", "/v1/stat", json={"hashes": hashes})
        result: dict[str, int] = resp.json()
        return result

    async def prune_steam_selection(
        self, exclude_app_ids: list[int], restore_app_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """Reconcile SteamPrefill's selectedAppsToPrefill.json on the agent (Piece
        1): remove exclude_app_ids (classifier non-games), keep/re-add
        restore_app_ids (operator 'allow'). Returns {removed, restored, remaining}."""
        resp = await self._request(
            "POST",
            "/v1/steam/prune-selection",
            json={
                "exclude_app_ids": exclude_app_ids,
                "restore_app_ids": restore_app_ids or [],
            },
        )
        result: dict[str, Any] = resp.json()
        return result

    async def steam_validate(self, app_id: int) -> dict[str, Any]:
        # A big game (tens of thousands of chunks) stat's many cache files over
        # NFS and can take well over the default 30s timeout — use a generous
        # per-call timeout so validate doesn't AgentError on large apps.
        resp = await self._request(
            "POST",
            "/v1/steam/validate",
            json={"app_id": app_id},
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        result: dict[str, Any] = resp.json()
        return result

    async def epic_validate(
        self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/v1/epic/validate",
            json={
                "app_id": app_id,
                "version": version,
                "cdn_base": cdn_base,
                "raw_manifest_b64": raw_manifest_b64,
            },
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        result: dict[str, Any] = resp.json()
        return result

    async def prefilled_apps(self) -> list[int]:
        resp = await self._request("GET", "/v1/steam/prefilled-apps")
        result: list[int] = resp.json()["app_ids"]
        return result

    async def manual_downloads(self, launcher: str) -> dict[str, Any]:
        """List the manually-downloaded game folders under `<cache>/<launcher>/`
        on the agent host (#222). Returns {launcher, present, entries}. The caller
        validates `launcher` (alnum/_/-) before it reaches the path."""
        resp = await self._request("GET", f"/v1/manual-downloads/{launcher}")
        result: dict[str, Any] = resp.json()
        return result

    async def downloaded_state(self) -> dict[str, list[int]]:
        resp = await self._request("GET", "/v1/steam/downloaded-state")
        result: dict[str, list[int]] = resp.json()
        return result

    async def auth_status(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v1/steam/auth-status")
        result: dict[str, Any] = resp.json()
        return result

    async def agent_health(self) -> dict[str, Any]:
        # re-arch ④: the agent owns the cache mount, so its liveness probe also
        # reports its local validator self-test. The control plane (an LXC with
        # no cache mount) reads validator_healthy from here to gate its own
        # app.state.validator_healthy.
        resp = await self._request("GET", "/v1/health")
        result: dict[str, Any] = resp.json()
        return result
