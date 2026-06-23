"""Network bind-detection helpers (neutral home so both the control-plane API
and the data-plane agent can detect a non-loopback bind without importing each
other). Pure stdlib — no DB, no routers."""

from __future__ import annotations

import os
import sys

LOOPBACK_HOST_VALUES = frozenset({"127.0.0.1", "::1", "localhost"})


def detect_non_loopback_bind(settings_host: str) -> str | None:
    """Return the non-loopback host string if any signal indicates it, else None.
    Covers the settings host, the UVICORN_HOST env var, and `--host` in argv."""
    if settings_host not in LOOPBACK_HOST_VALUES:
        return settings_host
    uvicorn_host = os.environ.get("UVICORN_HOST")
    if uvicorn_host and uvicorn_host not in LOOPBACK_HOST_VALUES:
        return uvicorn_host
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            value = argv[i + 1]
            if value not in LOOPBACK_HOST_VALUES:
                return value
        elif arg.startswith("--host="):
            value = arg.split("=", 1)[1]
            if value not in LOOPBACK_HOST_VALUES:
                return value
    return None
