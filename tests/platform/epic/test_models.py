"""F6: Epic data-model construction smoke tests."""

from __future__ import annotations

from orchestrator.platform.epic.models import (
    AuthTokens,
    EpicChunk,
    EpicLibraryItem,
    EpicManifest,
)


def test_models_construct():
    t = AuthTokens(
        access_token="a",
        refresh_token="r",
        account_id="id",
        display_name="n",
        expires_at="2026-06-03T00:00:00Z",
    )
    assert t.access_token == "a"

    c = EpicChunk(
        guid=(1, 2, 3, 4),
        hash=5,
        sha_hash=b"x" * 20,
        group_num=6,
        file_size=7,
        window_size=8,
    )
    m = EpicManifest(version=22, chunks=[c], cdn_base="http://cdn/path")
    assert m.version == 22
    assert m.chunks[0].group_num == 6

    item = EpicLibraryItem(app_name="App", namespace="ns", catalog_item_id="cat", title="T")
    assert item.app_name == "App"
