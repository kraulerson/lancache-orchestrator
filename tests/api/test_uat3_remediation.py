"""Regression tests for UAT-3 remediation (2026-04-30).

Each test class corresponds to one finding from
tests/uat/sessions/2026-04-27-session-3/agent-results/_consolidated.md.
"""

from __future__ import annotations

import json

import pytest

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# S2-A: AUTH_EXEMPT_PREFIXES exact-match (no substring collision)
# ---------------------------------------------------------------------------


class TestS2AExemptPrefixExactMatch:
    async def test_health_canonical_exempt(self, client):
        r = await client.get("/api/v1/health")
        assert r.status_code != 401

    async def test_healthxxx_not_exempt(self, client):
        # /api/v1/healthxxx must NOT bypass auth via prefix substring match.
        r = await client.get("/api/v1/healthxxx")
        assert r.status_code == 401

    async def test_health_subpath_not_exempt(self, client):
        # /api/v1/health/whatever — currently no such route, must require auth.
        r = await client.get("/api/v1/health/internal-debug")
        assert r.status_code == 401

    async def test_docs_canonical_exempt(self, client):
        r = await client.get("/api/v1/docs")
        assert r.status_code != 401

    async def test_docszzz_not_exempt(self, client):
        r = await client.get("/api/v1/docszzz")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# S2-B: git_sha truncated in unauth /health response
# ---------------------------------------------------------------------------


class TestS2BGitShaTruncation:
    async def test_git_sha_truncated_to_8_chars(self, client, unit_app):
        # Set a full-length 40-char SHA to simulate CI-set GIT_SHA env.
        unit_app.state.git_sha = "0123456789abcdef0123456789abcdef01234567"
        r = await client.get("/api/v1/health")
        body = r.json()
        # The unauth response should not leak the full 40-char SHA.
        assert len(body["git_sha"]) <= 8

    async def test_git_sha_unknown_passes_through(self, client, unit_app):
        unit_app.state.git_sha = "unknown"
        r = await client.get("/api/v1/health")
        body = r.json()
        assert body["git_sha"] == "unknown"


# ---------------------------------------------------------------------------
# S2-C + S3-h: openapi/docs/redoc loopback-restricted; IPv6 ::1 honored
# ---------------------------------------------------------------------------


class TestS2CS3hLoopbackRestrictedSchema:
    async def test_loopback_can_fetch_openapi(self, loopback_client):
        r = await loopback_client.get("/api/v1/openapi.json")
        assert r.status_code == 200

    async def test_loopback_can_fetch_docs(self, loopback_client):
        r = await loopback_client.get("/api/v1/docs")
        assert r.status_code == 200

    async def test_loopback_can_fetch_redoc(self, loopback_client):
        r = await loopback_client.get("/api/v1/redoc")
        assert r.status_code == 200

    async def test_external_blocked_from_openapi(self, external_client):
        r = await external_client.get("/api/v1/openapi.json")
        assert r.status_code == 403

    async def test_external_blocked_from_docs(self, external_client):
        r = await external_client.get("/api/v1/docs")
        assert r.status_code == 403

    async def test_external_blocked_from_redoc(self, external_client):
        r = await external_client.get("/api/v1/redoc")
        assert r.status_code == 403

    async def test_ipv6_loopback_treated_as_loopback(self, unit_app):
        import httpx

        transport = httpx.ASGITransport(app=unit_app, client=("::1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/openapi.json")
        assert r.status_code == 200, "IPv6 ::1 must be treated as loopback"

    async def test_ipv4_mapped_ipv6_loopback_treated_as_loopback(self, unit_app):
        import httpx

        transport = httpx.ASGITransport(app=unit_app, client=("::ffff:127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/openapi.json")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# S2-D: startup warning on non-loopback bind (deployment hardening hint)
# ---------------------------------------------------------------------------


class TestS2DNonLoopbackStartupWarning:
    async def test_lifespan_logs_warning_when_bound_to_0_0_0_0(self, db_path, monkeypatch, capsys):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app
        from orchestrator.core.logging import configure_logging
        from orchestrator.core.settings import reload_settings

        configure_logging()
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("ORCH_API_HOST", "0.0.0.0")  # noqa: S104
        reload_settings()

        app = create_app()
        async with LifespanManager(app):
            pass

        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e.get("event") for e in events]
        assert "api.boot.non_loopback_bind_warning" in names


# ---------------------------------------------------------------------------
# S2-F: CORS outermost — short-circuit responses include ACAO
# ---------------------------------------------------------------------------


class TestS2FCorsOutermost:
    async def test_middleware_order_cors_outermost(self):
        # Structural assertion: in the new ordering, CORS must be the
        # outermost middleware so 401/413 short-circuits get ACAO headers.
        from orchestrator.api.main import create_app

        app = create_app()
        names = [m.cls.__name__ for m in app.user_middleware]
        # user_middleware[0] is OUTERMOST (FastAPI prepends).
        assert names.index("CORSMiddleware") < names.index("CorrelationIdMiddleware")
        assert names.index("CorrelationIdMiddleware") < names.index("BodySizeCapMiddleware")
        assert names.index("BodySizeCapMiddleware") < names.index("BearerAuthMiddleware")

    async def test_401_response_includes_acao_for_allowed_origin(self, populated_pool, monkeypatch):
        import httpx

        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.api.main import create_app
        from orchestrator.core.settings import reload_settings

        monkeypatch.setenv("ORCH_CORS_ORIGINS", '["http://localhost:3000"]')
        reload_settings()

        app = create_app()
        app.dependency_overrides[get_pool_dep] = lambda: populated_pool
        app.state.boot_time = 0.0
        app.state.git_sha = "test"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get(
                "/api/v1/private-route",
                headers={"Origin": "http://localhost:3000"},
            )
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# S2-G: BodySizeCap doesn't double-emit response.start mid-stream
# ---------------------------------------------------------------------------


class TestS2GBodyCapNoDuplicateStart:
    async def test_no_duplicate_start_when_response_already_begun(self):
        from orchestrator.api.middleware import BodySizeCapMiddleware

        sent_messages: list[dict] = []

        async def downstream_app(scope, receive, send):
            # Simulate streaming handler: start response, then read body.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            # Now consume body — eventually triggers cap
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break

        middleware = BodySizeCapMiddleware(downstream_app)

        # 33 KiB > 32 KiB cap
        chunks = [b"x" * 1024 for _ in range(33)]
        chunks.append(b"")

        async def receive():
            if not chunks:
                return {"type": "http.disconnect"}
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }

        async def send(message):
            sent_messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/whatever",
            "headers": [],
        }

        await middleware(scope, receive, send)

        # Count http.response.start frames — must NOT be duplicated.
        starts = [m for m in sent_messages if m["type"] == "http.response.start"]
        assert len(starts) <= 1, (
            f"BodySizeCap emitted {len(starts)} response.start frames; "
            "duplicate would violate ASGI protocol"
        )


