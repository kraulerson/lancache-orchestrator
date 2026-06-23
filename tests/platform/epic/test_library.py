"""F6: Epic library enumeration (paginated)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.platform.epic import library as ep_lib

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


def _settings() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


async def test_enumerate_paginates(monkeypatch):
    pages = {
        None: {
            "records": [
                {"appName": "A", "namespace": "ns", "catalogItemId": "c1"},
            ],
            "responseMetadata": {"nextCursor": "CUR"},
        },
        "CUR": {
            "records": [
                {
                    "appName": "B",
                    "namespace": "ns",
                    "catalogItemId": "c2",
                    "metadata": {"title": "Game B"},
                },
            ],
            "responseMetadata": {},
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        cur = dict(req.url.params).get("cursor")
        assert req.headers.get("authorization", "").lower().startswith("bearer ")
        return httpx.Response(200, json=pages[cur])

    monkeypatch.setattr(ep_lib, "_build_transport", lambda: httpx.MockTransport(handler))
    items = await ep_lib.enumerate_library("TOK", _settings())
    assert [i.app_name for i in items] == ["A", "B"]
    assert items[0].namespace == "ns"
    assert items[0].catalog_item_id == "c1"
    assert items[0].title == "A"  # falls back to appName
    assert items[1].title == "Game B"  # from metadata


async def test_enumerate_error_raises(monkeypatch):
    monkeypatch.setattr(
        ep_lib,
        "_build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(401, json={})),
    )
    with pytest.raises(ep_lib.EpicLibraryError):
        await ep_lib.enumerate_library("TOK", _settings())


async def test_to_item_carries_build_version():
    item = ep_lib._to_item(
        {
            "appName": "Fortnite",
            "namespace": "fn",
            "catalogItemId": "abc",
            "buildVersion": "++Fortnite-29.00",
        }
    )
    assert item is not None
    assert item.build_version == "++Fortnite-29.00"


async def test_to_item_build_version_optional():
    item = ep_lib._to_item({"appName": "X", "namespace": "n", "catalogItemId": "c"})
    assert item is not None
    assert item.build_version is None


async def test_enumerate_raises_on_repeated_cursor(monkeypatch):
    """COR-4 (review 2026-06-23): a server that returns the SAME nextCursor must
    not loop forever — detect the repeat and fail loudly."""
    import asyncio

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"records": [], "responseMetadata": {"nextCursor": "STUCK"}}
        )

    monkeypatch.setattr(ep_lib, "_build_transport", lambda: httpx.MockTransport(handler))
    async with asyncio.timeout(5):  # hang detector
        with pytest.raises(ep_lib.EpicLibraryError, match="cursor"):
            await ep_lib.enumerate_library("TOK", _settings())


async def test_enumerate_caps_pages(monkeypatch):
    """COR-4: a server feeding endless DISTINCT cursors is bounded by a page cap
    rather than paginating without limit."""
    monkeypatch.setattr(ep_lib, "_MAX_PAGES", 3)
    seen = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(
            200,
            json={"records": [], "responseMetadata": {"nextCursor": f"c{seen['n']}"}},
        )

    monkeypatch.setattr(ep_lib, "_build_transport", lambda: httpx.MockTransport(handler))
    with pytest.raises(ep_lib.EpicLibraryError, match="page"):
        await ep_lib.enumerate_library("TOK", _settings())
    assert seen["n"] == 3
