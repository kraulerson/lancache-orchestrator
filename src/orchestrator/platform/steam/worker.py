"""Steam worker subprocess — gevent-patched, runs steam-next.

This module is launched as a subprocess by SteamWorkerClient. Its first
line MUST be the gevent monkey-patch — steam-next requires this BEFORE
any other imports.

The orchestrator process NEVER imports this module. Only the subprocess
runs it (`python -u -m orchestrator.platform.steam.worker`).
"""

# gevent monkey-patch — keep this as the FIRST line of executable code.
# Importing this module from the orchestrator's asyncio process would
# corrupt the asyncio loop. SteamWorkerClient.start() invokes us via
# subprocess.exec; nothing else should import us.
from steam import monkey  # type: ignore[import-not-found]

monkey.patch_minimal()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, TypedDict  # noqa: E402

# F-UAT6-2: worker reads the credential dir from env (set by orchestrator
# client.start()). Falls back to the BL10 default. The orchestrator's
# Settings.steam_session_dir authoritatively configures this; this
# constant exists only as the safety net if the env var isn't forwarded.
_DEFAULT_SESSION_DIR = "/var/lib/orchestrator/steam_session"

# Steam-next imports (post-monkey-patch).
from steam.client import SteamClient  # type: ignore[import-not-found]  # noqa: E402
from steam.enums import EResult  # type: ignore[import-not-found]  # noqa: E402

# Pure-Python enumeration helpers (testable without gevent monkey-patch).
from orchestrator.platform.steam import enumerate as enumerate_module  # noqa: E402

# In-memory state for the worker's lifetime.
_client: SteamClient | None = None
# BL12: CDNClient instance, constructed lazily on first manifest.fetch.
# Holds a reference to _client + a content-server list, a thread pool,
# and a requests session — non-trivial init, so we don't create it
# eagerly. None until first use; reused across subsequent calls.
_cdn_client: Any = None


class _Challenge(TypedDict):
    username: str
    password: str
    expires_at: float


_challenges: dict[str, _Challenge] = {}  # challenge_id -> {username, password, expires_at}


