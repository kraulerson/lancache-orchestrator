"""Tests for orchestrator-side session metadata file (BL10)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path  # noqa: TC003 — used at runtime via tmp_path type annotation


class TestSessionMetadataWrite:
    def test_writes_metadata_atomically(self, tmp_path: Path):
        from orchestrator.platform.steam.session import write_session_metadata

        target = tmp_path / "steam_session.json"
        write_session_metadata(
            target,
            steam_id=76561198000000000,
            username="alice",
            session_token_for_sha="opaque-token-bytes",  # noqa: S106 — test sentinel
        )
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["steam_id"] == 76561198000000000
        assert data["username"] == "alice"
        assert "last_refreshed_at" in data
        assert "sha256_prefix" in data
        assert len(data["sha256_prefix"]) == 8
        assert data["auth_method_version"] == 1

    def test_metadata_file_is_mode_0600(self, tmp_path: Path):
        from orchestrator.platform.steam.session import write_session_metadata

        target = tmp_path / "steam_session.json"
        write_session_metadata(
            target,
            steam_id=1,
            username="u",
            session_token_for_sha="t",  # noqa: S106 — test sentinel
        )
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o600

    def test_sha256_prefix_is_first_8_hex_of_sha256(self, tmp_path: Path):
        from orchestrator.platform.steam.session import write_session_metadata

        target = tmp_path / "steam_session.json"
        token = "the-secret-refresh-token"  # noqa: S105 — test sentinel
        write_session_metadata(
            target,
            steam_id=1,
            username="u",
            session_token_for_sha=token,
        )
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
        data = json.loads(target.read_text())
        assert data["sha256_prefix"] == expected

    def test_does_not_contain_raw_token(self, tmp_path: Path):
        from orchestrator.platform.steam.session import write_session_metadata

        target = tmp_path / "steam_session.json"
        token = "VERY_SECRET_TOKEN_AAAAAAAAAAAAAA"  # noqa: S105 — test sentinel, not a credential
        write_session_metadata(
            target,
            steam_id=1,
            username="u",
            session_token_for_sha=token,
        )
        content = target.read_text()
        assert token not in content

    def test_overwrite_uses_atomic_replace(self, tmp_path: Path):
        """Crash safety: an atomic os.replace means a partially-written
        file can never appear at the target path. Verified indirectly by
        confirming no temp file remains after a successful write."""
        from orchestrator.platform.steam.session import write_session_metadata

        target = tmp_path / "steam_session.json"
        write_session_metadata(target, steam_id=1, username="u", session_token_for_sha="t")  # noqa: S106
        # First call done; do a second
        write_session_metadata(target, steam_id=2, username="v", session_token_for_sha="t2")  # noqa: S106
        # No stray tempfiles
        leftovers = [p for p in tmp_path.iterdir() if p.name != "steam_session.json"]
        assert leftovers == []
