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

import json  # noqa: E402,I001
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import TypedDict  # noqa: E402

# Steam-next imports (post-monkey-patch).
from steam.client import SteamClient  # type: ignore[import-not-found]  # noqa: E402
from steam.enums import EResult  # type: ignore[import-not-found]  # noqa: E402


# In-memory state for the worker's lifetime.
_client: SteamClient | None = None


class _Challenge(TypedDict):
    username: str
    password: str
    expires_at: float


_challenges: dict[str, _Challenge] = {}  # challenge_id -> {username, password, expires_at}


def _send(payload: dict[str, str | bool | dict[str, str | int]]) -> None:
    """Write a single JSON response line to stdout."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ok(msg_id: str, result: dict[str, str | int | bool]) -> None:
    _send({"msg_id": msg_id, "ok": True, "result": result})


def _err(msg_id: str, kind: str, message: str = "") -> None:
    _send({"msg_id": msg_id, "ok": False, "error": {"kind": kind, "message": message}})


def _ensure_client(credential_dir: str = "/var/lib/orchestrator/steam_session") -> SteamClient:
    global _client
    if _client is None:
        _client = SteamClient()
        Path(credential_dir).mkdir(parents=True, exist_ok=True)
        _client.set_credential_location(credential_dir)
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


_HANDLERS = {
    "auth.begin": _handle_auth_begin,
    "auth.complete": _handle_auth_complete,
    "auth.status": _handle_auth_status,
}


def main() -> int:
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