def _send(payload: dict[str, Any]) -> None:
    """Write a single JSON response line to stdout."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ok(msg_id: str, result: dict[str, Any]) -> None:
    _send({"msg_id": msg_id, "ok": True, "result": result})


def _err(msg_id: str, kind: str, message: str = "") -> None:
    _send({"msg_id": msg_id, "ok": False, "error": {"kind": kind, "message": message}})


def _ensure_client(credential_dir: str | None = None) -> SteamClient:
    global _client
    if _client is None:
        # F-UAT6-2: pull from env (set by orchestrator client.start()) so
        # operator-customized ORCH_STEAM_SESSION_DIR actually wires through.
        # Argument override is for tests that want to inject a tmp_path.
        path = credential_dir or os.environ.get("ORCH_STEAM_SESSION_DIR", _DEFAULT_SESSION_DIR)
        _client = SteamClient()
        Path(path).mkdir(parents=True, exist_ok=True)
        _client.set_credential_location(path)
    return _client


def _handle_auth_begin(msg_id: str, params: dict[str, str]) -> None:
    username = params.get("username")
    password = params.get("password")
    if not username or not password:
        _err(msg_id, "InvalidCredentials", "missing username or password")
        return

    client = _ensure_client()
    try:
        result = client.login(username, password)
    except Exception as e:  # surface any steam-next exception as IPC error
        _err(msg_id, "SteamAPIError", str(e)[:200])
        return

    if result == EResult.OK:
        _ok(
            msg_id,
            {
                "authenticated": True,
                "steam_id": int(client.steam_id) if client.steam_id else 0,
                "licenses_count": len(client.licenses) if hasattr(client, "licenses") else 0,
            },
        )
        return

    if result in (EResult.AccountLoginDeniedNeedTwoFactor, EResult.AccountLogonDenied):
        challenge_type = (
            "mobile_authenticator"
            if result == EResult.AccountLoginDeniedNeedTwoFactor
            else "email_code"
        )
        challenge_id = str(uuid.uuid4())
        _challenges[challenge_id] = {
            "username": username,
            "password": password,
            "expires_at": time.time() + 300,  # 5 min
        }
        _ok(
            msg_id,
            {
                "authenticated": False,
                "challenge_id": challenge_id,
                "challenge_type": challenge_type,
            },
        )
        return

    _err(msg_id, "InvalidCredentials", f"steam returned {result!r}")


def _handle_auth_complete(msg_id: str, params: dict[str, str]) -> None:
    challenge_id = params.get("challenge_id", "")
    code = params.get("code", "")
    challenge = _challenges.get(challenge_id)
    if challenge is None:
        _err(msg_id, "ChallengeExpired", "no such challenge_id")
        return
    if time.time() > challenge["expires_at"]:
        _challenges.pop(challenge_id, None)
        _err(msg_id, "ChallengeExpired", "challenge expired")
        return

    client = _ensure_client()
    # steam-next picks two_factor_code vs auth_code based on the challenge type;
    # we pass both and let steam-next ignore the irrelevant one (matches Spike A).
    try:
        result = client.login(
            challenge["username"],
            challenge["password"],
            two_factor_code=code,
            auth_code=code,
        )
    except Exception as e:
        _challenges.pop(challenge_id, None)
        _err(msg_id, "SteamAPIError", str(e)[:200])
        return

    _challenges.pop(challenge_id, None)
    if result == EResult.OK:
        _ok(
            msg_id,
            {
                "authenticated": True,
                "steam_id": int(client.steam_id) if client.steam_id else 0,
                "licenses_count": len(client.licenses) if hasattr(client, "licenses") else 0,
            },
        )
        return
    _err(msg_id, "TwoFactorCodeMismatch", f"steam returned {result!r}")


def _handle_auth_status(msg_id: str, _params: dict[str, str]) -> None:
    global _client
    authenticated = _client is not None and _client.connected and _client.logged_on
    payload: dict[str, str | bool | int] = {
        "authenticated": authenticated,
        "last_check_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if authenticated and _client is not None and _client.steam_id:
        payload["steam_id"] = int(_client.steam_id)
    _ok(msg_id, payload)


def _handle_library_enumerate(msg_id: str, _params: dict[str, str]) -> None:
    """Enumerate the operator's owned Steam apps (BL11, post-spike-a2).

    Delegates to `enumerate.enumerate_apps` (pure, unit-testable). Waits
    up to 10s for `client.licenses` to populate after login — Steam
    sends `EMsg.ClientLicenseList` asynchronously and the dict is empty
    in the moments after `login()` returns. See spike_a2_steam_modern.md
    for the API mapping that UAT-6 surfaced.
    """
    global _client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    try:
        # Wait for the asynchronous license list to arrive before enumerating.
        enumerate_module.wait_for_licenses(_client, timeout=10.0)
        apps = enumerate_module.enumerate_apps(_client)
        _ok(msg_id, {"apps": apps})
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


def _handle_manifest_fetch(msg_id: str, params: dict[str, str]) -> None:
    """Fetch ALL depot manifests for a Steam app (BL12, post-spike-a3).

    Constructs a CDNClient lazily, calls `cdn.get_manifests(app_id)`,
    extracts {depot_id, manifest_gid, name, total_bytes, chunk_count,
    raw_b64} per manifest, returns the list.

    Per spike-a3:
    - `CDNDepotManifest.cdn_client` back-reference prevents pickle —
      use `.serialize()` to get raw protobuf bytes instead.
    - zstd-level-3 compresses the protobuf; base64 over JSON IPC.

    Live steam-next interaction is validated during UAT-9; unit tests
    exercise the orchestrator-side handler via a stub.
    """
    global _client, _cdn_client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    app_id_raw = params.get("app_id")
    if app_id_raw is None:
        _err(msg_id, "InvalidArgument", "manifest.fetch requires app_id")
        return
    try:
        app_id = int(app_id_raw)
    except (TypeError, ValueError):
        _err(msg_id, "InvalidArgument", f"app_id {app_id_raw!r} is not an integer")
        return

    try:
        # Steam-next imports inside the handler so module-import time
        # doesn't trigger any CDN-related network calls or thread-pool
        # allocation — they happen only when manifest.fetch is invoked.
        import base64 as _base64

        import zstandard as _zstd  # type: ignore[import-not-found]
        from steam.client.cdn import CDNClient  # type: ignore[import-not-found]

        if _cdn_client is None:
            _cdn_client = CDNClient(_client)

        manifests = _cdn_client.get_manifests(app_id, branch="public")
        compressor = _zstd.ZstdCompressor(level=3)

        payload: list[dict[str, Any]] = []
        for mfst in manifests:
            data = mfst.serialize()  # raw protobuf bytes
            compressed = compressor.compress(data)
            raw_b64 = _base64.b64encode(compressed).decode("ascii")

            # Sum chunk counts across all file mappings.
            chunk_count = 0
            for mapping in mfst.payload.mappings:
                chunk_count += len(mapping.chunks)

            total_bytes = int(mfst.metadata.cb_disk_original or 0)

            payload.append(
                {
                    "depot_id": int(mfst.depot_id),
                    "manifest_gid": int(mfst.gid),
                    "name": str(mfst.name or ""),
                    "total_bytes": total_bytes,
                    "chunk_count": chunk_count,
                    "raw_b64": raw_b64,
                }
            )

        _ok(msg_id, {"manifests": payload})
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


def _handle_manifest_expand(msg_id: str, params: dict[str, str]) -> None:
    """Deserialize a stored manifest BLOB → {depot_id, chunk_shas} (F7).

    Offline: zstd-decompress the stored bytes then reconstruct via
    `DepotManifest(data)` and iterate `payload.mappings[*].chunks[*].sha`.
    No CDNClient, no `_client`, no Steam session — pure protobuf parse, so
    validation works even when the operator's auth has expired.

    Chunk SHAs are deduped: the same chunk can appear in multiple file
    mappings (Steam content dedup), but in the cache it is one file.
    """
    raw_b64 = params.get("raw_b64")
    if not raw_b64:
        _err(msg_id, "InvalidArgument", "manifest.expand requires raw_b64")
        return
    try:
        import base64 as _base64

        import zstandard as _zstd
        from steam.core.manifest import DepotManifest  # type: ignore[import-not-found]

        compressed = _base64.b64decode(raw_b64)
        data = _zstd.ZstdDecompressor().decompress(compressed)
        mfst = DepotManifest(data)

        seen: dict[str, None] = {}
        for mapping in mfst.payload.mappings:
            for chunk in mapping.chunks:
                seen.setdefault(chunk.sha.hex(), None)

        _ok(msg_id, {"depot_id": int(mfst.depot_id), "chunk_shas": list(seen)})
    except Exception as e:
        _err(msg_id, "ManifestParseError", f"{type(e).__name__}: {e}"[:200])


_HANDLERS = {
    "auth.begin": _handle_auth_begin,
    "auth.complete": _handle_auth_complete,
    "auth.status": _handle_auth_status,
    "library.enumerate": _handle_library_enumerate,
    "manifest.fetch": _handle_manifest_fetch,
    "manifest.expand": _handle_manifest_expand,
}


def main() -> int:
    # Issue #95 item 7: stdin (worker reading from orchestrator) is NOT
    # length-capped here. The 10 MiB `MAX_IPC_LINE_BYTES` cap in
    # protocol.py is enforced on the response direction only — what the
    # client reads from the worker's stdout. The asymmetry is
    # intentional: stdin is a trusted channel from the orchestrator
    # process we ourselves spawned (see ADR-0013). Adding a cap here
    # would have to balance "longer requests may be legitimate"
    # (e.g., a manifest BLOB round-trip in BL12) against "no realistic
    # caller sends 100 MiB". Deferred until a concrete need exists.
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = req.get("msg_id", "")
        op = req.get("op", "")
        if op == "shutdown":
            _ok(msg_id, {"ok": True})
            return 0
        handler = _HANDLERS.get(op)
        if handler is None:
            _err(msg_id, "UnknownOp", op)
            continue
        handler(msg_id, req.get("params") or {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
