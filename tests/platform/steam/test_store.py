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
    # No categories in the response -> flags are unknown (None).
    assert await store.fetch_app_info(440) == {
        "type": "game",
        "name": "Team Fortress 2",
        "has_single_player": None,
        "has_multiplayer": None,
    }


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
    assert await store.fetch_app_info(570) == {
        "type": "dlc",
        "name": "Dota Plus",
        "has_single_player": None,
        "has_multiplayer": None,
    }


def _cats(*ids: int) -> list[dict]:
    return [{"id": i, "description": f"cat {i}"} for i in ids]


async def test_multiplayer_only_categories_set_flags(monkeypatch):
    # Dota 2: Multi-player(1) + Co-op(9), NO Single-player(2).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "570": {
                    "success": True,
                    "data": {"type": "game", "name": "Dota 2", "categories": _cats(1, 9, 29)},
                }
            },
        )

    _patch_transport(monkeypatch, handler)
    info = await store.fetch_app_info(570)
    assert info == {
        "type": "game",
        "name": "Dota 2",
        "has_single_player": 0,
        "has_multiplayer": 1,
    }


async def test_single_and_multiplayer_categories_set_both_flags(monkeypatch):
    # Portal 2: Single-player(2) + Multi-player(1) + Co-op(9).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "620": {
                    "success": True,
                    "data": {"type": "game", "name": "Portal 2", "categories": _cats(2, 1, 9)},
                }
            },
        )

    _patch_transport(monkeypatch, handler)
    info = await store.fetch_app_info(620)
    assert info["has_single_player"] == 1
    assert info["has_multiplayer"] == 1


async def test_single_player_only_categories(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "413150": {
                    "success": True,
                    "data": {"type": "game", "name": "Stardew Valley", "categories": _cats(2, 22)},
                }
            },
        )

    _patch_transport(monkeypatch, handler)
    info = await store.fetch_app_info(413150)
    assert info["has_single_player"] == 1
    assert info["has_multiplayer"] == 0


async def test_categories_with_only_non_gameplay_ids(monkeypatch):
    # Categories present but only Trading Cards(29)/Workshop(30) -> both flags 0.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "1": {
                    "success": True,
                    "data": {"type": "game", "name": "Cardy", "categories": _cats(29, 30)},
                }
            },
        )

    _patch_transport(monkeypatch, handler)
    info = await store.fetch_app_info(1)
    assert info["has_single_player"] == 0
    assert info["has_multiplayer"] == 0


async def test_empty_categories_list_leaves_flags_unknown(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"2": {"success": True, "data": {"type": "game", "name": "X", "categories": []}}},
        )

    _patch_transport(monkeypatch, handler)
    info = await store.fetch_app_info(2)
    assert info["has_single_player"] is None
    assert info["has_multiplayer"] is None
