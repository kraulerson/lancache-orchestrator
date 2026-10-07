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
_GATEWAY = (
    _REPO_ROOT / "tools" / "steam_manifest_helper" / "SteamManifestHelper" / "SteamKitGateway.cs"
)


def test_session_file_is_0600_from_creation_not_chmod_ed_afterwards():
    """#361 review cm4: the refresh token must never sit on disk in a wider mode.
    `UnixCreateMode` sets 0600 as the file is created; `SetUnixFileMode` after
    the write would leave a window in which the umask's mode applies."""
    source = _SESSION_STORE.read_text()
    assert "UnixCreateMode" in source
    assert "SetUnixFileMode" not in source


def test_gateway_logs_on_once_after_the_connect_retry_not_inside_it():
    """#361's core rule: one `user.LogOn` per connection attempt. The connect is
    retried (`ConnectRetry.RunAsync`), the logon never is, so a LogOn written
    inside the retry lambda, or a second LogOn anywhere, would bring back the
    login storm (and Steam's rate limit) that #361 removes."""
    source = _GATEWAY.read_text()
    assert source.count("user.LogOn(") == 1

    call = "ConnectRetry.RunAsync("
    assert source.count(call) == 1
    call_start = source.index(call)
    depth = 1
    pos = call_start + len(call)
    while depth:
        depth += {"(": 1, ")": -1}.get(source[pos], 0)
        pos += 1
    retry_span = source[call_start:pos]

    logon = source.index("user.LogOn(")
    assert logon >= pos, "user.LogOn( is inside the ConnectRetry.RunAsync( call"
    assert "user.LogOn(" not in retry_span