# ---------------------------------------------------------------------------
# S2-H: Single oversized chunk rejected before allocation
# ---------------------------------------------------------------------------


class TestS2HSingleChunkBodyCap:
    async def test_single_oversized_chunk_rejected(self):
        from orchestrator.api.middleware import BodySizeCapMiddleware

        downstream_called: list[bool] = []
        sent_messages: list[dict] = []

        async def downstream_app(scope, receive, send):
            await receive()
            downstream_called.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = BodySizeCapMiddleware(downstream_app)

        # Single chunk that's already over the cap (1 MiB).
        big_chunk = b"x" * (1024 * 1024)

        async def receive():
            return {
                "type": "http.request",
                "body": big_chunk,
                "more_body": False,
            }

        async def send(message):
            sent_messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/whatever",
            "headers": [],  # streaming path
        }

        await middleware(scope, receive, send)

        assert not downstream_called, "downstream consumed the oversized chunk before cap fired"
        assert sent_messages
        assert sent_messages[0]["status"] == 413


# ---------------------------------------------------------------------------
# S2-I: module-level `app` exposed for standard `uvicorn module:app`
# ---------------------------------------------------------------------------


class TestS2IModuleLevelApp:
    def test_module_level_app_attribute_exists(self):
        import orchestrator.api.main as m

        assert hasattr(m, "app"), (
            "main.py must expose module-level `app` so `uvicorn module:app` works"
        )

    def test_module_level_app_is_fastapi_instance(self):
        from fastapi import FastAPI

        from orchestrator.api.main import app

        assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# S2-J: migrate.run_migrations wraps sqlite3.OperationalError
# ---------------------------------------------------------------------------


class TestS2JMigrateWrapsSqliteError:
    def test_unable_to_open_database_raises_migration_error(self, tmp_path):
        from orchestrator.db import migrate

        bad_path = tmp_path / "non-existent-dir" / "cant-write.db"
        with pytest.raises(migrate.MigrationError) as ei:
            migrate.run_migrations(bad_path)
        assert "open" in str(ei.value).lower() or "database" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# S3-a: lifespan partial-init cleanup — close_pool runs if init succeeded
# ---------------------------------------------------------------------------


class TestS3aLifespanPartialInitCleanup:
    async def test_close_pool_called_when_post_init_step_fails(self, db_path, monkeypatch):
        """If init_pool succeeds but a later boot step raises, close_pool
        must still execute so the writer connection isn't leaked."""
        from orchestrator.api import main as main_mod

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))

        close_called: list[bool] = []

        original_close = main_mod.close_pool

        async def spy_close():
            close_called.append(True)
            await original_close()

        monkeypatch.setattr(main_mod, "close_pool", spy_close)

        # Patch app.state assignment to raise — simulates a post-init failure.
        # Selectively raise only when main.py looks up GIT_SHA, so unrelated
        # callers of os.environ.get (pydantic plugin loader, etc.) keep working.
        real_get = main_mod.os.environ.get

        def selective_get(name, *a, **kw):
            if name == "GIT_SHA":
                raise RuntimeError("simulated post-init failure")
            return real_get(name, *a, **kw)

        monkeypatch.setattr(main_mod.os.environ, "get", selective_get)

        app = main_mod.create_app()
        from asgi_lifespan import LifespanManager

        with pytest.raises(RuntimeError, match="simulated post-init failure"):
            async with LifespanManager(app):
                pass  # pragma: no cover

        assert close_called, "close_pool was not called during partial-init unwind"


