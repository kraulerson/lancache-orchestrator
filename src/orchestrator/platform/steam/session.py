"""Session metadata file (orchestrator-owned half of session persistence).

The steam-next subprocess manages its own credential files in
Settings.steam_session_dir (directory). This module writes a tiny
metadata JSON next to it at Settings.steam_session_path describing the
orchestrator's view of the session — never tokens.

File contract:
    {
        "steam_id": int,
        "username": str,
        "last_refreshed_at": iso8601 str,
        "sha256_prefix": str (first 8 hex of sha256(refresh_token)),
        "auth_method_version": int
    }

Atomic-write via os.replace from a tempfile in the same directory.
Mode 0600 on the final file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path  # noqa: TC003 — used at runtime (path.parent, mkdir, etc.)
from typing import Any

# Bumped if the schema ever changes; reader must reject unknown versions.
AUTH_METHOD_VERSION = 1


def write_session_metadata(
    path: Path,
    *,
    steam_id: int,
    username: str,
    session_token_for_sha: str,
) -> None:
    """Write the metadata JSON atomically with mode 0600.

    Args:
        path: target file (e.g. /var/lib/orchestrator/steam_session.json)
        steam_id: steam-next client.steam_id (int)
        username: username (identifier, not a credential — per Bible §7.2)
        session_token_for_sha: opaque refresh token; ONLY used to compute
            the 8-hex sha256 prefix for log correlation. The raw token is
            NEVER written to the file.
    """
    sha_prefix = hashlib.sha256(session_token_for_sha.encode("utf-8")).hexdigest()[:8]
    payload = {
        "steam_id": steam_id,
        "username": username,
        "last_refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256_prefix": sha_prefix,
        "auth_method_version": AUTH_METHOD_VERSION,
    }

    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a tempfile in the same directory (atomic rename requires
    # same filesystem). Set mode 0600 BEFORE rename so the file is never
    # world-readable on any filesystem with default-public umask.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".steam_session.",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.chmod(tmp_path_str, 0o600)
        os.replace(tmp_path_str, str(path))
    except Exception:
        # Best-effort cleanup of tempfile on failure
        with contextlib.suppress(OSError):
            os.unlink(tmp_path_str)
        raise


def read_session_metadata(path: Path) -> dict[str, Any] | None:
    """Read the metadata file, or None if missing/unparseable/unknown-version."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("auth_method_version") != AUTH_METHOD_VERSION:
        return None
    return data
