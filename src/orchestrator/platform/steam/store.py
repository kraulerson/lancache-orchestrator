"""Steam store appdetails lookup (public, no auth) — app type + name.

Used by library_sync to filter prefilled apps to actual games (type=='game')
and to get their display names, replacing the deleted worker's enumeration.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)
_URL = "https://store.steampowered.com/api/appdetails"


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an ``httpx.MockTransport``. None → real network."""
    return None


async def fetch_app_info(app_id: int) -> dict[str, str] | None:
    """Return {'type','name'} from the Steam store, or None on any failure."""
    transport = _build_transport()
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0, connect=10.0)}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(_URL, params={"appids": str(app_id), "filters": "basic"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        entry = resp.json().get(str(app_id), {})
    except ValueError:
        return None
    if not entry.get("success"):
        return None
    data = entry.get("data", {})
    name = data.get("name")
    app_type = data.get("type")
    if not isinstance(name, str) or not isinstance(app_type, str):
        return None
    return {"type": app_type, "name": name}
