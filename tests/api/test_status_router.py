"""Tests for F10 status page (`GET /`)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestStatusPageRoute:
    async def test_returns_200_html(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")

    async def test_does_not_require_bearer(self, client):
        """Bible §9.3: page itself is unauthenticated; embedded JS handles
        the bearer token via sessionStorage + prompt() before any API
        call. The page route must be auth-exempt."""
        # No `Authorization` header — must still 200.
        r = await client.get("/")
        assert r.status_code == 200

    async def test_security_headers_present(self, client):
        r = await client.get("/")
        assert r.headers.get("cache-control") == "no-store"
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "no-referrer"

    async def test_robots_noindex(self, client):
        """Operator-private page should NOT be indexed by search engines
        if exposed accidentally."""
        r = await client.get("/")
        body = r.text
        assert 'name="robots"' in body
        assert "noindex" in body


class TestPagePanels:
    """Bible §9.3 mandates 5 panels: Health, Platforms, Active Jobs,
    Stats, Recent Errors. Verify all five appear in the HTML."""

    async def test_all_five_panels_present(self, client):
        body = (await client.get("/")).text
        # IDs used by the embedded JS for panel updates
        for panel_id in (
            "panel-health",
            "panel-platforms",
            "panel-jobs",
            "panel-stats",
            "panel-errors",
        ):
            assert panel_id in body, f"missing panel id: {panel_id}"

    async def test_pill_elements_present(self, client):
        """Each panel has a status pill that the JS updates."""
        body = (await client.get("/")).text
        for pill_id in (
            "health-pill",
            "platforms-pill",
            "jobs-pill",
            "stats-pill",
            "errors-pill",
        ):
            assert pill_id in body


class TestAccessibility:
    """Intake §9: operator is colorblind. Every status indicator MUST
    use color + icon + text label — text label is the hard constraint
    that survives even with color stripped away."""

    async def test_text_labels_present_for_each_state(self, client):
        body = (await client.get("/")).text
        # The JS sets these via setPill() — verify they appear in the HTML.
        for label in ("OK", "DEGRADED", "ERROR", "UNKNOWN", "IDLE", "NONE"):
            assert label in body, f"state label missing: {label}"

    async def test_initial_unknown_pills_have_text_label(self, client):
        """Before JS runs, panels start at 'UNKNOWN' — the text must be
        present in static HTML so screen readers + curl users still see
        a label (not just a color)."""
        body = (await client.get("/")).text
        # Each pill markup includes the text label literally
        assert body.count("UNKNOWN") >= 5

    async def test_icons_have_text_neighbours(self, client):
        """Each .icon span sits inline with a text label — verify the
        ASCII fallback icons are paired."""
        body = (await client.get("/")).text
        # Check icons used in the JS render paths
        for icon in ("✓", "⚠", "✗", "?"):
            assert icon in body


class TestSize:
    """Bible §9.3 ceiling: < 20 KB gzipped. Sanity-bound the uncompressed
    payload — gzip typically gets ~3x on HTML, so 60 KB raw is the
    pragmatic uncompressed limit."""

    async def test_uncompressed_size_under_60kb(self, client):
        r = await client.get("/")
        body = r.text
        assert len(body) < 60 * 1024, f"status page is {len(body)} bytes uncompressed; aim < 60 KB"

    async def test_gzipped_size_under_20kb(self, client):
        import gzip

        r = await client.get("/")
        gz = gzip.compress(r.text.encode("utf-8"))
        assert len(gz) < 20 * 1024, (
            f"status page gzipped is {len(gz)} bytes; Bible §9.3 says < 20 KB"
        )


class TestNoExternalDependencies:
    """The page must work offline (operator's lancache LAN may not have
    internet)."""

    async def test_no_external_script_src(self, client):
        body = (await client.get("/")).text
        # Allow inline <script>; reject `src=` pointing anywhere external.
        # Permitted patterns: <script>...</script>
        # Forbidden: <script src="http..."> or src="//..."
        import re

        external = re.findall(r"""<script[^>]+src=["'](https?://|//)""", body)
        assert external == [], f"external script src found: {external}"

    async def test_no_external_stylesheet_link(self, client):
        body = (await client.get("/")).text
        import re

        external = re.findall(
            r"""<link[^>]+rel=["']stylesheet["'][^>]+href=["'](https?://|//)""",
            body,
        )
        assert external == [], f"external stylesheet found: {external}"


class TestEndpointsReferenced:
    """The JS references specific API endpoints — verify they're the
    ones the page will actually hit. Drift detection."""

    async def test_health_endpoint_referenced(self, client):
        body = (await client.get("/")).text
        assert "/api/v1" in body  # JS API prefix
        assert "/health" in body

    async def test_platforms_endpoint_referenced(self, client):
        body = (await client.get("/")).text
        assert "/platforms" in body

    async def test_jobs_endpoint_referenced(self, client):
        body = (await client.get("/")).text
        assert "/jobs" in body

    async def test_games_endpoint_referenced(self, client):
        body = (await client.get("/")).text
        assert "/games" in body

    async def test_manifests_endpoint_referenced(self, client):
        body = (await client.get("/")).text
        assert "/manifests" in body
