"""Tests for the control-plane AgentClient."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.clients.agent_client import AgentClient, AgentError

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


def _client(handler) -> AgentClient:
    transport = httpx.MockTransport(handler)
    return AgentClient(
        base_url="http://agent:8780",
        token=TOKEN,
        transport=transport,
        poll_interval_sec=0.0,
        connect_retry_backoff_sec=0.0,
    )


async def test_pull_posts_then_polls_to_done():
    state = {"polls": 0}

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "POST" and request.url.path == "/v1/pull":
            return httpx.Response(202, json={"job_id": "j1"})
        if request.url.path == "/v1/pull/j1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"state": "running", "done": 1, "total": 2})
            return httpx.Response(
                200,
                json={"state": "done", "result": {"chunks_ok": 2, "chunks_failed": 0}},
            )
        raise AssertionError(request.url.path)

    client = _client(handler)
    result = await client.pull([{"url": "/depot/1/chunk/x", "host": "h"}], user_agent="UA/1.0")
    assert result["chunks_ok"] == 2


async def test_steam_prefill_polls_to_done():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "s1"})
        return httpx.Response(200, json={"state": "done", "result": {"ok": True, "raw": "x"}})

    client = _client(handler)
    result = await client.steam_prefill([440], force=False)
    assert result["ok"] is True


async def test_stat_single_call():
    def handler(request):
        assert request.url.path == "/v1/stat"
        return httpx.Response(200, json={"cached": 3, "missing": 1})

    client = _client(handler)
    assert await client.stat(["a" * 32]) == {"cached": 3, "missing": 1}


async def test_prefilled_apps_single_call():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/steam/prefilled-apps"
        return httpx.Response(200, json={"app_ids": [440, 570, 730]})

    client = _client(handler)
    assert await client.prefilled_apps() == [440, 570, 730]


async def test_auth_status_single_call():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "reason": ""})

    client = _client(handler)
    assert (await client.auth_status())["ok"] is True


async def test_unreachable_raises_agent_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_401_raises_agent_error():
    def handler(request):
        return httpx.Response(401)

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_failed_job_raises_agent_error():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"state": "failed", "error": "boom"})

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.pull([{"url": "/x", "host": "h"}], user_agent="UA/1.0")


async def test_post_then_poll_missing_job_id_raises_agent_error():
    """COR-7 (review 2026-06-23): a 202 body without a job_id must surface a
    clean AgentError, not a raw KeyError."""

    def handler(request):
        return httpx.Response(202, json={})  # malformed: no job_id

    client = _client(handler)
    with pytest.raises(AgentError, match="job_id"):
        await client.pull([{"url": "/x", "host": "h"}], user_agent="UA/1.0")


async def test_post_then_poll_deadline_raises_agent_error():
    """MEM-2 (review 2026-06-23): a job that never reaches a terminal state must
    not poll forever — a poll deadline bounds it and raises AgentError."""

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"state": "running", "done": 1, "total": 2})

    transport = httpx.MockTransport(handler)
    client = AgentClient(
        base_url="http://agent:8780",
        token=TOKEN,
        transport=transport,
        poll_interval_sec=0.0,
        poll_timeout_sec=0.0,  # deadline immediately past → one poll then raise
    )
    with pytest.raises(AgentError, match="did not finish"):
        await client.pull([{"url": "/x", "host": "h"}], user_agent="UA/1.0")


async def test_poll_timeout_override_reported_in_error():
    """Regression: the deadline-exceeded error reports the EFFECTIVE per-call
    poll_timeout override, not the instance default (fetch_manifests uses a 6h
    override; a message citing the 2h default would misdirect an operator)."""

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"state": "running"})

    client = _client(handler)  # instance default poll_timeout_sec=7200.0
    with pytest.raises(AgentError, match=r"within 0\.0s"):
        await client._post_then_poll("/v1/steam/fetch-manifests", {}, poll_timeout=0.0)


async def test_steam_validate_single_call():
    def handler(request):
        assert request.url.path == "/v1/steam/validate"
        return httpx.Response(
            200,
            json={
                "chunks_total": 60,
                "chunks_cached": 60,
                "chunks_missing": 0,
                "outcome": "cached",
                "versions": "1018131:x",
                "error": None,
            },
        )

    client = _client(handler)
    res = await client.steam_validate(1018130)
    assert res["chunks_cached"] == 60
    assert res["outcome"] == "cached"


# --- UAT-12: agent-call resilience (transient connect-blip tolerance) ---


async def test_poll_tolerates_transient_connect_blip():
    """A transient connect blip on a GET poll is retried inside _request, so a
    single hiccup mid-prefill does not kill the whole multi-hour job. The agent's
    uvicorn listener can be briefly CPU-starved during a heavy prefill on the
    steal-bound VM, lagging accept() past the connect timeout."""
    state = {"polls": 0}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "s1"})
        state["polls"] += 1
        if state["polls"] <= 2:  # two transient connect failures, then done
            raise httpx.ConnectTimeout("listener starved")
        return httpx.Response(200, json={"state": "done", "result": {"ok": True}})

    client = _client(handler)
    result = await client.steam_prefill([440])
    assert result["ok"] is True
    assert state["polls"] == 3  # 2 blips retried + 1 success


async def test_connect_failure_exhausts_retries_then_raises():
    """A persistent connect failure still raises AgentError, but only after the
    bounded retries (default 2 → 3 attempts) — not on the first blip."""
    state = {"attempts": 0}

    def handler(request):
        state["attempts"] += 1
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])
    assert state["attempts"] == 3  # 1 initial + 2 retries


async def test_http_status_error_is_not_retried():
    """A real HTTP error response (e.g. 500) is NOT a transient connect blip —
    it propagates immediately without consuming the connect-retry budget."""
    state = {"attempts": 0}

    def handler(request):
        state["attempts"] += 1
        return httpx.Response(500)

    client = _client(handler)
    with pytest.raises(AgentError, match="returned 500"):
        await client.stat(["a" * 32])
    assert state["attempts"] == 1  # not retried


async def test_post_connect_blip_is_retried():
    """A connect blip on the dispatch POST is safe to retry — the connection was
    never established so the request never reached the agent (no duplicate job)."""
    state = {"posts": 0}

    def handler(request):
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                raise httpx.ConnectTimeout("starved")
            return httpx.Response(202, json={"job_id": "s1"})
        return httpx.Response(200, json={"state": "done", "result": {"ok": True}})

    client = _client(handler)
    result = await client.steam_prefill([440])
    assert result["ok"] is True
    assert state["posts"] == 2  # first POST blipped, retried to success


async def test_default_connect_timeout_raised_to_15s():
    """Default connect timeout bumped 10s -> 15s to absorb brief accept-lag on the
    CPU-contended agent VM in a single attempt."""
    c = AgentClient(base_url="http://agent:8780", token=TOKEN)
    assert c._timeout.connect == 15.0


async def test_default_poll_interval_raised_to_3s():
    """Default poll interval bumped 0.5s -> 3s: a multi-hour job needs far fewer
    connects, cutting the chance of hitting a connect blip ~6x."""
    c = AgentClient(base_url="http://agent:8780", token=TOKEN)
    assert c._poll == 3.0


async def test_steam_validate_unreachable_raises():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.steam_validate(1018130)


async def test_agent_health_single_call():
    """re-arch ④: agent_health() does a single GET /v1/health and returns the
    body, so the control plane can source validator health from the agent."""

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/health"
        return httpx.Response(200, json={"ok": True, "validator_healthy": True})

    client = _client(handler)
    body = await client.agent_health()
    assert body == {"ok": True, "validator_healthy": True}


async def test_steam_validate_uses_long_timeout():
    # A large validate (tens of thousands of chunks) stat's many files over NFS
    # and can take well over the default 30s; steam_validate must use a generous
    # per-call timeout so it doesn't AgentError on big games.
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "chunks_total": 1,
                "chunks_cached": 1,
                "chunks_missing": 0,
                "outcome": "cached",
                "versions": "",
                "error": None,
            },
        )

    client = _client(handler)
    await client.steam_validate(1)
    assert seen["timeout"]["read"] == 300.0


async def test_reuses_single_client_across_calls():
    """Re-arch ④ §3b-1: the cross-host client is built once and reused, not
    rebuilt per request."""
    built = []

    def handler(request):
        return httpx.Response(200, json={"cached": 1, "missing": 0})

    transport = httpx.MockTransport(handler)
    client = AgentClient(base_url="http://agent:8780", token=TOKEN, transport=transport)
    import orchestrator.clients.agent_client as mod

    real = mod.httpx.AsyncClient

    def counting(*a, **k):
        c = real(*a, **k)
        built.append(c)
        return c

    mod.httpx.AsyncClient = counting  # type: ignore[assignment]
    try:
        await client.stat(["a" * 32])
        await client.stat(["b" * 32])
    finally:
        mod.httpx.AsyncClient = real  # type: ignore[assignment]
        await client.aclose()
    assert len(built) == 1  # ONE client built, reused for both stat calls


async def test_aclose_is_idempotent():
    client = AgentClient(base_url="http://agent:8780", token=TOKEN)
    await client.aclose()  # never used → no client built, no error
    await client.aclose()  # twice → safe


async def test_fetch_manifests_posts_and_polls():
    def handler(request):
        if request.method == "POST":
            assert request.url.path == "/v1/steam/fetch-manifests"
            return httpx.Response(202, json={"job_id": "m1"})
        assert request.url.path == "/v1/steam/fetch-manifests/m1"
        return httpx.Response(
            200,
            json={"state": "done", "result": {"fetched": 5, "skipped": 0, "failed": 0, "apps": 5}},
        )

    client = _client(handler)
    result = await client.fetch_manifests()
    assert result == {"fetched": 5, "skipped": 0, "failed": 0, "apps": 5}


async def test_aclose_closes_underlying_and_rebuilds_on_next_call():
    """aclose() actually closes the underlying httpx client, and the next call
    transparently rebuilds a fresh one."""

    def handler(request):
        return httpx.Response(200, json={"cached": 1, "missing": 0})

    client = _client(handler)
    await client.stat(["a" * 32])  # builds the client
    underlying = client._client
    assert underlying is not None and not underlying.is_closed
    await client.aclose()
    assert underlying.is_closed
    assert client._client is None
    # Next call rebuilds transparently.
    await client.stat(["b" * 32])
    assert client._client is not None and not client._client.is_closed
    await client.aclose()


async def test_epic_validate_single_call():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/epic/validate"
        body = request.content
        import json

        payload = json.loads(body)
        assert payload["app_id"] == 1449820
        assert payload["version"] == "21.0"
        assert payload["cdn_base"] == "https://epicgames-download.akamaized.net"
        assert payload["raw_manifest_b64"] == "dGVzdA=="
        return httpx.Response(
            200,
            json={
                "chunks_total": 120,
                "chunks_cached": 85,
                "chunks_missing": 35,
                "outcome": "partial",
                "versions": "21.0:x",
                "error": None,
            },
        )

    client = _client(handler)
    res = await client.epic_validate(
        app_id=1449820,
        version="21.0",
        cdn_base="https://epicgames-download.akamaized.net",
        raw_manifest_b64="dGVzdA==",
    )
    assert res["chunks_cached"] == 85
    assert res["outcome"] == "partial"


async def test_prune_steam_selection_posts_and_returns():
    import json as _json

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/steam/prune-selection"
        body = _json.loads(request.content)
        assert body == {"exclude_app_ids": [2, 3], "restore_app_ids": [5]}
        return httpx.Response(200, json={"removed": 2, "restored": 1, "remaining": 10})

    client = _client(handler)
    res = await client.prune_steam_selection([2, 3], [5])
    assert res == {"removed": 2, "restored": 1, "remaining": 10}


# --- F18 purge -------------------------------------------------------------


async def test_steam_purge_single_call():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/steam/purge"
        import json

        assert json.loads(request.content) == {"app_id": 440}
        return httpx.Response(200, json={"deleted": 3, "failed": 0, "bytes_freed": 999})

    client = _client(handler)
    res = await client.steam_purge(440)
    assert res == {"deleted": 3, "failed": 0, "bytes_freed": 999}


async def test_epic_purge_single_call():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/epic/purge"
        import json

        payload = json.loads(request.content)
        assert payload == {
            "app_id": 1449820,
            "version": "21.0",
            "cdn_base": "https://epicgames-download.akamaized.net",
            "raw_manifest_b64": "dGVzdA==",
        }
        return httpx.Response(200, json={"deleted": 120, "failed": 1, "bytes_freed": 42})

    client = _client(handler)
    res = await client.epic_purge(
        app_id=1449820,
        version="21.0",
        cdn_base="https://epicgames-download.akamaized.net",
        raw_manifest_b64="dGVzdA==",
    )
    assert res == {"deleted": 120, "failed": 1, "bytes_freed": 42}


async def test_steam_purge_non_2xx_raises():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.steam_purge(440)
