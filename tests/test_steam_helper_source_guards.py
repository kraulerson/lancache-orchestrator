"""Static checks over the Steam manifest helper's C# source (#361).

Some properties cannot be unit-tested from xunit without racing the file system,
so they are pinned here by reading the source, in the style of test_dockerfile.py.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SESSION_STORE = (
    _REPO_ROOT / "tools" / "steam_manifest_helper" / "SteamManifestHelper" / "SessionStore.cs"
)


def test_session_file_is_0600_from_creation_not_chmod_ed_afterwards():
    """#361 review cm4: the refresh token must never sit on disk in a wider mode.
    `UnixCreateMode` sets 0600 as the file is created; `SetUnixFileMode` after
    the write would leave a window in which the umask's mode applies."""
    source = _SESSION_STORE.read_text()
    assert "UnixCreateMode" in source
    assert "SetUnixFileMode" not in source
