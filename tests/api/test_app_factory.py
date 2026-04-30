"""Tests for orchestrator.api.main.create_app() factory shape (spec §7.2)."""

from __future__ import annotations

from orchestrator.api.dependencies import AUTH_EXEMPT_PREFIXES
from orchestrator.api.main import create_app


class TestAppFactory:
    def test_create_app_returns_fastapi_instance(self):
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_health_route_mounted(self):
        app = create_app()
        paths = {route.path for route in app.routes}
        assert "/api/v1/health" in paths

    def test_openapi_security_scheme_registered(self):
        """Spec §3.3 + §5.4: even with auth-as-middleware, the OpenAPI
        schema must declare the bearer scheme so Swagger UI shows the
        Authorize button."""
        app = create_app()
        schema = app.openapi()
        assert "components" in schema
        assert "securitySchemes" in schema["components"]
        bearer = schema["components"]["securitySchemes"].get("bearerAuth")
        assert bearer is not None
        assert bearer["type"] == "http"
        assert bearer["scheme"] == "bearer"

    def test_middleware_order_matches_spec(self):
        """Spec §5.1 (revised post-UAT-3 S2-F): outermost-first order is
        CORS, CorrelationId, BodySizeCap, BearerAuth. CORS moved to outermost
        so 401/413 short-circuits include ACAO headers and the browser
        surfaces the real status to the operator. add_middleware prepends
        so user_middleware[0] is outermost."""
        app = create_app()
        names = [m.cls.__name__ for m in app.user_middleware]
        assert names.index("CORSMiddleware") < names.index("CorrelationIdMiddleware")
        assert names.index("CorrelationIdMiddleware") < names.index("BodySizeCapMiddleware")
        assert names.index("BodySizeCapMiddleware") < names.index("BearerAuthMiddleware")

    def test_auth_exempt_prefixes_align_with_documented_routes(self):
        """Spec §4: the exempt list must include /api/v1/health (the only
        unauthenticated handler in BL5) and the OpenAPI/Swagger paths."""
        assert "/api/v1/health" in AUTH_EXEMPT_PREFIXES
        assert any(p.endswith("/openapi.json") for p in AUTH_EXEMPT_PREFIXES)
        assert any(p.endswith("/docs") for p in AUTH_EXEMPT_PREFIXES)
