"""F6: Epic manifest URL fetch + binary download + parse."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.platform.epic import manifest as ep_man
from orchestrator.platform.epic.models import EpicLibraryItem
from tests.platform.epic._manifest_fixtures import build_manifest, make_chunks

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


def _settings() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


async def test_fetch_manifest_returns_parsed_and_cdn_base(monkeypatch):
    raw = build_manifest(22, make_chunks(1))

    def handler(req: httpx.Request) -> httpx.Response:
        if "/assets/v2/" in req.url.path:
            assert req.headers.get("authorization", "").lower().startswith("bearer ")
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {
                            "manifests": [
                                {
                                    "uri": "https://epiccdn.test/abc/def.manifest",
                                    "queryParams": [{"name": "k", "value": "v"}],
                                }
                            ]
                        }
                    ]
                },
            )
        # manifest binary download (the signed URI, with query params applied)
        assert req.url.host == "epiccdn.test"
        assert dict(req.url.params).get("k") == "v"
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(ep_man, "_build_transport", lambda: httpx.MockTransport(handler))
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    m, cdn_host, cdn_base = await ep_man.fetch_manifest("TOK", item, _settings())
    assert m.version == 22
    assert len(m.chunks) == 1
    assert cdn_host == "epiccdn.test"
    assert cdn_base == "/abc"  # dir of the manifest path


async def test_fetch_manifest_size_cap(monkeypatch):
    big = b"x" * 1024

    def handler(req: httpx.Request) -> httpx.Response:
        if "/assets/v2/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "elements": [{"manifests": [{"uri": "https://epiccdn.test/abc/d.manifest"}]}]
                },
            )
        return httpx.Response(200, content=big)

    monkeypatch.setattr(ep_man, "_build_transport", lambda: httpx.MockTransport(handler))
    s = Settings(orchestrator_token=VALID_TOKEN, manifest_size_cap_bytes=512)
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    with pytest.raises(ep_man.EpicManifestError, match="size cap"):
        await ep_man.fetch_manifest("TOK", item, s)


async def test_fetch_manifest_rejects_non_fqdn_host_before_get(monkeypatch):
    raw = build_manifest(22, make_chunks(1))
    downloaded = {"hit": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if "/assets/v2/" in req.url.path:
            # A bare (non-dotted) internal hostname — must be rejected.
            return httpx.Response(
                200,
                json={"elements": [{"manifests": [{"uri": "http://internal-host/a/d.manifest"}]}]},
            )
        downloaded["hit"] = True  # SSRF: the unvalidated host must NEVER be fetched
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(ep_man, "_build_transport", lambda: httpx.MockTransport(handler))
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    with pytest.raises(ep_man.EpicManifestError, match="CDN host"):
        await ep_man.fetch_manifest("TOK", item, _settings())
    assert downloaded["hit"] is False  # validation must precede the manifest GET


async def test_fetch_manifest_rejects_path_traversal_before_get(monkeypatch):
    raw = build_manifest(22, make_chunks(1))
    downloaded = {"hit": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if "/assets/v2/" in req.url.path:
            # Dotted host passes the FQDN guard, but the path carries traversal.
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"manifests": [{"uri": "https://epiccdn.test/a/../../x/d.manifest"}]}
                    ]
                },
            )
        downloaded["hit"] = True
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(ep_man, "_build_transport", lambda: httpx.MockTransport(handler))
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    with pytest.raises(ep_man.EpicManifestError, match="path traversal"):
        await ep_man.fetch_manifest("TOK", item, _settings())
    assert downloaded["hit"] is False  # traversal rejected before the manifest GET


async def test_fetch_manifest_no_elements_raises(monkeypatch):
    monkeypatch.setattr(
        ep_man,
        "_build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(200, json={"elements": []})),
    )
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    with pytest.raises(ep_man.EpicManifestError):
        await ep_man.fetch_manifest("TOK", item, _settings())
