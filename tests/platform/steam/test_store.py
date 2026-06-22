"""Tests for orchestrator.platform.steam.store — public appdetails lookup.

The client fetches the Steam store's appdetails endpoint (no auth) and returns
{'type','name'} or None on any failure. Tests inject an httpx.MockTransport via
the module-level _build_transport seam (mirrors prefill/downloader.py)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.platform.steam import store

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch, handler) -> None:
    monkeypatch.setattr(store, "_build_transport", lambda: httpx.MockTransport(handler))


async def test_success_returns_type_and_name(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"440": {"success": True, "data": {"type": "game", "name": "Team Fortress 2"}}},
        )

    _patch_transport(monkeypatch, handler)
    assert await store.fetch_app_info(440) == {"type": "game", "name": "Team Fortress 2"}


async def test_success_false_returns_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"440": {"success": False}})

    _patch_transport(monkeypatch, handler)
    assert await store.fetch_app_info(440) is None


async def test_non_200_returns_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    _patch_transport(monkeypatch, handler)
    assert await store.fetch_app_info(440) is None


async def test_transport_error_returns_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_transport(monkeypatch, handler)
    assert await store.fetch_app_info(440) is None


async def test_dlc_type_returned(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"570": {"success": True, "data": {"type": "dlc", "name": "Dota Plus"}}},
        )

    _patch_transport(monkeypatch, handler)
    assert await store.fetch_app_info(570) == {"type": "dlc", "name": "Dota Plus"}
