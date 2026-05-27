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

# In-memory state for the worker's lifetime.
_client: SteamClient | None = None


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
    """Enumerate the operator's owned Steam apps (BL11).

    Pattern follows Spike A: walk `_client.licenses` → resolve packages
    via `get_product_info(packages=...)` → for each app_id in those
    packages, resolve app details via `get_product_info(apps=...)`.

    Live steam-next interaction is validated during UAT-6; unit tests
    exercise the IPC contract via a stub.
    """
    global _client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    try:
        apps: list[dict[str, object]] = []
        seen_app_ids: set[int] = set()
        licenses = getattr(_client, "licenses", None) or []

        package_ids: list[int] = []
        for license_obj in licenses:
            pkg_id = getattr(license_obj, "package_id", None)
            if pkg_id is not None:
                package_ids.append(int(pkg_id))

        if not package_ids:
            _ok(msg_id, {"apps": []})
            return

        # Batch-resolve packages to apps.
        pkg_info_response = _client.get_product_info(packages=package_ids) or {}
        packages_info = pkg_info_response.get("packages", {}) or {}
        candidate_app_ids: list[int] = []
        for _pkg_id_str, pkg_info in packages_info.items():
            appid_map = (pkg_info or {}).get("appids", {}) or {}
            for app_id_val in appid_map.values():
                try:
                    candidate_app_ids.append(int(app_id_val))
                except (TypeError, ValueError):
                    continue

        if not candidate_app_ids:
            _ok(msg_id, {"apps": []})
            return

        # Batch-resolve apps to product info. steam-next returns a dict
        # keyed by app_id (string or int — defensively coerce).
        app_info_response = _client.get_product_info(apps=candidate_app_ids) or {}
        apps_info = app_info_response.get("apps", {}) or {}

        for app_id in candidate_app_ids:
            if app_id in seen_app_ids:
                continue
            seen_app_ids.add(app_id)
            # Look up by both int and str keys (steam-next inconsistency).
            info = apps_info.get(app_id) or apps_info.get(str(app_id)) or {}
            common = info.get("common", {}) or {}
            name = common.get("name") or f"app_{app_id}"
            depots_dict = info.get("depots", {}) or {}
            depot_ids: list[int] = []
            for depot_key in depots_dict:
                key_str = str(depot_key)
                if key_str.isdigit():
                    depot_ids.append(int(key_str))
            apps.append({"app_id": int(app_id), "name": str(name), "depots": depot_ids})

        _ok(msg_id, {"apps": apps})
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


_HANDLERS = {
    "auth.begin": _handle_auth_begin,
    "auth.complete": _handle_auth_complete,
    "auth.status": _handle_auth_status,
    "library.enumerate": _handle_library_enumerate,
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
