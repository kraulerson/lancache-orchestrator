import ipaddress
import time

import httpx
import pytest
import pytest_asyncio

from orchestrator.api.main import _enforce_lan_bind_policy
from orchestrator.api.middleware import (
    SourceAllowlistMiddleware,
    _is_source_allowed,
)
from orchestrator.core.settings import Settings


def _nets(*entries):
    return [ipaddress.ip_network(e, strict=False) for e in entries]


class TestIsSourceAllowed:
    def test_loopback_always_allowed(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("127.0.0.1", nets) is True
        assert _is_source_allowed("::1", nets) is True
        assert _is_source_allowed("::ffff:127.0.0.1", nets) is True

    def test_exact_ip_match(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("10.100.23.102", nets) is True
        assert _is_source_allowed("10.100.23.103", nets) is False

    def test_cidr_range(self):
        nets = _nets("10.0.0.0/24")
        assert _is_source_allowed("10.0.0.55", nets) is True
        assert _is_source_allowed("10.0.1.1", nets) is False

    def test_ipv4_mapped_ipv6_matches_ipv4_entry(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("::ffff:10.100.23.102", nets) is True

    def test_ipv4_mapped_ipv6_of_unlisted_addr_rejected(self):
        # Regression guard: a mapped IPv6 of a NON-allowlisted address must
        # still fail closed (the normalization must not accidentally allow it).
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("::ffff:203.0.113.9", nets) is False

    def test_allow_any(self):
        nets = _nets("0.0.0.0/0")
        assert _is_source_allowed("8.8.8.8", nets) is True

    def test_none_client_rejected(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed(None, nets) is False

    def test_unparseable_client_rejected(self):
        nets = _nets("10.100.23.102")
        assert _is_source_allowed("not-an-ip", nets) is False


def _make_scope(client):
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/games",
        "headers": [],
        "client": client,
    }


class _Settings:
    """Minimal settings stand-in for the middleware's get_settings() call."""

    def __init__(self, networks):
        self.allowed_source_networks = networks


@pytest.mark.asyncio
class TestSourceAllowlistMiddleware:
    async def _run(self, monkeypatch, networks, client):
        import orchestrator.api.middleware as mw

        monkeypatch.setattr(mw, "get_settings", lambda: _Settings(networks))
        reached = {"v": False}

        async def downstream(scope, receive, send):
            reached["v"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await SourceAllowlistMiddleware(downstream)(_make_scope(client), receive, send)
        return reached["v"], sent

    async def test_empty_allowlist_is_noop_allows_any_source(self, monkeypatch):
        reached, sent = await self._run(monkeypatch, [], ("203.0.113.9", 5000))
        assert reached is True
        assert sent[0]["status"] == 200

    async def test_enforcing_rejects_unlisted_source_403(self, monkeypatch):
        import ipaddress

        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, ("203.0.113.9", 5000))
        assert reached is False
        assert sent[0]["status"] == 403

    async def test_enforcing_allows_listed_source(self, monkeypatch):
        import ipaddress

        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, ("10.100.23.102", 5000))
        assert reached is True
        assert sent[0]["status"] == 200

    async def test_enforcing_allows_loopback(self, monkeypatch):
        import ipaddress

        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, _sent = await self._run(monkeypatch, nets, ("127.0.0.1", 5000))
        assert reached is True

    async def test_enforcing_none_client_rejected(self, monkeypatch):
        import ipaddress

        nets = [ipaddress.ip_network("10.100.23.102")]
        reached, sent = await self._run(monkeypatch, nets, None)
        assert reached is False
        assert sent[0]["status"] == 403

    async def test_non_http_scope_passes_through(self, monkeypatch):
        import orchestrator.api.middleware as mw

        monkeypatch.setattr(mw, "get_settings", lambda: _Settings([]))
        reached = {"v": False}

        async def downstream(scope, receive, send):
            reached["v"] = True

        async def send(msg):
            pass

        async def receive():
            return {}

        await SourceAllowlistMiddleware(downstream)({"type": "lifespan"}, receive, send)
        assert reached["v"] is True


@pytest_asyncio.fixture
async def enforcing_app(populated_pool, monkeypatch):
    """App whose settings allow only 10.100.23.102 (+ loopback)."""
    from orchestrator.api.dependencies import get_pool_dep
    from orchestrator.api.main import create_app
    from orchestrator.core.settings import get_settings

    monkeypatch.setenv("ORCH_TOKEN", "t" * 32)
    monkeypatch.setenv("ORCH_ALLOWED_SOURCE_IPS", "10.100.23.102")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_pool_dep] = lambda: populated_pool
    app.state.boot_time = time.monotonic()
    app.state.git_sha = "test-sha-deadbeef"
    yield app
    get_settings.cache_clear()


def _client(app, host):
    transport = httpx.ASGITransport(app=app, client=(host, 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
class TestSourceAllowlistIntegration:
    async def test_unlisted_source_403_on_health(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 403
        assert r.json()["detail"] == "forbidden: source not allowed"

    async def test_unlisted_source_403_on_games(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/games", headers={"Authorization": "Bearer " + "t" * 32})
        assert r.status_code == 403

    async def test_listed_source_without_token_401(self, enforcing_app):
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.get("/api/v1/games")
        assert r.status_code == 401

    async def test_listed_source_with_token_200(self, enforcing_app):
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.get("/api/v1/games", headers={"Authorization": "Bearer " + "t" * 32})
        assert r.status_code == 200

    async def test_listed_nonloopback_still_blocked_from_oq2_auth(self, enforcing_app):
        # Passes the source gate but OQ2 still requires loopback for /auth.
        async with _client(enforcing_app, "10.100.23.102") as c:
            r = await c.post(
                "/api/v1/platforms/steam/auth",
                headers={"Authorization": "Bearer " + "t" * 32},
                json={},
            )
        assert r.status_code == 403

    async def test_rejected_source_still_gets_correlation_id(self, enforcing_app):
        async with _client(enforcing_app, "203.0.113.9") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 403
        assert "x-correlation-id" in {k.lower() for k in r.headers}


class TestLanBindGuard:
    def test_loopback_bind_empty_allowlist_ok(self):
        s = Settings(orchestrator_token="t" * 32, api_host="127.0.0.1")
        _enforce_lan_bind_policy(s)  # no raise

    def test_non_loopback_bind_without_allowlist_systemexit(self):
        s = Settings(orchestrator_token="t" * 32, api_host="0.0.0.0")  # noqa: S104
        with pytest.raises(SystemExit):
            _enforce_lan_bind_policy(s)

    def test_non_loopback_bind_with_allowlist_ok(self):
        s = Settings(
            orchestrator_token="t" * 32,
            api_host="0.0.0.0",  # noqa: S104
            allowed_source_ips=["10.100.23.102"],
        )
        _enforce_lan_bind_policy(s)  # no raise
