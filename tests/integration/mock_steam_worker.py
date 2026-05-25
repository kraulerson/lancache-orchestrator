"""Mock steam worker for integration tests.

Speaks the same IPC protocol as the real worker but returns canned
responses controlled via the MOCK_SCENARIO env var. Used by
tests/integration/test_steam_client_subprocess.py to validate IPC
plumbing without launching real steam-next.

Scenarios:
  - "no_2fa": auth.begin returns success immediately (no 2FA challenge)
  - "needs_mobile_auth": auth.begin returns challenge_id + mobile type
  - "bad_code": auth.complete always returns TwoFactorCodeMismatch
  - "ipc_silence": never responds (used to test client timeout)
  - "crash_on_third": exits with code 1 on the 3rd request (restart-storm)
"""

from __future__ import annotations

import json
import os
import sys
import uuid

# ruff: noqa: T201  (print is required for IPC protocol output)


def main() -> int:
    scenario = os.environ.get("MOCK_SCENARIO", "no_2fa")
    request_count = 0

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0  # parent closed stdin → exit

        request_count += 1

        if scenario == "ipc_silence":
            continue  # never respond

        if scenario == "crash_on_third" and request_count == 3:
            sys.exit(1)

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = req.get("msg_id", "")
        op = req.get("op", "")

        if op == "shutdown":
            print(json.dumps({"msg_id": msg_id, "ok": True, "result": {"ok": True}}), flush=True)
            return 0

        if op == "auth.begin":
            if scenario == "no_2fa":
                response = {
                    "msg_id": msg_id,
                    "ok": True,
                    "result": {
                        "authenticated": True,
                        "steam_id": 76561198000000000,
                        "licenses_count": 42,
                    },
                }
            elif scenario == "needs_mobile_auth":
                response = {
                    "msg_id": msg_id,
                    "ok": True,
                    "result": {
                        "authenticated": False,
                        "challenge_id": str(uuid.uuid4()),
                        "challenge_type": "mobile_authenticator",
                    },
                }
            else:
                response = {
                    "msg_id": msg_id,
                    "ok": False,
                    "error": {"kind": "InvalidCredentials", "message": ""},
                }
            print(json.dumps(response), flush=True)
            continue

        if op == "auth.complete":
            if scenario == "bad_code":
                response = {
                    "msg_id": msg_id,
                    "ok": False,
                    "error": {"kind": "TwoFactorCodeMismatch", "message": "code did not match"},
                }
            else:
                response = {
                    "msg_id": msg_id,
                    "ok": True,
                    "result": {
                        "authenticated": True,
                        "steam_id": 76561198000000000,
                        "licenses_count": 42,
                    },
                }
            print(json.dumps(response), flush=True)
            continue

        if op == "auth.status":
            response = {
                "msg_id": msg_id,
                "ok": True,
                "result": {
                    "authenticated": True,
                    "steam_id": 76561198000000000,
                    "last_check_at": "2026-05-24T12:00:00Z",
                },
            }
            print(json.dumps(response), flush=True)
            continue

        # Unknown op
        response = {"msg_id": msg_id, "ok": False, "error": {"kind": "UnknownOp", "message": op}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    sys.exit(main())
