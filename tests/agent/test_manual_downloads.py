"""Tests for the agent GET /v1/manual-downloads/{launcher} endpoint (#222).

Lists the manually-downloaded game folders under
``manual_downloads_cache_path/<launcher>/`` so the control plane can diff them
against the owned library (Game_shelf). Read-only; the launcher path component is
strictly sanitized against traversal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

AUTH = {"Authorization": "Bearer " + "a" * 32}


def _settings(root: Path) -> Settings:
    return Settings(orchestrator_token="a" * 32, manual_downloads_cache_path=root)


def _seed_gog(root: Path) -> None:
    gog = root / "GOG"
    for name in ("alien_breed_2_assault", "akalabeth_world_of_doom", "trine_2"):
        (gog / name).mkdir(parents=True)
    # Special entries that must be filtered out.
    (gog / "!downloading").mkdir()
    (gog / "!orphaned").mkdir()
    (gog / "README.md").write_text("notes")


def test_lists_game_folders(tmp_path):
    _seed_gog(tmp_path)
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    r = client.get("/v1/manual-downloads/GOG")
    assert r.status_code == 200
    body = r.json()
    assert body["launcher"] == "GOG"
    assert body["present"] is True
    assert body["entries"] == ["akalabeth_world_of_doom", "alien_breed_2_assault", "trine_2"]


def test_filters_special_entries_and_files(tmp_path):
    _seed_gog(tmp_path)
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    entries = client.get("/v1/manual-downloads/GOG").json()["entries"]
    assert "!downloading" not in entries
    assert "!orphaned" not in entries
    assert "README.md" not in entries  # a file, not a game folder


def test_missing_launcher_folder_is_present_false(tmp_path):
    # Humble not downloaded yet -> folder absent -> present false, no error.
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    r = client.get("/v1/manual-downloads/Humble")
    assert r.status_code == 200
    assert r.json() == {"launcher": "Humble", "present": False, "entries": []}


def test_rejects_path_traversal_launcher(tmp_path):
    _seed_gog(tmp_path)
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    # A dot/slash launcher must never escape the cache root.
    for bad in ("..", "../cache", "GOG/..", "%2e%2e"):
        r = client.get(f"/v1/manual-downloads/{bad}")
        assert r.status_code in (400, 404), bad  # rejected or not-matched, never traversed


def test_requires_auth(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/v1/manual-downloads/GOG").status_code == 401
