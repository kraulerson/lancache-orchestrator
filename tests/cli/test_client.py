"""F11: OrchClient HTTP wrapper + exit-code exceptions."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.cli.client import ApiError, ApiUnreachableError, AuthError, OrchClient


def _client(handler) -> OrchClient:
    c = OrchClient(base_url="http://orch.test", token="tok")
    c._transport = httpx.MockTransport(handler)  # test seam
    return c


def test_get_sends_bearer_and_returns_json():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    body = _client(handler).get("/api/v1/health")
    assert body == {"ok": True}
    assert seen["auth"] == "Bearer tok"
    assert seen["url"] == "http://orch.test/api/v1/health"


def test_get_passes_query_params():
    def handler(req: httpx.Request) -> httpx.Response:
        assert dict(req.url.params) == {"platform": "steam", "limit": "5"}
        return httpx.Response(200, json={"games": []})

    _client(handler).get("/api/v1/games", platform="steam", limit=5)


def test_get_drops_none_params():
    def handler(req: httpx.Request) -> httpx.Response:
        assert dict(req.url.params) == {"limit": "5"}  # platform=None omitted
        return httpx.Response(200, json={"games": []})

    _client(handler).get("/api/v1/games", platform=None, limit=5)


def test_post_sends_json_body():
    def handler(req: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(req.content) == {"code": "X"}
        return httpx.Response(202, json={"job_id": 7})

    assert _client(handler).post("/api/v1/platforms/epic/auth", json={"code": "X"}) == {"job_id": 7}


def test_connect_error_raises_api_unreachable():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiUnreachableError):
        _client(handler).get("/api/v1/health")


def test_401_raises_auth_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    with pytest.raises(AuthError):
        _client(handler).get("/api/v1/games")


def test_500_raises_api_error_with_detail():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "database unavailable"})

    with pytest.raises(ApiError, match="database unavailable"):
        _client(handler).get("/api/v1/games")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("x"),
        httpx.ConnectTimeout("x"),
        httpx.ReadTimeout("x"),
        httpx.PoolTimeout("x"),
        httpx.WriteError("x"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_all_transport_errors_map_to_unreachable(exc):
    """Every httpx.TransportError (incl. server-disconnect on a mid-deploy
    restart) must map to ApiUnreachableError (exit 2), not escape to a traceback."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(ApiUnreachableError):
        _client(handler).get("/api/v1/health")


def test_malformed_base_url_maps_to_unreachable():
    """A --url with a control char makes httpx.Client(base_url=...) raise
    httpx.InvalidURL during construction — NOT a TransportError. It must still
    map to ApiUnreachableError (exit 2), not escape as a raw traceback."""
    c = OrchClient(base_url="h\ttp://x", token="tok")  # tab in scheme
    with pytest.raises(ApiUnreachableError):
        c.get("/api/v1/health")


def test_get_health_tolerates_503_degraded_body():
    """`/health` returns 503-with-body as the degraded representation; get_health
    returns the body instead of raising."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "degraded", "validator_healthy": False})

    body = _client(handler).get_health()
    assert body["status"] == "degraded"


def test_401_surfaces_server_detail_not_just_token_hint():
    """A 401 during `auth steam` (valid ORCH_TOKEN, wrong Steam password) returns
    the server's detail; the CLI must surface it, not the misleading hardcoded
    'check ORCH_TOKEN' (UAT-11 S11-E-03)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "authentication failed: bad_credentials"})

    with pytest.raises(AuthError, match="bad_credentials"):
        _client(handler).get("/api/v1/platforms")


def test_401_without_detail_falls_back_to_token_hint():
    """A bare 401 (no detail) still hints at ORCH_TOKEN."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(AuthError, match="ORCH_TOKEN"):
        _client(handler).get("/api/v1/games")


# --- issue #265: bodiless / detail-less error responses --------------------
#
# `f"HTTP {status}: {detail}"` rendered a blank "HTTP 404: " when the body was
# empty — common from a reverse proxy, and worst in exactly the case it matters
# most: a wrong --url/ORCH_API_URL pointed at a host that does not serve the
# route. Two variants bypass a naive fix: `{"detail": null}` coerces to the
# truthy literal "None", and a whitespace-only body stays truthy.


def _raises_api_error(handler, path="/api/v1/games/7") -> str:
    with pytest.raises(ApiError) as exc:
        _client(handler).get(path)
    return str(exc.value)


def test_bodiless_error_names_the_request_instead_of_trailing_a_colon():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    msg = _raises_api_error(handler)
    assert "GET /api/v1/games/7" in msg
    assert not msg.rstrip().endswith(":")


def test_null_detail_does_not_render_the_literal_none():
    """`str(None)` is the truthy literal "None" — the raw value has to be
    tested before coercion, or the operator is told the error was "None"."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": None})

    msg = _raises_api_error(handler)
    assert "None" not in msg
    assert "GET /api/v1/games/7" in msg


def test_whitespace_only_detail_is_treated_as_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "   \n\t "})

    msg = _raises_api_error(handler)
    assert "GET /api/v1/games/7" in msg
    assert "HTTP 404:  " not in msg


def test_whitespace_only_body_is_treated_as_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"   \n  ")

    msg = _raises_api_error(handler)
    assert "GET /api/v1/games/7" in msg


def test_reason_phrase_is_never_used_as_the_fallback():
    """reason_phrase is server-controlled: httpx decodes it as ASCII with
    errors="ignore" and h11 accepts any non-CRLF bytes, so ANSI escapes could
    reach the operator's terminal. An earlier attempt at this fix used it and
    was reverted; this test is why it must not come back."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, extensions={"reason_phrase": b"\x1b[31mPWNED\x1b[0m Not Found"})

    msg = _raises_api_error(handler)
    assert "PWNED" not in msg
    assert "\x1b" not in msg


def test_method_is_reflected_for_non_get():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    with pytest.raises(ApiError, match=r"POST /api/v1/games/7/prefill"):
        _client(handler).post("/api/v1/games/7/prefill")


def test_real_detail_still_wins_over_the_fallback():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "game not found"})

    msg = _raises_api_error(handler)
    assert "game not found" in msg
    assert "no response body" not in msg


def test_structured_detail_is_still_rendered():
    """The API's own RequestValidationError handler returns `detail` as a LIST
    of per-field errors (e.g. the #263 out-of-range game_id 400). Treating a
    non-string detail as empty would hide the actual validation failure."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": [{"type": "less_than_equal", "loc": ["path", "game_id"], "msg": "x"}]},
        )

    msg = _raises_api_error(handler)
    assert "less_than_equal" in msg
    assert "no response body" not in msg


def test_401_with_null_detail_falls_back_to_token_hint_not_none():
    """The 401 branch coerces the same way and had the same defect."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": None})

    with pytest.raises(AuthError) as exc:
        _client(handler).get("/api/v1/games")
    assert "None" not in str(exc.value)
    assert "ORCH_TOKEN" in str(exc.value)
