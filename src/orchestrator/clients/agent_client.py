"""Control-plane HTTP client for the data-plane agent.

POST-then-poll for async ops (pull, steam_prefill); single call for stat /
downloaded_state / auth_status. Raises AgentError on transport failure, non-2xx,
or a failed agent job — the handlers catch it to fail a job cleanly (never a
crash-loop)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx
import structlog

_log = structlog.get_logger(__name__)

# Poll ceiling for /v1/steam/prefill. Must stay ABOVE the agent's
# `steam_prefill_timeout_sec` (default 36000s = 10h, matching the host cron's
# RUN_MAX) so the AGENT's timeout is the one that fires. If the client gives up
# first, the run is abandoned while SteamPrefill keeps going and the agent's
# single-flight gate blocks every other prefill until it finally exits.
_STEAM_PREFILL_POLL_TIMEOUT_SEC = 37800.0  # 10.5h — 30 min of headroom over 10h

# Validate budget. Every call used to get a flat 300s regardless of size, so game
# 15035 (359,671 chunks, ~433s measured, up to 876s on a bad NFS day) was cut off on
# all 27 sweeps across 52 days while still reporting up_to_date (#297).
#
# Cutting it off does not just discard finished work — the agent's handler is torn
# down on client disconnect, so the run is ABORTED and the next sweep starts over.
#
# A bigger flat number would only move the cliff; MechWarrior 5 Editor already sits at
# 271s against the old 300s. So the budget scales with the chunk count the caller
# already holds in manifests.chunk_count.
VALIDATE_TIMEOUT_BASE_SEC = 300.0
# THE DISK THROUGHPUT THE BUDGET ASSUMES, stated outright — the previous form
# (`PER_CHUNK_SEC = 0.00217`) hid an assumption of ~461 chunks/sec, and nothing
# flagged it when the hardware stopped being able to deliver that.
#
# Measured on 2026-09-01 from validation_history durations:
#   before the OMV rebuild (bcache in front of the RAID0):  1471-1539 chunks/sec
#   after  the OMV rebuild (raw RAID0, NVMe cache detached):  48-54 chunks/sec
#
# 40 is deliberately BELOW the measured 48, so a slow day does not reintroduce
# the cliff. Raise it if a cache layer is restored — but measure first, and
# remember that being wrong in the optimistic direction is what caused the
# incident: 379 of 1811 games (21%) could not validate at all, and a timed-out
# validate writes no validation_history row, so those games never self-correct.
VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC = 40.0
# Must exceed the largest real game's requirement or the cliff simply moves: at
# 40/sec ARK: Survival Evolved (369,317 chunks) needs ~9,533s including the base.
# 4 hours leaves room for library growth. The ceiling exists so a genuinely
# wedged agent still surfaces as a failure rather than hanging forever.
#
# UPPER BOUND, and it is not negotiable: this must stay <= Settings.
# job_max_runtime_sec (21600s). The worker wraps every handler in
# asyncio.wait_for, so a ceiling above the job budget is a fiction — the job is
# cancelled first, and CancelledError is a BaseException that bypasses the
# sweep's per-game `except Exception`, aborting validates with NO
# validation_history row. That is the very "cannot self-correct" mechanism this
# constant was raised to remove. Pinned by
# tests/clients/test_validate_timeout_review_remediation.py.
VALIDATE_TIMEOUT_CEILING_SEC = 14400.0


def validate_timeout_for(
    chunk_count: int | None,
    *,
    chunks_per_sec: float = VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC,
) -> httpx.Timeout:
    """Read budget for a validate call, scaled to the work it implies.

    An unknown or nonsensical count keeps the base budget — no information means no
    change, so steam (which self-enumerates agent-side) behaves exactly as before.

    ``chunks_per_sec`` is the assumed disk throughput. It is a parameter rather than
    a baked-in constant because it is a property of the HARDWARE, not of the code:
    the 2026-09-01 incident was a storage change silently invalidating a number
    nobody could see. Overriding it must not require a code edit.

    Only the READ budget scales. Reaching the agent is fast or it is broken, so the
    connect timeout stays short.
    """
    read = VALIDATE_TIMEOUT_BASE_SEC
    if chunk_count is not None and chunk_count > 0 and chunks_per_sec > 0:
        read = min(
            VALIDATE_TIMEOUT_CEILING_SEC,
            VALIDATE_TIMEOUT_BASE_SEC + (chunk_count / chunks_per_sec),
        )
    return httpx.Timeout(read, connect=10.0)


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
        validate_chunks_per_sec: float = VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC,
    ) -> None:
        self._base_url = base_url
        # Assumed disk throughput for validate budgets, injected from Settings so a
        # hardware change is a config edit rather than a code edit — the 2026-09-01
        # incident was exactly a storage change invalidating a buried constant.
        self._validate_chunks_per_sec = validate_chunks_per_sec
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
        """Prefill specific apps on the agent. POST + poll to done.

        Uses a poll ceiling ABOVE the agent's own `steam_prefill_timeout_sec`
        (default 36000s = 10h, matching the host cron's RUN_MAX). With the
        default 2h ceiling the control plane always abandoned the job ~8 hours
        early: it marked the game failed while SteamPrefill kept running, and the
        agent's single-flight gate then refused every other prefill for the
        remainder of those 8 hours. Letting the agent-side timeout be the one
        that fires keeps a single authority over how long a run may take.
        Same per-endpoint remedy as `fetch_manifests` below.
        """
        return await self._post_then_poll(
            "/v1/steam/prefill",
            {"app_ids": app_ids, "force": force},
            poll_timeout=_STEAM_PREFILL_POLL_TIMEOUT_SEC,
        )

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

    async def steam_validate(
        self, app_id: int, *, chunk_count: int | None = None
    ) -> dict[str, Any]:
        # A big game stats many cache files over NFS. The budget scales with the
        # manifest's chunk count when the caller knows it; steam self-enumerates
        # agent-side, so it usually does not and keeps the base budget.
        resp = await self._request(
            "POST",
            "/v1/steam/validate",
            json={"app_id": app_id},
            timeout=validate_timeout_for(chunk_count, chunks_per_sec=self._validate_chunks_per_sec),
        )
        result: dict[str, Any] = resp.json()
        return result

    async def epic_validate(
        self,
        *,
        app_id: int,
        version: str,
        cdn_base: str,
        raw_manifest_b64: str,
        chunk_count: int | None = None,
    ) -> dict[str, Any]:
        # Epic callers read the manifest row first, so they know chunk_count and the
        # budget scales with it — this is the path game 15035 was dying on (#297).
        resp = await self._request(
            "POST",
            "/v1/epic/validate",
            json={
                "app_id": app_id,
                "version": version,
                "cdn_base": cdn_base,
                "raw_manifest_b64": raw_manifest_b64,
            },
            timeout=validate_timeout_for(chunk_count, chunks_per_sec=self._validate_chunks_per_sec),
        )
        result: dict[str, Any] = resp.json()
        return result

    async def steam_purge(self, app_id: int) -> dict[str, Any]:
        # Like steam_validate, a large game unlinks many cache files over NFS and
        # can exceed the default 30s timeout — use a generous per-call timeout so
        # purge doesn't AgentError on big apps.
        resp = await self._request(
            "POST",
            "/v1/steam/purge",
            json={"app_id": app_id},
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        result: dict[str, Any] = resp.json()
        return result

    async def epic_purge(
        self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/v1/epic/purge",
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

    async def manual_downloads(self, launcher: str, include_files: bool = False) -> dict[str, Any]:
        """List the manually-downloaded game entries under `<cache>/<launcher>/`
        on the agent host (#222). Returns {launcher, present, entries}. The launcher
        may contain spaces/dots (e.g. "Amazon Games") so it is URL-encoded here; the
        caller still validates it against the allowlist. With include_files, loose
        files (Humble/Itch installers) are listed too, not just directories."""
        path = f"/v1/manual-downloads/{quote(launcher, safe='')}"
        if include_files:
            path += "?include_files=true"
        resp = await self._request("GET", path)
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
