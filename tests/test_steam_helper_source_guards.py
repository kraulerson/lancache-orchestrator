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


def _span_of_call(source: str, call: str) -> tuple[int, int]:
    """Offsets of the first `call` (ending in "(") through its matching ")"."""
    start = source.index(call)
    depth = 1
    pos = start + len(call)
    while depth:
        depth += {"(": 1, ")": -1}.get(source[pos], 0)
        pos += 1
    return start, pos


def _depot_key_retry_problems(source: str) -> list[str]:
    """Why `GetDepotKeyAsync` does not re-send dropped requests, or [] if it does."""
    head = source.index("GetDepotKeyAsync(")
    body_start = source.index("{", head)
    body = source[body_start : source.index("internal static byte[]? MapDepotKey", head)]
    problems = []
    if "RequestRetry.RunAsync(" not in body:
        return ["GetDepotKeyAsync does not call RequestRetry.RunAsync("]
    start, end = _span_of_call(body, "RequestRetry.RunAsync(")
    if "MapDepotKey(" in body[start:end]:
        problems.append("MapDepotKey( is inside the RequestRetry.RunAsync( call")
    if "MapDepotKey(" not in body[end:]:
        problems.append("MapDepotKey( does not come after the RequestRetry.RunAsync( call")
    if "attempts: 3" not in body[start:end]:
        problems.append("the RequestRetry.RunAsync( call does not pass attempts: 3")
    return problems


def test_depot_key_request_is_re_sent_but_an_eresult_refusal_is_not():
    """#369: Steam drops ~1% of depot-key requests with no answer. Only the request
    is retried (`RequestRetry.RunAsync`); `MapDepotKey` turns an explicit EResult
    into not_owned or a failure and must run once, after the retry, never inside it."""
    assert _depot_key_retry_problems(_GATEWAY.read_text()) == []


def test_the_depot_key_guard_flags_the_unfixed_gateway():
    """Mutation proof: the pre-#369 shape (one bare attempt) and a MapDepotKey
    inside the retry lambda are both reported."""
    head = "Task<byte[]?> GetDepotKeyAsync(uint d, uint a, CancellationToken ct)\n{\n"
    tail = "}\ninternal static byte[]? MapDepotKey(uint d, EResult r, byte[] k) => k;\n"
    unfixed = (
        head
        + 'var key = await Call(() => apps.Get(d, a).ToTask(), $"k", ct);\n'
        + "return MapDepotKey(d, key.Result, key.DepotKey);\n"
        + tail
    )
    assert _depot_key_retry_problems(unfixed)
    inside = (
        head + "return await RequestRetry.RunAsync(async () => MapDepotKey(d, 0, k), 3);\n" + tail
    )
    assert any("inside" in p for p in _depot_key_retry_problems(inside))
    one_attempt = _GATEWAY.read_text().replace("attempts: 3,", "attempts: 1,")
    assert any("attempts: 3" in p for p in _depot_key_retry_problems(one_attempt))


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