# ---------------------------------------------------------------------------
# S3-k: scope["headers"] redaction handles list-of-(bytes,bytes) tuples
# ---------------------------------------------------------------------------


class TestS3kScopeHeadersRedaction:
    def test_redactor_walks_asgi_headers_shape(self):
        from orchestrator.core.logging import _redact_sensitive_values

        event = {
            "event": "test",
            "scope_headers": [
                (b"authorization", b"Bearer secret-token"),
                (b"content-type", b"application/json"),
                (b"x-trace-id", b"abc"),
            ],
        }
        redacted = _redact_sensitive_values(None, "info", event)
        # Sensitive header value must be redacted; non-sensitive intact.
        headers = redacted["scope_headers"]
        kv = dict(headers)
        assert kv[b"authorization"] != b"Bearer secret-token"
        assert kv[b"content-type"] == b"application/json"
        assert kv[b"x-trace-id"] == b"abc"


# ---------------------------------------------------------------------------
# S3-m: lowercase `bearer` scheme accepted (RFC 7235 §2.1 case-insensitive)
# ---------------------------------------------------------------------------


class TestS2DBindDetection:
    """UAT-3 S2-D extension: detect non-loopback bind from any of three
    signals so the warning fires even when the operator uses bare CLI flags."""

    def test_detects_settings_api_host(self):
        from orchestrator.api.main import _detect_non_loopback_bind

        assert _detect_non_loopback_bind("0.0.0.0") == "0.0.0.0"  # noqa: S104
        assert _detect_non_loopback_bind("127.0.0.1") is None
        assert _detect_non_loopback_bind("::1") is None
        assert _detect_non_loopback_bind("localhost") is None

    def test_detects_uvicorn_host_env(self, monkeypatch):
        from orchestrator.api.main import _detect_non_loopback_bind

        monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")  # noqa: S104
        assert _detect_non_loopback_bind("127.0.0.1") == "0.0.0.0"  # noqa: S104

    def test_detects_argv_host_flag_separate(self, monkeypatch):
        import sys

        from orchestrator.api.main import _detect_non_loopback_bind

        monkeypatch.setattr(sys, "argv", ["uvicorn", "--host", "0.0.0.0", "module:app"])  # noqa: S104
        assert _detect_non_loopback_bind("127.0.0.1") == "0.0.0.0"  # noqa: S104

    def test_detects_argv_host_flag_equals(self, monkeypatch):
        import sys

        from orchestrator.api.main import _detect_non_loopback_bind

        monkeypatch.setattr(sys, "argv", ["uvicorn", "--host=0.0.0.0", "module:app"])
        assert _detect_non_loopback_bind("127.0.0.1") == "0.0.0.0"  # noqa: S104

    def test_returns_none_when_all_signals_loopback(self, monkeypatch):
        import sys

        from orchestrator.api.main import _detect_non_loopback_bind

        monkeypatch.delenv("UVICORN_HOST", raising=False)
        monkeypatch.setattr(sys, "argv", ["uvicorn", "--host", "127.0.0.1", "module:app"])
        assert _detect_non_loopback_bind("127.0.0.1") is None


class TestS2JNoTracebackOnSystemExit:
    """UAT-3 S2-J full suppression: the SystemExit raised in the lifespan
    must not chain a __cause__ that Starlette will print as a traceback."""

    async def test_migration_failure_systemexit_has_no_cause(self, monkeypatch, tmp_path):
        # Bypass asgi-lifespan (which swallows SystemExit) and use FastAPI's
        # native lifespan_context per the existing test_lifespan.py pattern.
        from orchestrator.api.main import create_app

        bad_path = tmp_path / "non-existent-dir" / "cant-write.db"
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(bad_path))

        app = create_app()
        with pytest.raises(SystemExit) as ei:
            async with app.router.lifespan_context(app):
                pass  # pragma: no cover
        # `from None` should set __suppress_context__ so Starlette won't
        # print the underlying MigrationError traceback.
        assert ei.value.__cause__ is None
        assert ei.value.__suppress_context__ is True


class TestS3mLowercaseBearerScheme:
    async def test_lowercase_bearer_accepted(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 404, "lowercase 'bearer' scheme must be accepted (RFC 7235 §2.1)"

    async def test_uppercase_bearer_accepted(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"BEARER {VALID_TOKEN}"},
        )
        assert r.status_code == 404

    async def test_mixed_case_bearer_accepted(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"BeArEr {VALID_TOKEN}"},
        )
        assert r.status_code == 404
